"""Fetch official league history again once a Season's results are published.

League history gains the ended Season's row, with its official final
placement, minutes after the Season-opening Reset (about 05:13 UTC on
5 October 2026), after the Reset pair has already asked. So that Reset also
schedules one league-history-only request per frozen member, due 20 minutes
after it, on the ordinary lane and normal key budget. The
``refresh-league-history`` command schedules the same for every tracked
player, for a Season whose Reset fetch came too early. Each player has at
most one unfinished request per Season; once it finishes, running the
command again schedules another.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .domain import ranked_day_for

REFRESH_DELAY = timedelta(minutes=20)


def schedule_refresh(
    connection: Any,
    season_end: datetime,
    *,
    due_at: datetime,
    player_ids: list[int] | None = None,
) -> int:
    """Add one refresh per player for the Season ending at ``season_end``.

    Without ``player_ids`` every active player gets one. Returns how many
    were added; players with an unfinished one for this Season are skipped.
    """
    return connection.execute(
        """
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at,
            coalescing_key, profile_status, battle_log_status,
            league_history_status
        )
        SELECT 'league_history_refresh', 'ordinary', 'player', player.id,
               player.normalized_tag, %(due)s,
               'league-history-refresh:' || %(season)s || ':' || player.id,
               'not_applicable', 'not_applicable', 'pending'
        FROM players AS player
        WHERE CASE WHEN %(members)s::bigint[] IS NULL THEN player.active
                   ELSE player.id = ANY(%(members)s::bigint[]) END
        ON CONFLICT DO NOTHING
        """,
        {
            "due": due_at,
            "season": int(season_end.timestamp()),
            "members": player_ids,
        },
    ).rowcount


def add_command(
    subparsers: Any, database_argument: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Add the ``refresh-league-history`` command to the CLI."""
    command = subparsers.add_parser(
        "refresh-league-history",
        help="fetch league history again for every tracked player for the latest ended Season (collector database role)",
    )
    database_argument(command)


def run_command(database_url: str) -> int:
    import psycopg

    now = datetime.now(UTC)
    season_end = ranked_day_for(now).season_start
    with psycopg.connect(database_url) as connection:
        added = schedule_refresh(connection, season_end, due_at=now)
    print(
        json.dumps(
            {"season_end": season_end.isoformat(), "scheduled": added},
            sort_keys=True,
        )
    )
    return 0
