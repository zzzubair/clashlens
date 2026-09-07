"""Shared whole-season army summaries (issue #82, army slice).

One independently readable ``army_season_summaries`` record per
(season, lens, category) holds the whole-season aggregate projected from
the current versioned ``army_analytics_battle_facts`` rows: usage counts
and rates, 0/1/2/3-star attack counts with the three-star rate derived
from the stored attack sample, and the underlying denominators plus
excluded/undecodable counts and honest coverage. Projection reuses
``build_army_result`` so denominators and star math match the live reads.
Historical reads serve these rows only and never touch battle facts;
offense and defense stay separate lenses so opposite perspectives are
never mixed into duplicate attacks. Unknown stays explicit and a season
with fewer than 28 completed army days stays partial. Existing detail is
retained; no cleanup is authorized here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from .army_analytics import (
    CATEGORIES,
    ArmyAnalyticsSelection,
    build_army_result,
)

# The completed-season gate is shared with the player summaries so both
# slices agree on when a season is finalizable.
from .season_summaries import _season_completed

PROJECTION_VERSION = "army-season-summary-v1"
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


def _project_lens(connection: Any, season_id: str, lens: str) -> dict[str, Any]:
    """Aggregate one season-lens across all categories from current facts.

    Facts are keyed one-current-row per (battle_id, lens), so each lens
    counts its own attacks exactly once.
    """
    rows = connection.execute(
        """
        SELECT stars, destruction_percentage, army_state, home_troops,
               spells, siege, cc_troops, heroes, unresolved_components,
               perspective_disagreement, battle_time_trophies
        FROM army_analytics_battle_facts
        WHERE official_season_id = %s AND lens = %s AND is_current
        ORDER BY battle_id
        """,
        (season_id, lens),
    ).fetchall()
    facts = [
        {
            "stars": int(row[0]),
            "destruction_percentage": int(row[1]),
            "army_state": _text(row[2]),
            "home_troops": row[3] or [],
            "spells": row[4] or [],
            "siege": row[5] or [],
            "cc_troops": row[6] or [],
            "heroes": row[7] or [],
            "unresolved_components": row[8] or [],
            "perspective_disagreement": bool(row[9]),
        }
        for row in rows
    ]
    missing_trophies = sum(1 for row in rows if row[10] is None)
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
    for category in sorted(CATEGORIES):
        # Direct construction: the historical sample is the whole season
        # ("all"), which live-selection validation does not name. Only the
        # category, sort, and day range reach the shared builder.
        selection = ArmyAnalyticsSelection(
            lens=lens,
            season=season_id,
            start_day=1,
            end_day=28,
            population=HISTORICAL_POPULATION,
            category=category,
            sort="usage-rate",
        )
        result = build_army_result(facts, selection)
        summaries[category] = {
            "days_observed": len(observed),
            "days_missing": len(missing),
            "missing_days": missing,
            "coverage_state": coverage_state,
            "total_attacks": result["total_attacks"],
            "usable_army_sample": result["usable_army_sample"],
            "army_states": result["army_states"],
            "unknown_affected_attacks": result["unknown_affected_attacks"],
            "unknown_component_occurrences": result[
                "unknown_component_occurrences"
            ],
            "perspective_disagreement_count": result[
                "perspective_disagreement_count"
            ],
            "missing_trophy_membership_evidence": missing_trophies,
            "result_rows": result["rows"],
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

    Competing projections for the same season-lens serialize on a
    transaction-scoped advisory lock. All categories publish together:
    any failure raises and aborts the whole lens, so the prior complete
    lens stays readable and a lens is never published partially.
    Unchanged categories are a no-op that leaves the existing rows
    (including published_at) untouched. Offense and defense are
    independent lenses; callers isolate them (see
    materialize_completed_army_season) so one bad lens cannot abort the
    other.
    """
    _check_season_lens(season_id, lens)
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"army-season:{season_id}:{lens}",),
    )
    projected = _project_lens(connection, season_id, lens)
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
