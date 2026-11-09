"""Shared setup for reconciliation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
)

DAY = ranked_day_for(datetime(2026, 8, 4, 12, tzinfo=UTC))


def _coverage(*, gap: bool = False) -> tuple[CoverageObservation, ...]:
    # Reset-baseline sweeps request their endpoints after the 05:00 UTC
    # boundary, so the chain head and tail are observed strictly after the
    # ranked-day boundary timestamps and are identified by their observation
    # ids rather than by boundary-time equality.
    return (
        CoverageObservation(
            observed_at=DAY.start + timedelta(seconds=5),
            row_count=50,
            battle_identities=("older", "shared"),
            has_row_gap=False,
            observation_id=101,
        ),
        CoverageObservation(
            observed_at=DAY.start + timedelta(hours=12),
            row_count=50,
            battle_identities=("shared", "daily"),
            has_row_gap=gap,
            observation_id=102,
        ),
        CoverageObservation(
            observed_at=DAY.end + timedelta(seconds=5),
            row_count=2,
            battle_identities=("daily",),
            has_row_gap=False,
            observation_id=103,
        ),
    )


def _input(**overrides) -> ReconciliationInput:
    values = {
        "ranked_day": DAY,
        "now": DAY.end + timedelta(minutes=1),
        "start_baseline_id": 10,
        "end_baseline_id": 11,
        "start_trophies": 6000,
        "next_start_trophies": 5940,
        "start_baseline_battle_log_observation_id": 101,
        "end_baseline_battle_log_observation_id": 103,
        "coverage_observations": _coverage(),
        "contributions": (
            BattleContribution("attack-1", "offense", 20),
            BattleContribution("defense-1", "defense", 10),
        ),
        "previous_day": PreviousRankedDay(
            complete=True,
            observed_defense_count=2,
            observed_defense_loss=20,
            shield_run_length=0,
        ),
        "boundary_kind": None,
        "season_anchor_valid": True,
    }
    values.update(overrides)
    return ReconciliationInput(**values)


def _battles(lens: str, *amounts: int) -> tuple[BattleContribution, ...]:
    return tuple(
        BattleContribution(f"{lens}-{n}", lens, amount, battle_timestamp=DAY.start + timedelta(hours=n + 1))
        for n, amount in enumerate(amounts)
    )
