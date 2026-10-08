"""A player first tracked after a Season's opening Reset still gets Day 1.

The cases follow the October 2026 Season: most late players were first seen
during Day 1, a few were first found on Day 2 as someone's opponent, and some
crawl imports had not joined the Season yet. Their first battle log reaches
back past Day 1's start, and every row carries its own time.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, repair_season, store_observation
from test_reconciliation_postgres import BATTLE_FIXTURE, _profile
from test_reset_settlement_state_postgres import (
    BOUNDARIES,
    NEW_SEASON,
    TAG,
    _process,
    _reset_work,
    _season_profile,
)

from clashlens import domain_repair, first_battle_log, reconciliation_db
from clashlens.db import PYTHON_BACKFILL_PRIORITY, Database
from clashlens.domain import allocate_trophies
from clashlens.reconciliation import RECONCILIATION_RULE_VERSION

DAY_1 = BOUNDARIES["season"]
DAY_2 = DAY_1 + timedelta(days=1)
WIN = allocate_trophies(3, 100).attacker_gain
LOSS = allocate_trophies(2, 60).defender_loss


def _log(*battles: tuple[datetime, bool], filler: list[datetime] = ()) -> bytes:
    """A battle log: Legend battles (time, attack) won 3 stars as attacker and
    lost 2 stars at 60% as defender, plus multiplayer battles at ``filler``."""
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    items = [
        {
            **template, "attack": attack, "battleTime": 120,
            "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
            "stars": 3 if attack else 2, "destructionPercentage": 100 if attack else 60,
            "opponentPlayerTag": f"#{'89QGRJCUV'[index % 9]}{'PY'[index // 9]}P",
        }
        for index, (at, attack) in enumerate(battles)
    ] + [
        {**template, "battleType": "homeVillage", "battleTime": 120,
         "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
         "opponentPlayerTag": "#9PP"}
        for at in filler
    ]
    return json.dumps({"items": items}).encode()


def _new_season_profile(trophies: int, tag: str = TAG) -> bytes:
    payload = json.loads(_season_profile(trophies, NEW_SEASON))
    payload["tag"] = tag
    return json.dumps(payload).encode()


def _first_seen(connection_info, archive_server, at, *, profile, log, tag=TAG):
    """Save a player's first profile and battle log, read at ``at``."""
    return [
        store_observation(
            connection_info, archive_server, occurrence_key=f"{tag}-{endpoint}-{at}",
            endpoint=endpoint, body=body, observed_at=at, normalized_tag=tag,
        )[1]
        for endpoint, body in (("profile", profile), ("battle_log", log))
    ]


def _day_1(connection_info: str, tag: str = TAG):
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT DISTINCT ON (day.ranked_day_start)
                   day.state, day.confidence, day.start_trophies,
                   day.final_trophies_before_reset, day.coverage_complete,
                   day.failure_reasons,
                   day.input_evidence -> 'start_baseline_evidence'
                       ->> 'start_trophies_source'
            FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            WHERE player.normalized_tag = %s AND day.ranked_day_start = %s
            ORDER BY day.ranked_day_start, day.version DESC
            """,
            (tag, DAY_1),
        ).fetchone()


# Day-1 battles: attacks at 06:00 and 09:00, and a defense at 11:00 in the
# one-defense case.
ATTACKS = [(DAY_1 + timedelta(hours=1), True), (DAY_1 + timedelta(hours=4), True)]
DEFENSE = [(DAY_1 + timedelta(hours=6), False)]


@pytest.mark.parametrize("first_log,battles,expected", [
    # Full 50-row log whose oldest row is from before Day 1: Day 1 is
    # complete, from the Season rule's 5,000, so only inferred.
    ("reaches_back", ATTACKS, ("Complete", "inferred", True)),
    # A short log is the player's whole log, so it reaches back too.
    ("short", ATTACKS, ("Complete", "inferred", True)),
    # A full log that starts after Day 1's start may have lost battles, so
    # the day is uncertain, as any day with a coverage gap.
    ("full_from_day_1", ATTACKS, ("Partial", "uncertain", False)),
    # With 1 to 7 defenses the automatic defense loss averages Day 1's own
    # defenses: the day before, never tracked, is the previous Season's. On
    # Day 1, 2 attacks and 1 defense are charged for 2 - 1 missing defenses.
    ("reaches_back", ATTACKS + DEFENSE, ("Complete", "inferred", True)),
])
def test_player_first_seen_during_day_1_gets_a_season_rule_start(
    database_url: str, archive_server, first_log: str, battles, expected
) -> None:
    first_at = DAY_1 + timedelta(hours=7, minutes=45)  # 12:45 UTC
    # Multiplayer battles fill a full log to the game's 50 rows.
    full = range(50 - len(battles))
    filler = {
        "reaches_back": [DAY_1 - timedelta(hours=9 - i / 10) for i in full],
        "short": [],
        "full_from_day_1": [DAY_1 + timedelta(hours=2, minutes=i) for i in full],
    }[first_log]
    gained = sum(WIN if attack else -LOSS for _, attack in battles)
    automatic = LOSS if len(battles) == 3 else 0
    log = _log(*battles, filler=filler)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _first_seen(connection_info, archive_server, first_at,
                           profile=_new_season_profile(5000 + gained), log=log)
        # This morning's Reset reading ends Day 1 at 5,000 plus its battles,
        # less any automatic defense loss.
        jobs += _reset_work(connection_info, archive_server, DAY_2,
                            profile=_new_season_profile(5000 + gained - automatic),
                            log=log)
        _process(connection_info, archive_server, jobs)
        day = _day_1(connection_info)
    state, confidence, coverage = expected
    assert day[:2] == (state, confidence)
    assert day[2] == 5000 and day[6] == "season_rule"
    assert day[4] is coverage
    if state == "Complete":
        assert day[3] == 5000 + gained - automatic
    if not coverage:
        assert "missing_start_battle_log_baseline" in day[5]


def _queued_priorities(connection_info: str, key_prefix: str) -> set[int]:
    with psycopg.connect(connection_info) as connection:
        return {
            row[0] for row in connection.execute(
                "SELECT DISTINCT priority FROM python_processing_jobs"
                " WHERE deduplication_key LIKE %s",
                (key_prefix + "%",),
            )
        }


def test_opponent_found_on_day_2_gets_day_1_and_the_backfill_finds_the_rest(
    database_url: str, archive_server
) -> None:
    found_at = DAY_2 + timedelta(hours=9)  # 14:00 UTC on Day 2
    older = [DAY_1 - timedelta(hours=9 - i / 10) for i in range(47)]
    day_1_attack = (DAY_1 + timedelta(hours=13), True)
    day_2_attack = (DAY_2 + timedelta(hours=5), True)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # The opponent: first found on Day 2, with a Day 1 battle in their log.
        jobs = _first_seen(
            connection_info, archive_server, found_at, tag="#2YY",
            profile=_new_season_profile(5000 + 2 * WIN, "#2YY"),
            log=_log(day_1_attack, day_2_attack, filler=older),
        )
        # A crawl import not yet in the Season: Season ID 0, no Day 1 Legend
        # battle.
        season_zero = json.loads(_profile(5000, "#2LL"))
        season_zero["currentLeagueSeasonId"] = 0
        jobs += _first_seen(
            connection_info, archive_server, found_at, tag="#2LL",
            profile=json.dumps(season_zero).encode(),
            log=_log(day_2_attack, filler=older),
        )
        # A player first seen during Day 1, whose Day 1 the Reset built.
        jobs += _first_seen(
            connection_info, archive_server, DAY_1 + timedelta(hours=8),
            profile=_new_season_profile(5000 + 2 * WIN),
            log=_log(*ATTACKS, filler=older),
        )
        jobs += _reset_work(connection_info, archive_server, DAY_2,
                            profile=_new_season_profile(5000 + 2 * WIN),
                            log=_log(*ATTACKS, filler=older))
        _process(connection_info, archive_server, jobs)
        opponent = _day_1(connection_info, "#2YY")
        not_in_season = _day_1(connection_info, "#2LL")
        # Both were queued when their logs were saved. Players tracked before
        # that existed have no such job, which the backfill is for.
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                "DELETE FROM python_processing_jobs"
                " WHERE deduplication_key LIKE 'reconcile:first-log:%'"
            )

        database = Database(connection_info)
        try:
            preview = first_battle_log.backfill(
                database, str(NEW_SEASON), queue=False, max_jobs=100
            )
            queued = first_battle_log.backfill(
                database, str(NEW_SEASON), queue=True, max_jobs=100
            )
            again = first_battle_log.backfill(
                database, str(NEW_SEASON), queue=True, max_jobs=100
            )
        finally:
            database.close()
        # The batch yields to any higher-priority work its thread can claim.
        priorities = _queued_priorities(connection_info, "reconcile:first-log:")
        _process(connection_info, archive_server, [])
        day_1_joiner = _day_1(connection_info)

    # No Reset reading ends the opponent's Day 1, but their first log holds
    # all of it, so it shows its end-of-day total. With no reading, whether
    # they were shielded stays unknown.
    assert opponent[:3] == ("Partial", "uncertain", 5000)
    assert opponent[3] == 5000 + WIN and opponent[4] is True
    assert "missing_end_baseline" in opponent[5]
    assert not_in_season is None
    assert day_1_joiner[:4] == ("Complete", "inferred", 5000, 5000 + 2 * WIN)
    # The backfill lists the Day 1 joiner and the opponent, not the crawl
    # import.
    assert preview == {
        "season": str(NEW_SEASON), "players": 2, "first_seen_day_1": 1,
        "first_seen_later_with_earlier_battles": 1,
        "days": {DAY_1.isoformat(): 2},
        "already_queued": 0, "waiting_for_profile": 0, "queued": 0,
        "left_to_queue": 2,
    }
    assert (queued["queued"], queued["left_to_queue"]) == (2, 0)
    assert (again["queued"], again["already_queued"]) == (0, 2)
    assert priorities == {PYTHON_BACKFILL_PRIORITY}


def test_opponent_whose_battle_log_is_processed_before_their_profile_gets_day_1(
    database_url: str, archive_server
) -> None:
    found_at = DAY_2 + timedelta(hours=9)  # 14:00 UTC on Day 2
    older = [DAY_1 - timedelta(hours=9 - i / 10) for i in range(47)]
    day_1_attack = (DAY_1 + timedelta(hours=13), True)
    day_2_attack = (DAY_2 + timedelta(hours=5), True)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_job, log_job = _first_seen(
            connection_info, archive_server, found_at, tag="#2YY",
            profile=_new_season_profile(5000 + 2 * WIN, "#2YY"),
            log=_log(day_1_attack, day_2_attack, filler=older),
        )
        # The first battle log finishes before the profile the Season rule
        # needs, so Day 1 waits for it, and so does the backfill.
        _process(connection_info, archive_server, [log_job])
        database = Database(connection_info)
        try:
            waiting = first_battle_log.backfill(
                database, str(NEW_SEASON), queue=True, max_jobs=100
            )
        finally:
            database.close()
        _process(connection_info, archive_server, [profile_job])
        opponent = _day_1(connection_info, "#2YY")

    assert (waiting["waiting_for_profile"], waiting["queued"]) == (1, 0)
    assert opponent[:4] == ("Partial", "uncertain", 5000, 5000 + WIN)
    assert opponent[4] is True and opponent[6] == "season_rule"


@pytest.mark.parametrize("log_first", [False, True])
def test_day_1_joiner_processed_after_the_reset_gets_day_1_in_either_order(
    database_url: str, archive_server, log_first: bool
) -> None:
    older = [DAY_1 - timedelta(hours=9 - i / 10) for i in range(48)]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_job, log_job = _first_seen(
            connection_info, archive_server, DAY_1 + timedelta(hours=7, minutes=45),
            profile=_new_season_profile(5000 + 2 * WIN),
            log=_log(*ATTACKS, filler=older),
        )
        # Day 1 has ended, so only whichever of the two finishes last can
        # queue it.
        for job in [log_job, profile_job] if log_first else [profile_job, log_job]:
            _process(connection_info, archive_server, [job])
        day = _day_1(connection_info)

    assert day[2] == 5000 and day[6] == "season_rule"


def test_older_first_log_processed_after_a_newer_one_recalculates_day_1(
    database_url: str, archive_server
) -> None:
    found_at = DAY_2 + timedelta(hours=9)  # 14:00 UTC on Day 2
    day_1_attack = (DAY_1 + timedelta(hours=13), True)
    day_2_attack = (DAY_2 + timedelta(hours=5), True)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        profile_job, older_job = _first_seen(
            connection_info, archive_server, found_at, tag="#2YY",
            profile=_new_season_profile(5000 + 2 * WIN, "#2YY"),
            log=_log(day_1_attack, day_2_attack, filler=[
                DAY_1 - timedelta(hours=9 - i / 10) for i in range(48)
            ]),
        )
        # An hour later the full log's oldest row is on Day 1, so it cannot
        # show that it holds all of Day 1.
        newer_at = found_at + timedelta(hours=1)
        _, newer_job = store_observation(
            connection_info, archive_server, occurrence_key=f"#2YY-newer-{newer_at}",
            endpoint="battle_log", observed_at=newer_at, normalized_tag="#2YY",
            body=_log(day_1_attack, day_2_attack, filler=[
                DAY_1 + timedelta(hours=14, minutes=i) for i in range(48)
            ]),
        )
        _process(connection_info, archive_server, [profile_job, newer_job])
        from_newer = _day_1(connection_info, "#2YY")
        # The worker reaches the older log last; it becomes the earliest
        # saved log and recalculates Day 1 from it.
        _process(connection_info, archive_server, [older_job])
        from_older = _day_1(connection_info, "#2YY")

    assert from_newer[2] == 5000 and from_newer[4] is False
    assert from_older[:4] == ("Partial", "uncertain", 5000, 5000 + WIN)
    assert from_older[4] is True and from_older[6] == "season_rule"


def test_day_1_saved_with_the_previous_season_average_is_recalculated_once(
    database_url: str, archive_server, monkeypatch
) -> None:
    older = [DAY_1 - timedelta(hours=9 - i / 10) for i in range(47)]
    # Day 1 charges 2 attacks and 1 defense for one missing defense.
    automatic = LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # Day 1 joiners: one with a single defense, one with none.
        ending = 5000 + 2 * WIN - LOSS - automatic
        jobs = _first_seen(connection_info, archive_server, DAY_1 + timedelta(hours=8),
                           profile=_new_season_profile(ending),
                           log=_log(*ATTACKS, *DEFENSE, filler=older))
        jobs += _reset_work(connection_info, archive_server, DAY_2,
                            profile=_new_season_profile(ending),
                            log=_log(*ATTACKS, *DEFENSE, filler=older))
        jobs += _first_seen(connection_info, archive_server, DAY_1 + timedelta(hours=8),
                            tag="#2YY", profile=_new_season_profile(5000 + 2 * WIN, "#2YY"),
                            log=_log(*ATTACKS, filler=older))
        # Saved before the fix, Day 1 needed the previous Season's last day.
        original = reconciliation_db.reconcile_ranked_day
        monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day",
                            lambda data: original(replace(data, season_first_day=False)))
        _process(connection_info, archive_server, jobs)
        before = _day_1(connection_info)
        # The run before this correction already queued, and finished, the
        # player under its own key.
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
            ).fetchone()[0]
            first_battle_log._queue(
                connection, player_id, DAY_1, None, trigger="season_day_1",
                key=f"reconcile:season-day-1:{player_id}:"
                f"{DAY_1:%Y-%m-%dT%H:%M:%SZ}:{RECONCILIATION_RULE_VERSION}",
            )
        _process(connection_info, archive_server, [])
        unchanged = _day_1(connection_info)
        monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day", original)

        preview, queued = repair_season(connection_info, str(NEW_SEASON))
        again = repair_season(connection_info, str(NEW_SEASON))[1]
        # The batch yields to any higher-priority work its thread can claim.
        priorities = _queued_priorities(connection_info, "reconcile:season-repair:")
        _process(connection_info, archive_server, [])
        after = _day_1(connection_info)

    assert before[0] == "Partial" and before[3] is None
    assert (unchanged[0], unchanged[3]) == ("Partial", None)
    assert "automatic_defense_basis_unavailable" in before[5]
    # Each player with a saved day of the Season is recalculated once.
    assert (preview["players"], preview["left_to_queue"]) == (2, 2)
    assert (queued["phase"], queued["queued"], queued["left_to_queue"]) == ("days", 2, 0)
    assert (again["phase"], again["queued"], again["unfinished"]) == ("days", 0, 2)
    assert priorities == {PYTHON_BACKFILL_PRIORITY}
    assert after[:4] == ("Complete", "inferred", 5000, ending)


def test_day_flagged_by_logs_sharing_only_other_battles_is_recalculated_once(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Two Day 1 attacks, then a full log of multiplayer battles only: it
    # shares 48 multiplayer rows with the first log and no Legend battle.
    older = [DAY_1 - timedelta(hours=9 - i / 10) for i in range(48)]
    later = [DAY_1 + timedelta(hours=8), DAY_1 + timedelta(hours=8, minutes=30)]
    only_multiplayer = _log(filler=later + older)
    ending = 5000 + 2 * WIN
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _first_seen(connection_info, archive_server, DAY_1 + timedelta(hours=7),
                           profile=_new_season_profile(ending),
                           log=_log(*ATTACKS, filler=older))
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="multiplayer-log",
            endpoint="battle_log", body=only_multiplayer,
            observed_at=DAY_1 + timedelta(hours=9), normalized_tag=TAG,
        )[1])
        jobs += _reset_work(connection_info, archive_server, DAY_2,
                            profile=_new_season_profile(ending), log=only_multiplayer)
        # Saved before the fix, only shared Legend battles showed overlap.
        original = reconciliation_db.reconcile_ranked_day
        monkeypatch.setattr(
            reconciliation_db, "reconcile_ranked_day",
            lambda data: original(replace(data, coverage_observations=tuple(
                replace(log, source_row_ids=()) for log in data.coverage_observations
            ))),
        )
        _process(connection_info, archive_server, jobs)
        before = _day_1(connection_info)
        monkeypatch.setattr(reconciliation_db, "reconcile_ranked_day", original)

        preview, queued = repair_season(connection_info, str(NEW_SEASON))
        with psycopg.connect(connection_info) as connection:
            job_id, player_id = connection.execute(
                "UPDATE python_processing_jobs SET status = 'failed',"
                " failure_category = 'invalid_work_input'"
                " WHERE deduplication_key LIKE 'reconcile:season-repair:%'"
                " RETURNING id, (input_json ->> 'player_id')::bigint"
            ).fetchone()
        database = Database(connection_info)
        try:
            failed = domain_repair.season_repair(
                database, str(NEW_SEASON), "receipt", max_jobs=100
            )
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE python_processing_jobs SET status = 'pending',"
                " failure_category = NULL WHERE id = %s", (job_id,),
            )
        priorities = _queued_priorities(connection_info, "reconcile:season-repair:")
        _process(connection_info, archive_server, [])
        after = _day_1(connection_info)

    assert (before[0], before[3]) == ("Partial", None)
    assert "battle_log_overlap_gap" in before[5]
    assert (preview["players"], preview["left_to_queue"], preview["failed"]) == (1, 1, 0)
    assert (queued["queued"], queued["left_to_queue"]) == (1, 0)
    # A failed recalculation is kept and listed, not queued again.
    assert (failed["left_to_queue"], failed["unfinished"], failed["failed"]) == (0, 0, 1)
    assert failed["failed_blockers"] == [{
        "job_id": job_id, "player_id": player_id,
        "failure_category": "invalid_work_input",
    }]
    # The receipt keeps the day as it was before the repair.
    day_1 = DAY_1.isoformat()
    assert failed["days"]["before"][day_1]["states"] == {"Partial": 1}
    assert "battle_log_overlap_gap" in failed["days"]["before"][day_1]["reasons"]
    assert priorities == {PYTHON_BACKFILL_PRIORITY}
    assert after[:4] == ("Complete", "inferred", 5000, ending)
