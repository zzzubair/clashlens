"""A late Reset reading through the worker: it ends one day and starts the
next, and a new-day battle saved later recalculates the day it ended."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from domain_test_support import domain_database, store_observation
from test_reconciliation_postgres import _battle_log, _processor, _profile
from test_reset_settlement_state_postgres import (
    BOUNDARIES,
    TAG,
    _process,
    _reset_work,
    _rows,
)

from clashlens import reconciliation_db
from clashlens.domain import allocate_trophies

DAYS = """
    SELECT DISTINCT ON (ranked_day_start) ranked_day_start, state,
           final_trophies_before_reset, start_trophies, next_start_trophies,
           input_evidence -> 'late_end_reading' ->> 'outcome'
    FROM ranked_day_versions ORDER BY ranked_day_start, version DESC, id DESC"""


def test_late_reading_ends_the_day_starts_the_next_and_follows_later_battles(
    database_url: str, archive_server
) -> None:
    # The day starts at 6,000 and takes eight 3-star defenses. The Reset
    # profile is read at 05:30, after a new-day 3-star attack at 05:10 and a
    # 2-star defense at 05:15; the Reset battle log just after it.
    boundary = BOUNDARIES["ordinary"]
    start = boundary - timedelta(days=1)
    template = json.loads(_battle_log())["items"][0]

    def battle(at: datetime, attack: bool, stars: int, tag: str) -> dict:
        return {**template, "attack": attack, "stars": stars,
                "destructionPercentage": 100 if stars == 3 else 60,
                "opponentPlayerTag": f"#{tag}PP", "battleTime": 120,
                "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z")}

    defenses = [battle(start + timedelta(hours=1 + i), False, 3, tag)
                for i, tag in enumerate("89QGRJCU")]
    new_day = [battle(boundary + timedelta(minutes=10), True, 3, "YL"),
               battle(boundary + timedelta(minutes=15), False, 2, "VL")]
    three_stars, two_stars = allocate_trophies(3, 100), allocate_trophies(2, 60)
    day_end = 6000 - 8 * three_stars.defender_loss
    reading = day_end + three_stars.attacker_gain - two_stars.defender_loss
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, start,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="midday-log",
            endpoint="battle_log", body=json.dumps({"items": defenses}).encode(),
            observed_at=start + timedelta(hours=12), normalized_tag=TAG,
        )[1])
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_profile(reading),
                            log=json.dumps({"items": new_day + defenses}).encode(),
                            profile_at=boundary + timedelta(minutes=30),
                            log_at=boundary + timedelta(minutes=30, seconds=1))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (start, boundary):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=boundary + timedelta(hours=1), request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        verified = {row[0]: row[1:] for row in _rows(connection_info, DAYS)}
        # A later log shows a 05:20 defense the Reset log had missed: the
        # reading held it, so less every battle it no longer matches.
        missed = battle(boundary + timedelta(minutes=20), False, 3, "RL")
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="later-log",
            endpoint="battle_log",
            body=json.dumps({"items": [missed, *new_day, *defenses]}).encode(),
            observed_at=boundary + timedelta(hours=2), normalized_tag=TAG,
        )[1]])
        # During the new day, its saved battle queues the new day's
        # calculation; that starts with the day the late reading ended.
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary,
                now=boundary + timedelta(hours=2), request_key="later-log",
            )
            assert processor.process_job(job, owner="later") is not None
        finally:
            database.close()
        after = {row[0]: row[1:] for row in _rows(connection_info, DAYS)}

    assert verified[start] == ("Complete", day_end, 6000, day_end, "verified")
    # The next day starts from it, though its own start reading came late.
    assert verified[boundary][2] == day_end
    assert after[start][0] == "Inconsistent"
    assert after[start][4] == "contradicted"
