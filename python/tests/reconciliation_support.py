"""Shared setup for reconciliation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    reconcile_ranked_day,
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


def _counted_battles(tag: str, lens: str, count: int, total: int) -> tuple[BattleContribution, ...]:
    amounts = [total // count] * (count - 1) + [total - total // count * (count - 1)]
    return tuple(
        BattleContribution(f"{tag}-{lens}-{index}", lens, amount)
        for index, amount in enumerate(amounts)
    )


def _season_day_1(attacks, defenses, previous_day, next_start_trophies, **overrides):
    day_1 = ranked_day_for(datetime(2026, 10, 5, 12, tzinfo=UTC))
    return reconcile_ranked_day(
        _input(
            ranked_day=day_1,
            now=day_1.end + timedelta(minutes=1),
            start_trophies=5000,
            next_start_trophies=next_start_trophies,
            contributions=(
                *(BattleContribution(f"a{i}", "offense", n) for i, n in enumerate(attacks)),
                *(BattleContribution(f"d{i}", "defense", n) for i, n in enumerate(defenses)),
            ),
            previous_day=previous_day,
            season_first_day=True,
            **overrides,
        )
    )


def _attacks(*amounts: int) -> tuple[BattleContribution, ...]:
    return tuple(BattleContribution(f"a{i}", "offense", n) for i, n in enumerate(amounts))


def _defenses(*amounts: int) -> tuple[BattleContribution, ...]:
    return tuple(BattleContribution(f"d{i}", "defense", n) for i, n in enumerate(amounts))


def _october(day: int, **overrides) -> ReconciliationInput:
    ranked_day = ranked_day_for(datetime(2026, 10, day, 12, tzinfo=UTC))
    return _input(ranked_day=ranked_day, now=ranked_day.end + timedelta(minutes=5), **overrides)
