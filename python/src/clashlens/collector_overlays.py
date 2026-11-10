"""Players shown on an open OBS overlay, checked first in the collector's
queue and 30 seconds apart instead of 90, so a battle reaches the stream
sooner. An overlay counts as open for 5 minutes after it last asked for its
settings (migration 0113)."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .collector_db import CollectorDatabase

OVERLAY_GAP = timedelta(seconds=30)
OPEN_FOR = timedelta(minutes=5)
# A read through the link writes the time it was seen at most this often.
MARK_EVERY = timedelta(minutes=1)
# How often the collector reads the list again.
REFRESH_SECONDS = 10.0


class ViewerPresence(StrEnum):
    """Whether an overlay in OBS is watching its player, from the
    obs_overlays row's player_id and last_seen_at alone (`PRESENCE_SQL`).

    CLOSED     no player chosen, never read through its current link, or
               last read OPEN_FOR or longer ago
    OPEN       read within OPEN_FOR, MARK_EVERY or longer ago: the next
               read writes the time again
    JUST_SEEN  read within MARK_EVERY: open, and the next read writes nothing

    Events: OBS reading the link (api_overlays.read_link) moves CLOSED or
    OPEN to JUST_SEEN while a player is chosen; the clock alone moves
    JUST_SEEN to OPEN to CLOSED, writing nothing; a new link
    (api_overlays.new_link) clears last_seen_at, so CLOSED until OBS reads
    the new one; choosing no player is CLOSED at once. While OPEN or
    JUST_SEEN, the collector checks the player first, OVERLAY_GAP apart
    (`players_in_use`).

    Owner: the database row, on the database clock. Nothing else holds it,
    so a failed read writes nothing and a restart of the website, data
    service or collector loses nothing.
    """

    CLOSED = "closed"
    OPEN = "open"
    JUST_SEEN = "just_seen"


def _interval(gap: timedelta) -> str:
    return f"interval '{int(gap.total_seconds())} seconds'"


# The presence of the obs_overlays row named `overlay`: the one place the
# rule lives, read by the collector and by each read through the link.
PRESENCE_SQL = f"""(CASE
    WHEN overlay.player_id IS NULL OR overlay.last_seen_at IS NULL
         OR overlay.last_seen_at <= clock_timestamp() - {_interval(OPEN_FOR)}
        THEN '{ViewerPresence.CLOSED}'
    WHEN overlay.last_seen_at < clock_timestamp() - {_interval(MARK_EVERY)}
        THEN '{ViewerPresence.OPEN}'
    ELSE '{ViewerPresence.JUST_SEEN}' END)"""


def players_in_use(database: CollectorDatabase) -> list[tuple[int, str]]:
    """Each active player an open overlay shows: (player id, tag)."""
    with database.connection() as connection:
        rows = connection.execute(
            f"""
            SELECT DISTINCT player.id, player.normalized_tag
            FROM obs_overlays AS overlay
            JOIN players AS player ON player.id = overlay.player_id
            WHERE {PRESENCE_SQL} <> '{ViewerPresence.CLOSED}' AND player.active
            """
        ).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]
