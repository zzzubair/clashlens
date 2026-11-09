"""League history is fetched again after Supercell publishes a Season's results.

The ended Season's official row appears minutes after the Season-opening
Reset, after the Reset pair asked, so each tracked player gets one more
league-history request 20 minutes later, and an operator command can
schedule the same for a Season already past.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import psycopg
from domain_test_support import domain_database
from reset_settlement_collection_postgres_support import (
    MONDAY_RESET,
    SEASON_RESET,
    _collector,
    _finish_reset_pairs,
    _players,
    _Provider,
    _provider,
)

from clashlens.collector_db import CollectorDatabase
from clashlens.league_history_refresh import schedule_refresh


def _refreshes(connection_info: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT player_id, due_at, status FROM collector_work
            WHERE kind = 'league_history_refresh' ORDER BY player_id
            """
        ).fetchall()


def test_season_opening_reset_fetches_league_history_again_after_20_minutes(
    database_url: str, tmp_path
) -> None:
    with (
        domain_database(database_url) as connection_info,
        _provider() as origin,
    ):
        first, second = _players(connection_info, "#2PP", "#8PY")
        _players(connection_info, "#9QQ", active=False)
        database = CollectorDatabase(connection_info)
        database.begin_reset(SEASON_RESET)
        _finish_reset_pairs(connection_info)
        database.begin_reset(MONDAY_RESET)
        due = SEASON_RESET + timedelta(minutes=20)
        # Only the Season-opening Reset adds them, one per frozen member.
        assert _refreshes(connection_info) == [
            (first, due, "pending"),
            (second, due, "pending"),
        ]

        def refresh_intents(now):
            return [
                intent
                for intent in database.pending_intents(
                    limit=20, now=now, interactive=False
                )
                if intent.kind == "league_history_refresh"
            ]

        assert refresh_intents(due - timedelta(minutes=1)) == []
        intent = refresh_intents(due)[0]
        result = asyncio.run(
            _collector(origin, database, tmp_path).intents.collect(intent)
        )

        assert result == "complete"
        assert [endpoint for endpoint, _at in _Provider.requests] == ["leaguehistory"]
        assert _refreshes(connection_info)[0][2] == "complete"


def test_season_end_history_checks_are_picked_beside_same_due_settlement_checks(
    database_url: str,
) -> None:
    # On 2 Nov 2026 every settlement check, due with these at 05:20 and
    # scheduled first, filled each pick, so no history check had started by
    # 07:14 although their own slots were free.
    with domain_database(database_url) as connection_info:
        _players(connection_info, "#2PP", "#8PY", "#9QQ", "#PQ2")
        database = CollectorDatabase(connection_info)
        database.begin_reset(SEASON_RESET)
        _finish_reset_pairs(connection_info)
        due = SEASON_RESET + timedelta(minutes=20)

        picked = database.pending_intents(
            limit=4, now=due, interactive=False, reset_limit=2
        )

        assert [intent.kind for intent in picked] == [
            "reset_settlement",
            "reset_settlement",
            "league_history_refresh",
            "league_history_refresh",
        ]


def test_refresh_command_skips_players_whose_refresh_is_still_waiting(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        _players(connection_info, "#2PP", "#8PY")
        _players(connection_info, "#9QQ", active=False)
        with psycopg.connect(connection_info) as connection:
            assert schedule_refresh(connection, SEASON_RESET, due_at=SEASON_RESET) == 2
            assert schedule_refresh(connection, SEASON_RESET, due_at=SEASON_RESET) == 0
            connection.execute(
                "UPDATE collector_work SET status = 'complete', completed_at = now()"
                " WHERE kind = 'league_history_refresh'"
            )
            # Once they finished, a later run fetches again.
            assert schedule_refresh(connection, SEASON_RESET, due_at=SEASON_RESET) == 2
