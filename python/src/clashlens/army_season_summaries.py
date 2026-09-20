"""Whole-season usage and outcomes by unit ID and quantity."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from .army_history import HISTORY_CATEGORIES, aggregate_usage

# The completed-season gate is shared with the player summaries so both
# slices agree on when a season is finalizable.
from .season_summaries import _season_completed

PROJECTION_VERSION = "army-unit-usage-v3"
LENSES = ("offense", "defense")
# Historical army reads cover the whole-season sample, never a
# population-filtered cohort; per-cohort historical filters are out of scope.
HISTORICAL_POPULATION = "all"
_SEASON_DAYS = tuple(range(1, 29))

_SUMMARY_COLUMNS = (
    "official_season_id",
    "lens",
    "category",
    "days_observed",
    "days_missing",
    "missing_days",
    "coverage_state",
    "total_attacks",
    "usable_army_sample",
    "army_states",
    "unknown_affected_attacks",
    "unknown_component_occurrences",
    "perspective_disagreement_count",
    "missing_trophy_membership_evidence",
    "result_rows",
    "unit_usage",
    "projection_version",
    "content_digest",
)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _check_season_lens(season_id: str, lens: str) -> None:
    # Same 80-char contract as ArmyAnalyticsSelection.parse: the API path
    # already enforces it, so materialization rejects anything longer here.
    if not season_id or len(season_id) > 80:
        raise ValueError("official season id is outside the supported range")
    if lens not in LENSES:
        raise ValueError("unsupported army summary lens")


def acquire_army_season_lock(connection: Any, season_id: str, lens: str) -> None:
    """Serialize whole-season materialization for one lens.

    The shared retirement season lock only fences retirement; same-lens
    writers serialize on this finer key so a refresh always projects from
    the last committed build.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"army-season-summary:{season_id}:{lens}",),
    )


def _project_lens(connection: Any, season_id: str, lens: str) -> dict[str, Any]:
    """Aggregate one season-lens across all categories from current facts.

    Facts are keyed one-current-row per (battle_id, lens), so each lens
    counts its own attacks exactly once.
    """
    rows = connection.execute(
        """
        SELECT stars, army_state, home_troops,
               spells, siege, heroes, unresolved_components,
               perspective_disagreement
        FROM army_analytics_battle_facts
        WHERE official_season_id = %s AND lens = %s AND is_current
        ORDER BY battle_id
        """,
        (season_id, lens),
    ).fetchall()
    facts = [
        {
            "stars": int(row[0]),
            "army_state": _text(row[1]),
            "home_troops": row[2] or [],
            "spells": row[3] or [],
            "siege": row[4] or [],
            "heroes": row[5] or [],
            "unresolved_components": row[6] or [],
            "perspective_disagreement": bool(row[7]),
        }
        for row in rows
    ]
    observed = {
        int(row[0])
        for row in connection.execute(
            """
            SELECT season_day_number FROM army_analytics_completed_days
            WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchall()
    }
    missing = sorted(set(_SEASON_DAYS) - observed)
    coverage_state = "complete" if len(observed) == 28 and not missing else "partial"
    summaries: dict[str, Any] = {}
    usage = aggregate_usage(facts)
    states = Counter(fact["army_state"] for fact in facts)
    army_states = {
        "fully_decoded": states.pop("decoded", 0),
        "partial": states.pop("partial", 0),
        "missing_code": states.pop("missing_army_share_code", 0),
        "empty_code": states.pop("empty_army_share_code", 0),
        "malformed": states.pop("malformed", 0),
        "structurally_unsupported": states.pop("structurally_unsupported", 0),
        **dict(sorted(states.items())),
    }
    for category in sorted(HISTORY_CATEGORIES):
        summaries[category] = {
            "days_observed": len(observed),
            "days_missing": len(missing),
            "missing_days": missing,
            "coverage_state": coverage_state,
            "total_attacks": len(facts),
            "usable_army_sample": army_states["fully_decoded"] + army_states["partial"],
            "army_states": army_states,
            "unknown_affected_attacks": sum(bool(f["unresolved_components"]) for f in facts),
            "unknown_component_occurrences": sum(len(f["unresolved_components"]) for f in facts),
            "perspective_disagreement_count": sum(f["perspective_disagreement"] for f in facts),
            "missing_trophy_membership_evidence": 0,
            "result_rows": [],
            "unit_usage": usage[category],
            "projection_version": PROJECTION_VERSION,
        }
    return summaries


def _digest(summary: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def materialize_army_season(
    connection: Any, season_id: str, lens: str
) -> dict[str, Any]:
    """Project and atomically store one season-lens across all categories.

    Competing writers for the same season-lens serialize on the
    transaction-scoped advisory lock from acquire_army_season_lock; the
    shared season lock only fences retirement, so the two lenses of one
    season materialize concurrently. All categories publish together:
    any failure raises and aborts the whole lens, so the prior complete
    lens stays readable and a lens is never published partially.
    Unchanged categories are a no-op that leaves the existing rows
    (including published_at) untouched. Offense and defense are
    independent lenses; callers isolate them (see
    materialize_completed_army_season) so one bad lens cannot abort the
    other.
    """
    _check_season_lens(season_id, lens)
    from .season_retirement import (
        SEASON_DETAIL_RETIRED,
        acquire_season_lock_shared,
        is_season_detail_retired,
    )

    acquire_season_lock_shared(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        return {
            "season_id": season_id,
            "lens": lens,
            "status": SEASON_DETAIL_RETIRED,
            "materialized": 0,
            "unchanged": 0,
            "failures": [],
            "content_digests": {},
        }
    acquire_army_season_lock(connection, season_id, lens)
    projected = _project_lens(connection, season_id, lens)
    connection.execute(
        "DELETE FROM army_season_summaries WHERE official_season_id = %s "
        "AND lens = %s AND NOT (category = ANY(%s))",
        (season_id, lens, sorted(HISTORY_CATEGORIES)),
    )
    report: dict[str, Any] = {
        "season_id": season_id,
        "lens": lens,
        "materialized": 0,
        "unchanged": 0,
        "failures": [],
        "content_digests": {},
    }
    for category in sorted(projected):
        summary = projected[category]
        digest = _digest(summary)
        # No per-category savepoint: a single failed upsert must abort the
        # enclosing transaction so a lens never publishes partially. The
        # caller decides whether that aborts just this lens (backfill) or
        # is isolated from day facts (late-correction refresh).
        unchanged = _upsert_category(
            connection, season_id, lens, category, summary, digest
        )
        report["unchanged" if unchanged else "materialized"] += 1
        report["content_digests"][category] = digest
    return report


def _upsert_category(
    connection: Any,
    season_id: str,
    lens: str,
    category: str,
    summary: dict[str, Any],
    digest: str,
) -> bool:
    """Insert or refresh one category row; returns True when unchanged."""
    existing = connection.execute(
        """
        SELECT content_digest FROM army_season_summaries
        WHERE official_season_id = %s AND lens = %s AND category = %s
        FOR UPDATE
        """,
        (season_id, lens, category),
    ).fetchone()
    if existing is not None and _text(existing[0]) == digest:
        return True
    values = [
        season_id,
        lens,
        category,
        summary["days_observed"],
        summary["days_missing"],
        summary["missing_days"],
        summary["coverage_state"],
        summary["total_attacks"],
        summary["usable_army_sample"],
        Jsonb(summary["army_states"]),
        summary["unknown_affected_attacks"],
        summary["unknown_component_occurrences"],
        summary["perspective_disagreement_count"],
        summary["missing_trophy_membership_evidence"],
        Jsonb(summary["result_rows"]),
        Jsonb(summary["unit_usage"]),
        summary["projection_version"],
        digest,
    ]
    assignments = ", ".join(
        f"{column} = EXCLUDED.{column}"
        for column in _SUMMARY_COLUMNS
        if column not in ("official_season_id", "lens", "category")
    )
    connection.execute(
        f"""
        INSERT INTO army_season_summaries ({", ".join(_SUMMARY_COLUMNS)})
        VALUES ({", ".join(["%s"] * len(values))})
        ON CONFLICT (official_season_id, lens, category) DO UPDATE
        SET {assignments}, published_at = clock_timestamp()
        """,
        values,
    )
    return False


def materialize_completed_army_season(
    connection: Any, *, season_id: str, now: datetime
) -> dict[str, Any]:
    """Bounded backfill of one completed season for both lenses.

    A live or otherwise active season is left untouched. Source detail is
    retained. Each lens materializes in a savepoint so one bad lens is
    reported as a lens failure while the other lens still publishes; a
    failed lens keeps its prior summaries.
    """
    if not season_id or len(season_id) > 80:
        raise ValueError("official season id is outside the supported range")
    from .season_retirement import (
        SEASON_DETAIL_RETIRED,
        acquire_season_lock_shared,
        is_season_detail_retired,
    )

    acquire_season_lock_shared(connection, season_id)
    if is_season_detail_retired(connection, season_id):
        return {
            "season_id": season_id,
            "season_completed": False,
            "reason": SEASON_DETAIL_RETIRED,
            "lenses": {},
        }
    completed, reason = _season_completed(connection, season_id, now)
    if not completed:
        return {
            "season_id": season_id,
            "season_completed": False,
            "reason": reason,
            "lenses": {},
        }
    report: dict[str, Any] = {
        "season_id": season_id,
        "season_completed": True,
        "reason": "",
        "lenses": {},
    }
    for lens in LENSES:
        try:
            with connection.transaction():
                report["lenses"][lens] = materialize_army_season(
                    connection, season_id, lens
                )
        except Exception as error:  # noqa: BLE001 - reported per lens, backfill continues
            report["lenses"][lens] = {
                "season_id": season_id,
                "lens": lens,
                "materialized": 0,
                "unchanged": 0,
                "failures": [{"category": None, "error": str(error)[:200]}],
                "content_digests": {},
            }
    return report
