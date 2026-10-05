"""An ended Season's saved days stay reachable before its summary is stored.

A summary is stored only by Day 28's Complete publication or a backfill, so
the Season that ended at the latest Reset is summarized on read until then.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database
from test_player_season_summaries_postgres import _log, _player, _ranked

from clashlens import api_players
from clashlens.api_db import ApiDatabase
from clashlens.season_summaries import materialize_player_season

SEPTEMBER = "1788757200"
OCTOBER = "1791176400"
SEPTEMBER_START = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
OCTOBER_START = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)
NOW = OCTOBER_START + timedelta(hours=1)


def _day(connection, player_id, season, season_start, number):
    start = season_start + timedelta(days=number - 1)
    version_id = _ranked(
        connection, player_id, number, start, start + timedelta(days=1), season=season
    )
    _log(connection, player_id, number, version_id, start, season=season)


def test_ended_season_days_are_listed_and_shown_before_a_summary(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                for number in range(24, 29):
                    _day(connection, player_id, SEPTEMBER, SEPTEMBER_START, number)
                _day(connection, player_id, OCTOBER, OCTOBER_START, 1)
                connection.commit()

            seasons = api_players.list_player_seasons(database, "#2PP", now=NOW)
            assert [
                (s["official_season_id"], s["source"], s["days_observed"])
                for s in seasons
            ] == [(SEPTEMBER, "tracked_summary", 5)]
            detail = api_players.get_player_season_summary(
                database, "#2PP", SEPTEMBER, now=NOW
            )
            assert detail is not None
            assert detail["source"] == "tracked_summary"
            assert [
                entry["season_day_number"] for entry in detail["daily_entries"]
            ] == [24, 25, 26, 27, 28]
            assert detail["season_end"] == OCTOBER_START.isoformat()
            # The current Season stays in the current log, not under Seasons.
            assert (
                api_players.get_player_season_summary(database, "#2PP", OCTOBER, now=NOW)
                is None
            )
            with database.pool.connection() as connection:
                stored = connection.execute(
                    "SELECT count(*) FROM player_season_summaries"
                ).fetchone()
                assert stored == (0,)
                materialize_player_season(connection, player_id, SEPTEMBER)
                connection.commit()

            # A stored summary replaces the read-time one; the Season is listed once.
            seasons = api_players.list_player_seasons(database, "#2PP", now=NOW)
            assert [s["official_season_id"] for s in seasons] == [SEPTEMBER]
            assert seasons[0]["published_at"] is not None
        finally:
            database.close()
