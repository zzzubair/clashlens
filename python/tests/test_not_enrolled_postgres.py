"""Days before a late joiner signed up are marked not enrolled, only when proven.

A Legend I player who has not signed up for the Season yet has profiles
showing Season ID 0. Once a later profile in the same Season shows them
signed up, any battle-free day that a "not signed up" profile saved after
it ended covers is marked ``not_enrolled``; other days are unchanged.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from test_first_battle_log_postgres import (
    DAY_1,
    DAY_2,
    WIN,
    _log,
    _new_season_profile,
)
from test_reconciliation_postgres import _profile
from test_reset_settlement_state_postgres import TAG, _process, _reset_work

from clashlens import first_battle_log, reset_baselines
from clashlens.db import Database
from clashlens.domain import ranked_day_for

DAY_3 = DAY_2 + timedelta(days=1)


def _not_signed_up() -> bytes:
    payload = json.loads(_profile(5000))
    payload["currentLeagueSeasonId"] = 0
    return json.dumps(payload).encode()


def _reasons(connection_info: str) -> dict:
    with psycopg.connect(connection_info) as connection:
        return {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT DISTINCT ON (day.ranked_day_start)
                       day.ranked_day_start, day.failure_reasons
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE player.normalized_tag = %s
                ORDER BY day.ranked_day_start, day.version DESC
                """,
                (TAG,),
            )
        }


def test_days_before_a_proven_late_sign_up_are_not_enrolled(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, DAY_1,
                           profile=_not_signed_up(), log=_log())
        jobs += _reset_work(connection_info, archive_server, DAY_2,
                            profile=_not_signed_up(), log=_log())
        _process(connection_info, archive_server, jobs)
        # Not signed up yet: nothing proves this Season is the one they join.
        assert "not_enrolled" not in _reasons(connection_info)[DAY_1]

        jobs = _reset_work(connection_info, archive_server, DAY_3,
                           profile=_new_season_profile(5000), log=_log())
        _process(connection_info, archive_server, jobs)
        reasons = _reasons(connection_info)

    # Day 1 ended before a profile still showed no sign-up; Day 2 did not.
    assert "not_enrolled" in reasons[DAY_1]
    assert "not_enrolled" not in reasons[DAY_2]


def test_every_proven_day_before_a_late_sign_up_is_calculated(
    database_url: str, archive_server
) -> None:
    day_8, day_9, day_10 = (DAY_1 + timedelta(days=n) for n in (7, 8, 9))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # First found on Day 8, not signed up and with no battles.
        jobs = _reset_work(connection_info, archive_server, day_8,
                           profile=_not_signed_up(), log=_log())
        _process(connection_info, archive_server, jobs)
        jobs = _reset_work(connection_info, archive_server, day_10,
                           profile=_new_season_profile(5000), log=_log())
        _process(connection_info, archive_server, jobs)
        reasons = _reasons(connection_info)

    # Days 1-7 ended before the Day 8 profile; Day 9 had no such profile.
    for day in range(7):
        assert "not_enrolled" in reasons[DAY_1 + timedelta(days=day)]
    assert "not_enrolled" not in reasons[day_9]


def test_a_not_signed_up_profile_processed_after_the_sign_up_still_proves_it(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        late = _reset_work(connection_info, archive_server, DAY_2,
                           profile=_not_signed_up(), log=_log())
        jobs = _reset_work(connection_info, archive_server, DAY_3,
                           profile=_new_season_profile(5000), log=_log())
        _process(connection_info, archive_server, jobs)
        assert "not_enrolled" not in _reasons(connection_info).get(DAY_1, [])
        _process(connection_info, archive_server, late)
        reasons = _reasons(connection_info)

    assert "not_enrolled" in reasons[DAY_1]


def _sign_up_day(connection_info: str) -> tuple:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT DISTINCT ON (ranked_day_start) state, confidence,
                   start_trophies, next_start_trophies,
                   input_evidence -> 'start_baseline_evidence'
                       ->> 'start_trophies_source'
            FROM ranked_day_versions WHERE ranked_day_start = %s
            ORDER BY ranked_day_start, version DESC
            """,
            (DAY_2,),
        ).fetchone()


@pytest.mark.parametrize("saved_before_rule", [False, True])
def test_sign_up_day_starts_at_5000_by_the_season_rule(
    database_url: str, archive_server, monkeypatch, saved_before_rule: bool
) -> None:
    # As #YPG2LRYQ on 6 October 2026: the Reset reading names Season 0 at
    # 5,000, the player signs up at 05:19 and battles the same day.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, DAY_2,
                           profile=_not_signed_up(), log=_log())
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="sign-up",
            endpoint="profile", body=_new_season_profile(5000),
            observed_at=DAY_2 + timedelta(minutes=19), normalized_tag=TAG,
        )[1])
        jobs += _reset_work(connection_info, archive_server, DAY_3,
                            profile=_new_season_profile(5000 + WIN),
                            log=_log((DAY_2 + timedelta(hours=1), True)))
        if saved_before_rule:
            # Saved before the rule: only a Season's first Reset used it.
            original = reset_baselines._season_rule_holds
            monkeypatch.setattr(
                reset_baselines, "_season_rule_holds",
                lambda *args, before=None: True if before else original(*args),
            )
        _process(connection_info, archive_server, jobs)
        if saved_before_rule:
            monkeypatch.undo()
            assert _sign_up_day(connection_info)[0] == "Partial"
            season = ranked_day_for(DAY_2).official_season_id
            database = Database(connection_info)
            try:
                preview = first_battle_log.requeue_sign_up_days(
                    database, season, queue=False, max_jobs=100)
                queued = first_battle_log.requeue_sign_up_days(
                    database, season, queue=True, max_jobs=100)
            finally:
                database.close()
            assert (preview["players"], preview["queued"]) == (1, 0)
            assert (queued["queued"], queued["left_to_queue"]) == (1, 0)
            _process(connection_info, archive_server, [])
        day = _sign_up_day(connection_info)

    assert day == ("Complete", "inferred", 5000, 5000 + WIN, "season_rule")


def _sign_up_days(connection_info: str) -> int:
    database = Database(connection_info)
    try:
        return first_battle_log.requeue_sign_up_days(
            database, ranked_day_for(DAY_2).official_season_id,
            queue=False, max_jobs=100,
        )["players"]
    finally:
        database.close()


def test_a_season_profile_read_before_the_reset_but_saved_last_rules_out_sign_up(
    database_url: str, archive_server
) -> None:
    # The same profile read at 04:50 and 05:20: the later reading is saved
    # first, so the saved profile is first dated 05:20.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        late, early = (
            store_observation(
                connection_info, archive_server, occurrence_key=key,
                endpoint="profile", body=_new_season_profile(5240),
                observed_at=DAY_2 + timedelta(minutes=minutes), normalized_tag=TAG,
            )[1]
            for key, minutes in (("late", 20), ("early", -10))
        )
        _process(connection_info, archive_server, [late, early])
        jobs = _reset_work(connection_info, archive_server, DAY_2,
                           profile=_not_signed_up(), log=_log(),
                           profile_at=DAY_2 + timedelta(minutes=1))
        jobs += _reset_work(connection_info, archive_server, DAY_3,
                            profile=_new_season_profile(5240), log=_log())
        _process(connection_info, archive_server, jobs)
        day = _sign_up_day(connection_info)
        players = _sign_up_days(connection_info)

    assert (day[2], day[4], players) == (None, None, 0)


def test_earlier_legend_battles_in_the_season_rule_out_sign_up(
    database_url: str, archive_server
) -> None:
    # First found at the Day 2 Reset, whose battle log shows Day 1 battles.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, DAY_2,
                           profile=_not_signed_up(),
                           log=_log((DAY_1 + timedelta(hours=1), True)))
        jobs += _reset_work(connection_info, archive_server, DAY_3,
                            profile=_new_season_profile(5160),
                            log=_log((DAY_1 + timedelta(hours=1), True)))
        _process(connection_info, archive_server, jobs)
        day = _sign_up_day(connection_info)
        players = _sign_up_days(connection_info)

    assert (day[2], day[4], players) == (None, None, 0)


def test_a_battle_before_a_delayed_reset_reading_rules_out_sign_up(
    database_url: str, archive_server
) -> None:
    # Already signed up: a battle at 05:06, the Season 0 reading at 05:10,
    # then a profile naming the Season at 05:20.
    battle = (DAY_2 + timedelta(minutes=6), True)
    read_at = DAY_2 + timedelta(minutes=10)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, DAY_2,
                           profile=_not_signed_up(), log=_log(battle),
                           profile_at=read_at, log_at=read_at)
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="signed",
            endpoint="profile", body=_new_season_profile(5140 + WIN),
            observed_at=DAY_2 + timedelta(minutes=20), normalized_tag=TAG,
        )[1])
        jobs += _reset_work(connection_info, archive_server, DAY_3,
                            profile=_new_season_profile(5140 + WIN),
                            log=_log(battle))
        _process(connection_info, archive_server, jobs)
        day = _sign_up_day(connection_info)
        players = _sign_up_days(connection_info)

    assert (day[2], day[4], players) == (None, None, 0)
