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
from domain_test_support import domain_database
from test_first_battle_log_postgres import DAY_1, DAY_2, _log, _new_season_profile
from test_reconciliation_postgres import _profile
from test_reset_settlement_state_postgres import TAG, _process, _reset_work

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
