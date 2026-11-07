"""When population builds may start.

However many worker processes run, one population build runs at a time:
its transaction holds a permit lock, and a build claimed but not yet started
holds a live lease, so claims skip builds while either is true.

Corrections are also paced in how often a Reset rebuilds (``CorrectionPace``).

Each correction generation rebuilds a Reset's whole leaderboard and army
records and freezes new manifests, so corrections arriving between rebuilds
wait and start together. A waiting correction stays queued, and the
worker's publication re-check starts it once the Reset may rebuild; no
correction is dropped. The rule, counted from the Reset's newest generation
(its first board, before any rebuild):

- the newest swept Reset rebuilds at most once an hour (owner decision,
  12 Oct 2026, confirmed 6 Nov 2026), except at once for a correction that
  adds a member published without a day result whose day has saved since:
  that member appears in the board within seconds. The quiet window does
  not apply to it.
- a Reset older than the newest rebuilds at most once every 6 hours, and
  never starts a rebuild from 04:30 to 07:00 UTC.
- a correction the Season repair's boards step queued or found waiting
  (``REPAIR_CORRECTION``) waits for neither interval, on any Reset, outside
  the quiet window: the repair otherwise waited up to 6 hours a board (owner
  decision, 14 Oct 2026). It changes nothing else: in the quiet window, and
  for the operator's own window, it waits as any other.

No build for a past Reset starts in the quiet window, so one not yet started
waits until it ends; builds already running finish and publish.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from typing import Any

from psycopg.types.json import Jsonb

# A past Reset starts at most one correction generation per interval,
# counted from its newest generation.
PAST_RESET_CORRECTION_INTERVAL = timedelta(hours=6)
NEWEST_RESET_CORRECTION_INTERVAL = timedelta(hours=1)
# The pending input of a member published without a day result, whose day
# saved after publication.
ARRIVAL = {"arrival": True}
# No past Reset starts a correction generation or its builds in this UTC
# window, which keeps the worker free for the live Reset's publication.
PAST_RESET_QUIET_START = time(4, 30)
PAST_RESET_QUIET_END = time(7, 0)
# An operator correction (boundary.queue_army_corrections), marked by this
# pending input, neither starts nor has its leaderboard, statistics or army
# build claimed in this wider UTC window, for any Reset; those builds run at
# background priority.
OPERATOR_CORRECTION = {"kind": "operator"}
OPERATOR_QUIET_START = time(4, 0)
# A correction the Season repair's boards step queued or found waiting
# (boundary.queue_board_rebuilds with ``repair``), marked by this input.
REPAIR_CORRECTION = {"kind": "repair"}


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


def operator_generation(connection: Any, boundary_at: datetime, generation: Any) -> bool:
    """Whether this Reset generation is an operator correction's rebuild."""
    return connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM boundary_publication_corrections AS correction
            JOIN boundary_publication_generations AS rebuilt
              ON rebuilt.id = correction.generation_id
            WHERE rebuilt.boundary_at = %s AND rebuilt.generation = %s
              AND clashlens_correction_inputs(correction) @> %s::jsonb
        )
        """,
        (boundary_at, generation, Jsonb([OPERATOR_CORRECTION])),
    ).fetchone()[0]


def operator_correction_waits(connection: Any) -> bool:
    """Whether an operator correction, or its builds, must wait now."""
    now = _now(connection).astimezone(UTC).time()
    return OPERATOR_QUIET_START <= now < PAST_RESET_QUIET_END


def past_reset_build_waits(connection: Any, boundary_at: datetime) -> bool:
    """Whether a past Reset's build must wait out the quiet window."""
    return _is_past_reset(connection, boundary_at) and _in_quiet_window(
        _now(connection)
    )


def past_reset_build_hold(connection: Any) -> str | None:
    """In the quiet window, the newest Reset as build jobs write it; else None.

    The worker claims no build for a Reset before this one, so a
    build queued before the window starts after it, with no attempt spent.
    """
    if not _in_quiet_window(_now(connection)):
        return None
    latest_reset = _latest_reset(connection)
    if latest_reset is None:
        return None
    return latest_reset.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CorrectionPace(StrEnum):
    """How soon a correction to a Reset may start, by the rule above.

    QUIET_WINDOW: a past Reset from 04:30 to 07:00 UTC: waits until 07:00.
    EVERY_SIX_HOURS: a past Reset: 6 hours after its newest generation.
    HOURLY: the newest Reset: an hour after its newest generation.
    AT_ONCE: the newest Reset, when this change or a waiting correction adds
    a member published without a day result whose day has saved since; or,
    outside the quiet window, any Reset with a waiting Season repair
    correction (``REPAIR_CORRECTION``).
    """

    QUIET_WINDOW = "quiet_window"
    EVERY_SIX_HOURS = "every_six_hours"
    HOURLY = "hourly"
    AT_ONCE = "at_once"


def correction_pace(
    connection: Any, boundary_at: datetime, *, latest_reset: datetime, now: datetime,
    arrival: bool,
) -> CorrectionPace:
    """The pace of a correction to this Reset at ``now``; ``arrival`` when
    the change waiting to start is one."""
    past = boundary_at < latest_reset
    quiet = _in_quiet_window(now)
    if past and quiet:
        return CorrectionPace.QUIET_WINDOW
    if arrival and not past:
        return CorrectionPace.AT_ONCE
    # Waiting inputs that skip the interval: on the newest Reset an arrival,
    # and outside the quiet window a Season repair's correction.
    markers = ([] if past else [ARRIVAL]) + ([] if quiet else [REPAIR_CORRECTION])
    if markers and connection.execute(
        """
        SELECT EXISTS (
            SELECT FROM boundary_publication_corrections AS correction
            WHERE boundary_at = %s AND state IN ('queued', 'pending_inputs')
              AND clashlens_correction_inputs(correction) @> ANY(%s::jsonb[]))
        """,
        (boundary_at, [Jsonb([marker]) for marker in markers]),
    ).fetchone()[0]:
        return CorrectionPace.AT_ONCE
    return CorrectionPace.EVERY_SIX_HOURS if past else CorrectionPace.HOURLY


def correction_waits(connection: Any, boundary_at: datetime, arrival: bool = False) -> bool:
    """Whether a correction to this Reset must wait before it starts;
    ``arrival`` when the change waiting to start is one."""
    latest_reset = _latest_reset(connection)
    if latest_reset is None:
        return False
    now = _now(connection)
    pace = correction_pace(
        connection, boundary_at, latest_reset=latest_reset, now=now, arrival=arrival
    )
    if pace is CorrectionPace.QUIET_WINDOW:
        return True
    if pace is CorrectionPace.AT_ONCE:
        return False
    interval = (
        NEWEST_RESET_CORRECTION_INTERVAL
        if pace is CorrectionPace.HOURLY
        else PAST_RESET_CORRECTION_INTERVAL
    )
    last_generation_at = connection.execute(
        "SELECT max(created_at) FROM boundary_publication_generations WHERE boundary_at = %s",
        (boundary_at,),
    ).fetchone()[0]
    return last_generation_at is not None and now - last_generation_at < interval


BUILD_PERMIT_KEY = "population-build-permit"


def take_build_permit(connection: Any) -> None:
    """Hold the one-build permit until this build's transaction ends."""
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (BUILD_PERMIT_KEY,)
    )


def build_permit_busy(
    connection: Any, jobs_relation: str, build_work_types: Collection[str]
) -> bool:
    """Whether a build runs or waits to start; else this claim holds the permit.

    The lease check is its own statement, so it reads every build claim
    committed before this one took the permit.
    """
    if not connection.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (BUILD_PERMIT_KEY,)
    ).fetchone()[0]:
        return True
    return connection.execute(
        f"""
        SELECT EXISTS (
            SELECT FROM {jobs_relation}
            WHERE state = 'leased' AND lease_expires_at > clock_timestamp()
              AND work_type = ANY(%s::text[]))
        """,
        (sorted(build_work_types),),
    ).fetchone()[0]
