"""How often a past Reset may rebuild its publication after corrections.

Each correction generation rebuilds a Reset's whole leaderboard and army
records and freezes new manifests, so corrections to Resets before the
newest one wait and start together. A waiting correction stays queued, and
the worker's publication re-check starts it once the Reset may rebuild; no
correction is dropped. The newest swept Reset is live and never waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

# A past Reset starts at most one correction generation per interval,
# counted from its newest generation.
PAST_RESET_CORRECTION_INTERVAL = timedelta(hours=6)
# No past Reset starts a correction generation in this UTC window, which
# keeps the worker free for the live Reset's publication.
PAST_RESET_QUIET_START = time(4, 30)
PAST_RESET_QUIET_END = time(7, 0)


def _now(connection: Any) -> datetime:
    return connection.execute("SELECT clock_timestamp()").fetchone()[0]


def past_reset_correction_waits(connection: Any, boundary_at: datetime) -> bool:
    """Whether a correction to this Reset must wait before it starts."""
    latest_reset, last_generation_at = connection.execute(
        """
        SELECT (SELECT max(boundary_at) FROM collector_reset_sweeps),
               (SELECT max(created_at) FROM boundary_publication_generations
                WHERE boundary_at = %s)
        """,
        (boundary_at,),
    ).fetchone()
    if latest_reset is None or boundary_at >= latest_reset:
        return False
    now = _now(connection).astimezone(UTC)
    if PAST_RESET_QUIET_START <= now.time() < PAST_RESET_QUIET_END:
        return True
    return (
        last_generation_at is not None
        and now - last_generation_at < PAST_RESET_CORRECTION_INTERVAL
    )
