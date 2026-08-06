from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens_prototype.domain import ranked_day_for
from clashlens_prototype.reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    reconcile_ranked_day,
)

DAY = ranked_day_for(datetime(2026, 8, 4, 12, tzinfo=UTC))


def _coverage(*, gap: bool = False) -> tuple[CoverageObservation, ...]:
    return (
        CoverageObservation(
            observed_at=DAY.start,
            row_count=50,
            battle_identities=("older", "shared"),
            has_row_gap=False,
        ),
        CoverageObservation(
            observed_at=DAY.start + timedelta(hours=12),
            row_count=50,
            battle_identities=("shared", "daily"),
            has_row_gap=gap,
        ),
        CoverageObservation(
            observed_at=DAY.end,
            row_count=2,
            battle_identities=("daily",),
            has_row_gap=False,
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


def test_active_ranked_day_is_live_without_claiming_complete_evidence() -> None:
    result = reconcile_ranked_day(
        _input(now=DAY.start + timedelta(hours=1), end_baseline_id=None)
    )

    assert result.state == "Live"
    assert result.confidence == "partial"
    assert result.reconciliation_rule_version == RECONCILIATION_RULE_VERSION


def test_complete_ranked_day_requires_paired_baselines_continuous_coverage_and_equation() -> None:
    result = reconcile_ranked_day(_input())

    assert result.state == "Complete"
    assert result.confidence == "exact"
    assert result.attack_count == 1
    assert result.defense_count == 1
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "confirmed"
    assert result.final_trophies_before_reset == 5940
    assert result.failure_reasons == ()


def test_automatic_defense_adjustment_can_be_calculated_without_end_confirmation() -> None:
    result = reconcile_ranked_day(
        _input(end_baseline_id=None, next_start_trophies=None)
    )

    assert result.state == "Partial"
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "calculated"
    assert "missing_end_baseline" in result.failure_reasons


def test_zero_defenses_does_not_apply_automatic_adjustment_and_has_uncertain_shield_rules() -> None:
    first = reconcile_ranked_day(
        _input(
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )
    third = reconcile_ranked_day(
        _input(
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            previous_day=PreviousRankedDay(True, 0, 0, 2),
        )
    )

    assert first.automatic_defense_loss is None
    assert first.shield_state == "inferred_shielded"
    assert first.shield_duration_days == 1
    assert third.shield_state == "uncertain_sequence"
    assert "shield_sequence_longer_than_two_days" in third.failure_reasons


def test_coverage_gap_or_missing_overlap_makes_ended_day_partial() -> None:
    no_overlap = list(_coverage())
    no_overlap[1] = CoverageObservation(
        observed_at=no_overlap[1].observed_at,
        row_count=50,
        battle_identities=("different",),
        has_row_gap=False,
    )

    gap_result = reconcile_ranked_day(_input(coverage_observations=_coverage(gap=True)))
    overlap_result = reconcile_ranked_day(
        _input(coverage_observations=tuple(no_overlap))
    )

    assert gap_result.state == "Partial"
    assert "battle_log_row_gap" in gap_result.failure_reasons
    assert overlap_result.state == "Partial"
    assert "battle_log_overlap_gap" in overlap_result.failure_reasons


def test_weekly_and_season_reset_adjustments_reconcile_against_5000_baseline() -> None:
    for boundary_kind in ("weekly", "season"):
        result = reconcile_ranked_day(
            _input(
                contributions=(BattleContribution("attack-1", "offense", 20),),
                next_start_trophies=5000,
                boundary_kind=boundary_kind,
            )
        )

        assert result.state == "Complete"
        assert result.final_trophies_before_reset == 6020
        assert result.boundary_adjustment == -1020
        assert result.boundary_adjustment_type == f"{boundary_kind}_reset"
