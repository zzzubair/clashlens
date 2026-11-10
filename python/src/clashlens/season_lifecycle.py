"""Season locks, closure state, and canonical detail bounds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .db_rows import text_value as _text
from .domain import SEASON_ANCHOR_RULE_VERSION, SEASON_DURATION

SEASON_DETAIL_RETIRED = "season_detail_retired"
# Late battles, replays and corrections land for a week after the Season
# ends; finalization stops corrections and retirement deletes detail.
SEASON_CLOSE_WAIT = timedelta(days=7)


_RETIREMENT_GATE = "clashlens:season-retirement-global"


def acquire_retirement_reader(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        (_RETIREMENT_GATE,),
    )


def acquire_retirement_writer(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (_RETIREMENT_GATE,),
    )


def acquire_season_lock(connection: Any, season_id: str) -> None:
    """Exclusive transaction-scoped season lock held by retirement only.

    Finalization and retirement take this so no writer can be mid-flight
    while a season is fenced or its detail deleted.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"season-retirement:{season_id}",),
    )


def acquire_season_lock_shared(connection: Any, season_id: str) -> None:
    """Shared transaction-scoped season lock for detail writers.

    Writers hold this so finalization/retirement still fences them; they
    do not exclude each other. Writer-vs-writer conflicts stay on upsert
    keys, row locks, and finer-grained advisory locks.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
        (f"season-retirement:{season_id}",),
    )


def is_season_detail_retired(connection: Any, season_id: str) -> bool:
    """True once a season is finalized (writers fenced) or retired.

    Mutating callers hold a season lock first and keep it through their
    writes: shared for writers, exclusive for retirement.
    """
    return season_closure(connection, season_id) is not SeasonClosure.OPEN


def retired_day_ranges(connection: Any) -> list[tuple[Any, Any]]:
    """(start, end) bounds of fenced seasons for rolling-log filtering."""
    return [
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT season_start, season_end FROM season_detail_retirements
            WHERE status IN ('finalized', 'retired')
              AND season_start IS NOT NULL AND season_end IS NOT NULL
            """
        ).fetchall()
    ]


def is_detail_retired_for_day(connection: Any, day: datetime) -> bool:
    """True when a ranked-day start falls inside a fenced season."""
    for start, end in retired_day_ranges(connection):
        if start <= day.astimezone(UTC) < end.astimezone(UTC):
            return True
    return False


def filter_live_rows(rows: list[Any], day_of: Any, ranges: list[tuple[Any, Any]]) -> list[Any]:
    """Drop rows whose ranked day falls inside a fenced season.

    Rolling logs may carry retired battles: callers skip those rows but
    still process live-season content in the same observation.
    """
    if not ranges:
        return rows
    kept: list[Any] = []
    for row in rows:
        day = day_of(row)
        moment = day.astimezone(UTC) if day.tzinfo else day.replace(tzinfo=UTC)
        if not any(start <= moment < end for start, end in ranges):
            kept.append(row)
    return kept


def aware_utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("season close time must be timezone-aware")
    return now.astimezone(UTC)


def season_close_block(
    canonical: tuple[datetime, datetime] | None, stored: Any, now: datetime
) -> tuple[dict[str, str], str | None]:
    """Return the known eligible time and why a Season cannot close yet.

    ``canonical`` is the exact Season window from confirmed timing, or
    None when it cannot be established. ``stored`` is the window a record
    holds, which older code may have written early; it must match exactly,
    and a finalized record is not evidence that the wait passed. Closing
    waits until seven days after the Season end, equality included. This
    is the clock check only: every other check still applies.
    """
    now_utc = aware_utc(now)
    if canonical is None:
        return {}, "unknown_season_boundary"
    eligible_at = canonical[1].astimezone(UTC) + SEASON_CLOSE_WAIT
    eligible = {"eligible_at": eligible_at.isoformat()}
    if None in tuple(stored):
        return eligible, "unknown_season_boundary"
    if tuple(stored) != tuple(canonical):
        return eligible, "conflicting_season_boundary"
    if now_utc < eligible_at:
        return eligible, "season_close_wait"
    return eligible, None


def canonical_season_bounds(connection: Any, season_id: str) -> tuple[datetime, datetime] | None:
    """Return the exact 28-day window, never the observed log envelope.

    The confirmed anchor gives the current and previous Season; an older
    Season's canonical id is its start on the same 28-day calendar. A
    later, misaligned or noncanonical id is unknown.
    """
    from .domain import (
        SEASON_ANCHOR_RULE_VERSION,
        SEASON_DURATION,
        DomainRuleError,
        _canonical_season_start,
    )

    anchor = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        ORDER BY current_start DESC LIMIT 1
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if anchor is None:
        return None
    for anchored_id, start in ((anchor[0], anchor[2]), (anchor[1], anchor[3])):
        if _text(anchored_id) == season_id:
            return start, start + SEASON_DURATION
    try:
        start = _canonical_season_start(season_id)
    except DomainRuleError:
        return None
    if start >= anchor[3] or (anchor[3] - start) % SEASON_DURATION:
        return None
    return start, start + SEASON_DURATION


class SeasonClosure(StrEnum):
    """Where a Season's closure is, from its ``season_detail_retirements``
    record (``season_closure``).

    - ``open``: no record; the Season still takes corrections.
      ``finalize_season_detail`` saves the record once the Season has ended
      and seven days passed, its stored window agrees, its summaries match
      their days and ``close_blockers`` finds nothing. On this code it
      always finds ``promised_history``, so no Season leaves ``open``.
    - ``finalized``: the record is saved with its summary digests and fences
      writers; no detail is deleted yet.
    - ``retiring``: finalized, and ``retire_season_detail`` has deleted at
      least one batch (its ``progress`` saved). The record still says
      ``finalized``.
    - ``retired``: every eligible row is deleted; status ``retired``.

    Only those two commands move it, each under the Season's lock. Every
    retirement call rechecks the window, the wait, the stored digests and
    ``close_blockers``, so a blocker found later holds deletion. A batch
    interrupted before its progress is saved is counted again by the next
    call, so a restart only repeats a count.
    """

    OPEN = "open"
    FINALIZED = "finalized"
    RETIRING = "retiring"
    RETIRED = "retired"


def season_closure(connection: Any, season_id: str) -> SeasonClosure:
    """The Season's closure state. The caller checks the record table exists."""
    row = connection.execute(
        """
        SELECT status, progress ? 'last_deleted' FROM season_detail_retirements
        WHERE official_season_id = %s
        """,
        (season_id,),
    ).fetchone()
    if row is None:
        return SeasonClosure.OPEN
    status = row[0].decode() if isinstance(row[0], bytes) else row[0]
    if status == SeasonClosure.RETIRED:
        return SeasonClosure.RETIRED
    return SeasonClosure.RETIRING if row[1] else SeasonClosure.FINALIZED


def acquire_player_season_lock(
    connection: Any, player_id: int, season_id: str
) -> None:
    """Serialize writers for one player-season summary pair."""
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"player-season-summary:{int(player_id)}:{season_id}",),
    )


def season_completed(
    connection: Any, season_id: str, now: datetime
) -> tuple[bool, str]:
    """Decide whether a season is completed for explicit backfill.

    Authoritative season timing wins where available: a season matching
    the confirmed anchor's current id is completed once its exact 28 days
    have elapsed, and a season matching the previous id is completed
    because a newer season began. Noncanonical legacy ids fail closed
    unless a completed day-28 publication establishes the boundary.
    Returns (completed, reason) with reason empty when completed.
    """
    now_utc = now.astimezone(UTC)
    history = connection.execute(
        "SELECT 1 FROM api_player_daily_logs WHERE official_season_id = %s LIMIT 1",
        (season_id,),
    ).fetchone()
    if history is None:
        return False, "no_history"
    anchor = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id, current_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if anchor is not None:
        current_id = _text(anchor[0])
        previous_id = _text(anchor[1])
        if season_id == current_id and anchor[2] is not None:
            if anchor[2].astimezone(UTC) + SEASON_DURATION <= now_utc:
                return True, ""
            return False, "season_not_completed"
        if season_id == previous_id:
            return True, ""
    boundary = connection.execute(
        """
        SELECT 1 FROM api_player_daily_logs
        WHERE official_season_id = %s
          AND season_day_number = 28
          AND state = 'Complete'
          AND ranked_day_end IS NOT NULL
          AND ranked_day_end <= %s
        LIMIT 1
        """,
        (season_id, now_utc),
    ).fetchone()
    if boundary is not None:
        return True, ""
    return False, "season_not_completed"
