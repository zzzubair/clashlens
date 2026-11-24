"""Shared setup for league week days postgres tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg
from domain_test_support import text
from reset_scenarios_postgres_support import (
    SEASON,
    Player,
)

from clashlens import league_days
from clashlens.db import Database
from clashlens.league_week import Below, Claim, League, ProofKind


@dataclass
class Climber(Player):
    """A player whose profile can show Legend II while naming the Season:
    the game's accepted answer for a Legend II player. ``dropped`` still
    shows Legend II naming Season 0."""

    legend_ii: bool = False

    def profile(self, trophies: int, season: datetime | None) -> bytes:
        payload = json.loads(super().profile(trophies, season))
        if self.legend_ii:
            payload["leagueTier"] = {"id": 105000035, "name": "Legend II"}
        return json.dumps(payload).encode()


def _public_days(connection_info: str, tag: str) -> dict[datetime, dict[str, object]]:
    """Each Legend day the player's page shows: the newest public row of
    each day, its state, unfinished reasons, attacks and defenses."""
    with psycopg.connect(connection_info) as connection:
        return {
            start: {"state": text(state), "reasons": list(reasons),
                    "attacks": attack_count, "defenses": defense_count}
            for start, state, reasons, attack_count, defense_count in connection.execute(
                """
                SELECT DISTINCT ON (daily.ranked_day_start)
                       daily.ranked_day_start, daily.state, daily.partial_reasons,
                       daily.attack_count, daily.defense_count
                FROM api_player_daily_logs AS daily
                JOIN players AS player ON player.id = daily.player_id
                WHERE player.normalized_tag = %s
                ORDER BY daily.ranked_day_start, daily.version DESC
                """,
                (tag,),
            ).fetchall()
        }


def _decide_below(connection_info: str, tags: tuple[str, ...], now: datetime) -> None:
    """Decide each player's week of 5 October below Legend I."""
    database = Database(connection_info)
    try:
        with database.pool.connection() as connection, connection.transaction():
            for tag in tags:
                league_days.save_season(
                    database, connection, _player_id(connection, tag), SEASON,
                    {SEASON: Below(Claim(SEASON - timedelta(days=7), None,
                                         ProofKind.PREVIOUS_WEEK, League.BELOW))},
                    now, queue_days=False)
    finally:
        database.close()


def _player_id(connection: psycopg.Connection, tag: str) -> int:
    return connection.execute(
        "SELECT id FROM players WHERE normalized_tag = %s", (tag,)).fetchone()[0]
