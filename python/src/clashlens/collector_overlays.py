"""Players shown on an open OBS overlay, checked first in the collector's
queue and 30 seconds apart instead of 90, so a battle reaches the stream
sooner. An overlay counts as open for 5 minutes after it last asked for its
settings (migration 0113)."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector_db import CollectorDatabase

OVERLAY_GAP = timedelta(seconds=30)
OPEN_FOR = timedelta(minutes=5)
# How often the collector reads the list again.
REFRESH_SECONDS = 10.0


def players_in_use(database: CollectorDatabase) -> list[tuple[int, str]]:
    """Each active player an open overlay shows: (player id, tag)."""
    with database._connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT player.id, player.normalized_tag
            FROM obs_overlays AS overlay
            JOIN players AS player ON player.id = overlay.player_id
            WHERE overlay.last_seen_at > clock_timestamp() - %s AND player.active
            """,
            (OPEN_FOR,),
        ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]
