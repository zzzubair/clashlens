"""Compact historical player-season summaries (issue #82, first slice).

One independently readable ``player_season_summaries`` record per
(player, season) holds typed season totals plus up to 28 compact daily
trophy entries. Projection reads the latest published
``api_player_daily_logs`` version per day and joins only that log's exact
``ranked_day_version_id`` for starting trophies, end-of-day trophies
(``final_trophies_before_reset``), and reconciliation cross-checks. It never
independently picks a newer ranked-day version. Unknown stays NULL; no
battle IDs or battle evidence are stored.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .domain import SEASON_ANCHOR_RULE_VERSION

PROJECTION_VERSION = "player-season-summary-v1"
MAX_DAILY_ENTRIES = 28
_SEASON_DAYS = tuple(range(1, 29))
_SEASON_LENGTH_DAYS = 28

# Arbitrary source reason strings are normalized into a bounded
# representation so a valid source row can never fail the compact
# publication CHECK on flag size. Projection-owned literals are always
# kept; only free-form reasons are capped, with an explicit marker.
_REASON_CHARS = 64
_DAY_REASON_LIMIT = 8
_SEASON_REASON_LIMIT = 16
TRUNCATED_REASONS_FLAG = "truncated_reasons"
KNOWN_SEASON_FLAGS = frozenset(
    {
        "missing_days",
        "unknown_day_numbers",
        "partial_days",
        "too_many_days",
        "malformed_battle_entries",
        "ranked_version_missing",
        "detailed_boundaries_unavailable",
        "ranked_version_mismatch",
        "attack_star_total_mismatch",
        "defense_star_total_mismatch",
    }
)

_TOTAL_FIELDS = (
    "attack_count",
    "attack_gain",
    "attack_three_star_count",
    "defense_count",
    "defense_loss",
    "defense_three_star_count",
    "net_trophy_change",
)

_DAILY_COLUMNS = (
    "ranked_day_start",
    "ranked_day_end",
    "season_day_number",
    "version",
    "state",
    "coverage",
    "confidence",
    "attack_count",
    "attack_three_star_count",
    "attack_gain",
    "defense_count",
    "defense_three_star_count",
    "defense_loss",
    "net_trophy_change",
    "adjustments",
    "battles",
    "partial_reasons",
    "ranked_day_version_id",
)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    return int(value)


def _normalize_reasons(values: Any) -> tuple[list[str], bool]:
    """Bound arbitrary reason strings; returns (kept, overflowed)."""
    kept: list[str] = []
    overflowed = False
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str) or not value:
            continue
        short = value[:_REASON_CHARS]
        if short != value:
            overflowed = True
        if short in kept:
            continue
        if len(kept) >= _DAY_REASON_LIMIT:
            overflowed = True
            continue
        kept.append(short)
    return kept, overflowed


def _star_buckets(battles: Any) -> tuple[dict[str, int], bool]:
    """Count canonical included offense/defense events by stars.

    Each canonical ``(lens, battle identity)`` counts once; one offense
    plus one defense perspective survive for the same battle identity.
    Malformed or missing stars are unknown, never zero stars. Zero-star
    events keep their trophy asymmetry because trophies are never filtered.
    Excluded rows are ignored.
    """
    counts = {
        "attack": [0, 0, 0, 0],
        "attack_unknown": 0,
        "defense": [0, 0, 0, 0],
        "defense_unknown": 0,
    }
    malformed = not isinstance(battles, list)
    seen: set[tuple[str, str]] = set()
    for item in battles if isinstance(battles, list) else []:
        if not isinstance(item, dict):
            malformed = True
            continue
        if item.get("included") is False:
            continue
        lens = item.get("lens")
        if lens not in ("offense", "defense"):
            continue
        identity = item.get("battle_id", item.get("battle_identity"))
        if (
            ("battle_id" not in item and "battle_identity" not in item)
            or isinstance(identity, bool)
            or identity is None
            or str(identity).strip() == ""
        ):
            continue
        key = (lens, str(identity))
        if key in seen:
            continue
        seen.add(key)
        stars = item.get("stars")
        bucket = "attack" if lens == "offense" else "defense"
        if isinstance(stars, bool) or not isinstance(stars, int) or not 0 <= stars <= 3:
            counts[f"{bucket}_unknown"] += 1
        else:
            counts[bucket][stars] += 1
    return counts, malformed


def _adjustment_total(adjustments: Any) -> tuple[bool, int | None]:
    if not isinstance(adjustments, list) or not adjustments:
        return False, None
    total = 0
    seen = False
    for entry in adjustments:
        if isinstance(entry, dict) and isinstance(entry.get("amount"), int):
            total += int(entry["amount"])
            seen = True
    return True, total if seen else None


def _project(player_id: int, season_id: str, connection: Any) -> dict[str, Any] | None:
    # Migration 0011 (ranked_day_version_id) is guaranteed before 0019, so
    # the link column is always selected.
    query = (
        "SELECT "
        + ", ".join(_DAILY_COLUMNS)
        + """
        FROM (
            SELECT DISTINCT ON (ranked_day_start) *
            FROM api_player_daily_logs
            WHERE player_id = %s AND official_season_id = %s
            ORDER BY ranked_day_start, version DESC
        ) AS latest_days
        ORDER BY season_day_number NULLS LAST, ranked_day_start
        """
    )
    cursor = connection.execute(query, (player_id, season_id))
    rows = cursor.fetchall()
    if not rows:
        return None
    columns = [d.name for d in cursor.description]
    days = [dict(zip(columns, row)) for row in rows]

    ranked_by_id: dict[int, dict[str, Any]] = {}
    wanted = {int(day["ranked_day_version_id"]) for day in days if day.get("ranked_day_version_id") is not None}
    if wanted:
        version_rows = connection.execute(
            """
            SELECT id, start_trophies, final_trophies_before_reset,
                   attack_count, defense_count, attack_gain,
                   observed_defense_loss
            FROM ranked_day_versions
            WHERE id = ANY(%s::bigint[]) AND player_id = %s
            """,
            (sorted(wanted), player_id),
        ).fetchall()
        for version_row in version_rows:
            ranked_by_id[int(version_row[0])] = {
                "start_trophies": _int_or_none(version_row[1]),
                "end_trophies": _int_or_none(version_row[2]),
                "attack_count": _int_or_none(version_row[3]),
                "defense_count": _int_or_none(version_row[4]),
                "attack_gain": _int_or_none(version_row[5]),
                "defense_loss": _int_or_none(version_row[6]),
            }

    entries: list[dict[str, Any]] = []
    season_literals: set[str] = set()
    season_reasons: list[str] = []
    season_overflow = False
    totals: dict[str, int | None] = {field: 0 for field in _TOTAL_FIELDS}
    totals_complete = {field: True for field in _TOTAL_FIELDS}
    stars = {key: 0 for key in (
        "attack_star_0", "attack_star_1", "attack_star_2", "attack_star_3",
        "attack_star_unknown", "defense_star_0", "defense_star_1",
        "defense_star_2", "defense_star_3", "defense_star_unknown",
    )}
    observed_numbers: set[int] = set()
    numbers_known = True
    for day in days[:MAX_DAILY_ENTRIES]:
        number = _int_or_none(day.get("season_day_number"))
        if number is None:
            numbers_known = False
        else:
            observed_numbers.add(number)
        ranked = ranked_by_id.get(int(day["ranked_day_version_id"])) if day.get("ranked_day_version_id") is not None else None
        buckets, malformed = _star_buckets(day.get("battles"))
        for star in range(4):
            stars[f"attack_star_{star}"] += buckets["attack"][star]
            stars[f"defense_star_{star}"] += buckets["defense"][star]
        stars["attack_star_unknown"] += buckets["attack_unknown"]
        stars["defense_star_unknown"] += buckets["defense_unknown"]
        has_adjustment, adjustment_total = _adjustment_total(day.get("adjustments"))
        kept_reasons, reasons_overflow = _normalize_reasons(
            day.get("partial_reasons")
        )
        literals: list[str] = []
        if malformed:
            literals.append("malformed_battle_entries")
        if ranked is None and day.get("ranked_day_version_id") is not None:
            literals.append("ranked_version_missing")
        if ranked is None and day.get("ranked_day_version_id") is None:
            literals.append("detailed_boundaries_unavailable")
        if ranked is not None:
            for daily_field, ranked_field in (
                ("attack_count", "attack_count"),
                ("defense_count", "defense_count"),
                ("attack_gain", "attack_gain"),
                ("defense_loss", "defense_loss"),
            ):
                daily_value = _int_or_none(day.get(daily_field))
                ranked_value = ranked[ranked_field]
                if daily_value is not None and ranked_value is not None and daily_value != ranked_value:
                    literals.append("ranked_version_mismatch")
                    break
        for field in _TOTAL_FIELDS:
            value = _int_or_none(day.get(field))
            if value is None:
                totals_complete[field] = False
            elif totals[field] is not None:
                totals[field] = totals[field] + value  # type: ignore[operator]
        attack_events = sum(buckets["attack"]) + buckets["attack_unknown"]
        defense_events = sum(buckets["defense"]) + buckets["defense_unknown"]
        if _int_or_none(day.get("attack_count")) is not None and attack_events != day["attack_count"]:
            literals.append("attack_star_total_mismatch")
        if _int_or_none(day.get("defense_count")) is not None and defense_events != day["defense_count"]:
            literals.append("defense_star_total_mismatch")
        flags = sorted(
            set(literals)
            | set(kept_reasons)
            | ({TRUNCATED_REASONS_FLAG} if reasons_overflow else set())
        )
        season_literals.update(literals)
        for reason in kept_reasons:
            if reason not in season_reasons:
                season_reasons.append(reason)
        if reasons_overflow:
            season_overflow = True
        entries.append(
            {
                "season_day_number": number,
                "ranked_day_start": day["ranked_day_start"].astimezone(UTC).isoformat(),
                "ranked_day_end": (
                    None
                    if day["ranked_day_end"] is None
                    else day["ranked_day_end"].astimezone(UTC).isoformat()
                ),
                "start_trophies": ranked["start_trophies"] if ranked else None,
                "end_trophies": ranked["end_trophies"] if ranked else None,
                "attack_gain": _int_or_none(day.get("attack_gain")),
                "defense_loss": _int_or_none(day.get("defense_loss")),
                "net_change": _int_or_none(day.get("net_trophy_change")),
                "attack_count": _int_or_none(day.get("attack_count")),
                "defense_count": _int_or_none(day.get("defense_count")),
                "attack_three_star_count": _int_or_none(day.get("attack_three_star_count")),
                "defense_three_star_count": _int_or_none(day.get("defense_three_star_count")),
                "state": _text(day["state"]),
                "coverage": _text(day["coverage"]),
                "confidence": None if day.get("confidence") is None else _text(day["confidence"]),
                "has_adjustment": has_adjustment,
                "adjustment_total": adjustment_total,
                "flags": sorted(set(flags)),
            }
        )
    if len(days) > MAX_DAILY_ENTRIES:
        season_literals.add("too_many_days")

    missing = sorted(set(_SEASON_DAYS) - observed_numbers) if numbers_known else []
    if numbers_known and missing:
        season_literals.add("missing_days")
    if not numbers_known:
        season_literals.add("unknown_day_numbers")
    if any(day["state"] != "Complete" or day["coverage"] != "complete" for day in days):
        season_literals.add("partial_days")

    by_number = {entry["season_day_number"]: entry for entry in entries if entry["season_day_number"] is not None}
    first = by_number.get(1)
    last = by_number.get(28)
    start_trophies = first["start_trophies"] if first else None
    end_trophies = last["end_trophies"] if last else None
    season_start = first["ranked_day_start"] if first else None
    season_end = last["ranked_day_end"] if last else None

    final_reasons = season_reasons[:_SEASON_REASON_LIMIT]
    if len(season_reasons) > _SEASON_REASON_LIMIT:
        season_overflow = True
    unresolved = sorted(
        season_literals
        | set(final_reasons)
        | ({TRUNCATED_REASONS_FLAG} if season_overflow else set())
    )
    # Complete requires 28 valid complete-covered days, no missing day
    # numbers, no unresolved flags or reasons, and known season
    # boundary trophies. Unknown stays NULL and forces partial.
    complete = (
        numbers_known
        and len(days) == MAX_DAILY_ENTRIES
        and not missing
        and all(day["state"] == "Complete" and day["coverage"] == "complete" for day in days)
        and not unresolved
        and start_trophies is not None
        and end_trophies is not None
        and season_start is not None
        and season_end is not None
    )
    final_rank = _season_final_rank(connection, player_id, last)
    resolved_totals = {
        field: (totals[field] if totals_complete[field] else None) for field in _TOTAL_FIELDS
    }
    return {
        "player_id": player_id,
        "official_season_id": season_id,
        "season_start": season_start,
        "season_end": season_end,
        "start_trophies": start_trophies,
        "end_trophies": end_trophies,
        "final_rank": final_rank,
        **resolved_totals,
        **stars,
        "days_observed": len(days[:MAX_DAILY_ENTRIES]),
        "days_missing": len(missing),
        "missing_days": missing,
        "coverage_state": "complete" if complete else "partial",
        "unresolved_flags": unresolved,
        "daily_entries": entries,
        "projection_version": PROJECTION_VERSION,
    }


def _season_final_rank(
    connection: Any, player_id: int, last: dict[str, Any] | None
) -> int | None:
    """Final rank only from official season-final evidence.

    The day-28 ranked-day end must exist and match a frozen leaderboard
    boundary. The newest board for that boundary wins: when it omits the
    player or carries a NULL rank, the rank stays NULL instead of falling
    back to an older board or inventing a value.
    """
    if last is None or not last.get("ranked_day_end"):
        return None
    board = connection.execute(
        """
        SELECT id FROM api_frozen_leaderboards
        WHERE boundary_at = %s
        ORDER BY version DESC
        LIMIT 1
        """,
        (last["ranked_day_end"],),
    ).fetchone()
    if board is None:
        return None
    row = connection.execute(
        """
        SELECT official_rank FROM api_frozen_leaderboard_entries
        WHERE leaderboard_id = %s AND player_id = %s
        """,
        (board[0], player_id),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


_SUMMARY_COLUMNS = (
    "player_id", "official_season_id", "season_start", "season_end",
    "start_trophies", "end_trophies", "final_rank", *_TOTAL_FIELDS,
    "attack_star_0", "attack_star_1", "attack_star_2", "attack_star_3",
    "attack_star_unknown", "defense_star_0", "defense_star_1",
    "defense_star_2", "defense_star_3", "defense_star_unknown",
    "days_observed", "days_missing", "missing_days", "coverage_state",
    "unresolved_flags", "daily_entries", "projection_version",
    "content_digest",
)


def _digest(summary: dict[str, Any]) -> str:
    canonical = {key: summary[key] for key in sorted(summary) if key != "published_at"}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def materialize_player_season(
    connection: Any, player_id: int, season_id: str
) -> dict[str, Any]:
    """Project and atomically store one player-season summary.

    Competing projections for the same player-season serialize on a
    transaction-scoped advisory lock. Unchanged input is a no-op that leaves
    the existing row (including published_at) untouched; a transaction
    failure preserves the prior summary.
    """
    if not season_id or len(season_id) > 128:
        raise ValueError("official season id is outside the supported range")
    from .season_retirement import (
        SEASON_DETAIL_RETIRED,
        acquire_season_lock,
        is_season_detail_retired,
    )

    acquire_season_lock(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        existing_digest = connection.execute(
            """
            SELECT content_digest FROM player_season_summaries
            WHERE player_id = %s AND official_season_id = %s
            """,
            (int(player_id), season_id),
        ).fetchone()
        return {
            "status": SEASON_DETAIL_RETIRED,
            "content_digest": (
                existing_digest[0].decode("utf-8")
                if isinstance(existing_digest[0], bytes)
                else str(existing_digest[0])
            )
            if existing_digest is not None
            else None,
        }
    projected = _project(int(player_id), season_id, connection)
    if projected is None:
        return {"status": "missing", "content_digest": None}
    digest = _digest(projected)
    existing = connection.execute(
        """
        SELECT content_digest FROM player_season_summaries
        WHERE player_id = %s AND official_season_id = %s
        FOR UPDATE
        """,
        (int(player_id), season_id),
    ).fetchone()
    if existing is not None and _text(existing[0]) == digest:
        return {"status": "unchanged", "content_digest": digest}
    from psycopg.types.json import Jsonb

    values = [
        projected["player_id"],
        projected["official_season_id"],
        projected["season_start"],
        projected["season_end"],
        projected["start_trophies"],
        projected["end_trophies"],
        projected["final_rank"],
        *(projected[field] for field in _TOTAL_FIELDS),
        projected["attack_star_0"],
        projected["attack_star_1"],
        projected["attack_star_2"],
        projected["attack_star_3"],
        projected["attack_star_unknown"],
        projected["defense_star_0"],
        projected["defense_star_1"],
        projected["defense_star_2"],
        projected["defense_star_3"],
        projected["defense_star_unknown"],
        projected["days_observed"],
        projected["days_missing"],
        projected["missing_days"],
        projected["coverage_state"],
        projected["unresolved_flags"],
        Jsonb(projected["daily_entries"]),
        projected["projection_version"],
        digest,
    ]
    assignments = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _SUMMARY_COLUMNS
        if column not in ("player_id", "official_season_id")
    )
    connection.execute(
        f"""
        INSERT INTO player_season_summaries ({", ".join(_SUMMARY_COLUMNS)})
        VALUES ({", ".join(["%s"] * len(values))})
        ON CONFLICT (player_id, official_season_id) DO UPDATE
        SET {assignments}, published_at = clock_timestamp()
        """,
        values,
    )
    return {"status": "published", "content_digest": digest}


def _season_completed(
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
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if anchor is not None:
        current_id = _text(anchor[0])
        previous_id = _text(anchor[1])
        if season_id == current_id and anchor[2] is not None:
            if anchor[2].astimezone(UTC) + timedelta(days=_SEASON_LENGTH_DAYS) <= now_utc:
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


def materialize_completed_seasons(
    connection: Any,
    *,
    season_id: str,
    max_players: int,
    now: datetime,
    after_player_id: int = 0,
) -> dict[str, Any]:
    """Bounded backfill of one completed season.

    Canonical completed seasons materialize even when day 28 is missing;
    noncanonical seasons require a completed day-28 boundary. A live or
    otherwise active season is left untouched. Source detail is retained.
    Each player materializes in a savepoint so one bad player cannot
    abort the bounded batch.
    """
    if not season_id or len(season_id) > 128:
        raise ValueError("official season id is outside the supported range")
    if not isinstance(max_players, bool) and isinstance(max_players, int):
        bounded = 1 <= max_players <= 1000
    else:
        bounded = False
    if not bounded:
        raise ValueError("backfill batch size is outside the supported range")
    if (
        isinstance(after_player_id, bool)
        or not isinstance(after_player_id, int)
        or after_player_id < 0
    ):
        raise ValueError("backfill cursor is outside the supported range")
    from .season_retirement import (
        SEASON_DETAIL_RETIRED,
        acquire_season_lock,
        is_season_detail_retired,
    )

    acquire_season_lock(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        return {
            "season_id": season_id,
            "season_completed": False,
            "reason": SEASON_DETAIL_RETIRED,
            "candidates": 0,
            "materialized": 0,
            "unchanged": 0,
            "failures": [],
            "next_after_player_id": None,
        }
    completed, reason = _season_completed(connection, season_id, now)
    if not completed:
        return {
            "season_id": season_id,
            "season_completed": False,
            "reason": reason,
            "candidates": 0,
            "materialized": 0,
            "unchanged": 0,
            "failures": [],
            "next_after_player_id": None,
        }
    candidates = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT player_id FROM api_player_daily_logs
            WHERE official_season_id = %s AND player_id > %s
            ORDER BY player_id
            LIMIT %s
            """,
            (season_id, after_player_id, max_players),
        ).fetchall()
    ]
    report: dict[str, Any] = {
        "season_id": season_id,
        "season_completed": True,
        "candidates": len(candidates),
        "materialized": 0,
        "unchanged": 0,
        "failures": [],
        "next_after_player_id": max(candidates) if candidates else None,
    }
    for player_id in candidates:
        try:
            with connection.transaction():
                outcome = materialize_player_season(connection, player_id, season_id)
        except Exception as error:  # noqa: BLE001 - reported per player, batch continues
            report["failures"].append({"player_id": player_id, "error": str(error)[:200]})
            continue
        if outcome["status"] == "published":
            report["materialized"] += 1
        elif outcome["status"] == "unchanged":
            report["unchanged"] += 1
    return report
