"""How often a past Reset may rebuild its publication after corrections.

Each correction generation rebuilds a Reset's whole leaderboard and army
records and freezes new manifests, so corrections to Resets before the
newest one wait and start together. A waiting correction stays queued, and
the worker's publication re-check starts it once the Reset may rebuild; no
correction is dropped. A correction already waiting for its inputs at the
quiet window starts no build until the window ends; builds already running
finish and publish. The newest swept Reset is live and never waits.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

# A past Reset starts at most one correction generation per interval,
# counted from its newest generation.
PAST_RESET_CORRECTION_INTERVAL = timedelta(hours=6)
# No past Reset starts a correction generation or its builds in this UTC
# window, which keeps the worker free for the live Reset's publication.
PAST_RESET_QUIET_START = time(4, 30)
PAST_RESET_QUIET_END = time(7, 0)


def _now(connection: Any) -> datetime:
    return connection.execute("SELECT clock_timestamp()").fetchone()[0]


def _latest_reset(connection: Any) -> datetime | None:
    return connection.execute(
        "SELECT max(boundary_at) FROM collector_reset_sweeps"
    ).fetchone()[0]


def _is_past_reset(connection: Any, boundary_at: datetime) -> bool:
    latest_reset = _latest_reset(connection)
    return latest_reset is not None and boundary_at < latest_reset


def _in_quiet_window(now: datetime) -> bool:
    return PAST_RESET_QUIET_START <= now.astimezone(UTC).time() < PAST_RESET_QUIET_END


def past_reset_build_waits(connection: Any, boundary_at: datetime) -> bool:
    """Whether a past Reset's correction build must wait out the quiet window."""
    return _is_past_reset(connection, boundary_at) and _in_quiet_window(
        _now(connection)
    )


def past_reset_build_hold(connection: Any) -> str | None:
    """In the quiet window, the newest Reset as build jobs write it; else None.

    The worker claims no correction build for a Reset before this one, so a
    build queued before the window starts after it, with no attempt spent.
    """
    if not _in_quiet_window(_now(connection)):
        return None
    latest_reset = _latest_reset(connection)
    if latest_reset is None:
        return None
    return latest_reset.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def past_reset_correction_waits(connection: Any, boundary_at: datetime) -> bool:
    """Whether a correction to this Reset must wait before it starts."""
    if not _is_past_reset(connection, boundary_at):
        return False
    now = _now(connection)
    if _in_quiet_window(now):
        return True
    last_generation_at = connection.execute(
        "SELECT max(created_at) FROM boundary_publication_generations WHERE boundary_at = %s",
        (boundary_at,),
    ).fetchone()[0]
    return (
        last_generation_at is not None
        and now - last_generation_at < PAST_RESET_CORRECTION_INTERVAL
    )
