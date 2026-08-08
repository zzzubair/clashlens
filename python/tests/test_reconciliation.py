from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    RECONCILIATION_RULE_VERSION,
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
            next_start_trophies=6000,
            contributions=(),
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )
    third = reconcile_ranked_day(
        _input(
            next_start_trophies=6000,
            contributions=(),
            previous_day=PreviousRankedDay(True, 0, 0, 2),
        )
    )

    assert first.automatic_defense_loss is None
    assert first.automatic_defense_evidence_state == "not_applicable"
    assert first.state == "Complete"
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

    assert gap_result.state == "Malformed"
    assert "battle_log_row_gap" in gap_result.failure_reasons
    assert overlap_result.state == "Partial"
    assert "battle_log_overlap_gap" in overlap_result.failure_reasons


def test_weekly_and_season_reset_adjustments_reconcile_against_5000_baseline() -> None:
    weekly = reconcile_ranked_day(
        _input(
            start_trophies=4900,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            next_start_trophies=5000,
            boundary_kind="weekly",
        )
    )
    season = reconcile_ranked_day(
        _input(
            contributions=(BattleContribution("attack-1", "offense", 20),),
            next_start_trophies=5000,
            boundary_kind="season",
        )
    )

    assert weekly.state == "Complete"
    assert weekly.final_trophies_before_reset == 4920
    assert weekly.boundary_adjustment == 80
    assert weekly.boundary_adjustment_type == "weekly_reset"
    assert season.state == "Complete"
    assert season.final_trophies_before_reset == 6020
    assert season.boundary_adjustment == -1020
    assert season.boundary_adjustment_type == "season_reset"


def test_automatic_defense_uses_previous_and_current_observed_losses() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=5858,
            contributions=(BattleContribution("defense-1", "defense", 30),),
            previous_day=PreviousRankedDay(
                complete=True,
                observed_defense_count=2,
                observed_defense_loss=20,
                shield_run_length=0,
            ),
        )
    )

    assert result.automatic_defense_loss == 112
    assert result.final_trophies_before_reset == 5858
    assert result.state == "Complete"


def test_shield_is_not_inferred_when_the_player_has_an_attack() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            previous_day=PreviousRankedDay(
                complete=True,
                observed_defense_count=8,
                observed_defense_loss=100,
                shield_run_length=0,
            ),
        )
    )

    assert result.shield_state == "not_inferred"
    assert result.shield_duration_days is None


def test_weekly_reset_is_not_applied_when_final_trophies_are_at_or_above_5000() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            boundary_kind="weekly",
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.final_trophies_before_reset == 6020
    assert result.boundary_adjustment == 0
    assert result.boundary_adjustment_type is None
    assert result.state == "Complete"


def test_reconciliation_exposes_net_change_boundary_and_unexplained_residual() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=5941,
            contributions=(
                BattleContribution("attack-1", "offense", 20),
                BattleContribution("defense-1", "defense", 10),
            ),
        )
    )

    assert result.attack_trophy_gain == 20
    assert result.observed_defense_loss == 10
    assert result.net_trophy_change == -60
    assert result.boundary_adjustment == 0
    assert result.unexplained_residual == 1
    assert result.formula_components["expected_next_start_trophies"] == 5940
    assert result.state == "Inconsistent"
    assert "trophy_equation_mismatch" in result.failure_reasons


def test_malformed_battle_log_evidence_is_not_reported_as_a_normal_partial_day() -> None:
    result = reconcile_ranked_day(
        _input(
            coverage_observations=_coverage(gap=True),
        )
    )

    assert result.state == "Malformed"
    assert "battle_log_row_gap" in result.failure_reasons


def test_shield_requires_observation_coverage() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6000,
            contributions=(),
            coverage_observations=(),
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.shield_state == "unknown"
    assert result.shield_duration_days is None
    assert result.automatic_defense_evidence_state == "not_applicable"


def test_repeated_and_two_sided_contributions_count_once_per_own_perspective() -> None:
    result = reconcile_ranked_day(
        _input(
            contributions=(
                BattleContribution("shared-battle", "offense", 20),
                BattleContribution("shared-battle", "offense", 20),
                BattleContribution("shared-battle", "defense", 10),
                BattleContribution("shared-battle", "defense", 10),
            )
        )
    )

    assert result.state == "Complete"
    assert result.attack_count == 1
    assert result.defense_count == 1
    assert result.attack_trophy_gain == 20
    assert result.observed_defense_loss == 10
    included = [
        item
        for item in result.input_evidence["contributions"]
        if item["included"]
    ]
    excluded = [
        item
        for item in result.input_evidence["contributions"]
        if not item["included"]
    ]
    assert {(item["lens"], item["included"]) for item in included} == {
        ("offense", True),
        ("defense", True),
    }
    assert len(excluded) == 2


def test_incomplete_coverage_keeps_one_to_seven_defense_adjustment_unknown() -> None:
    result = reconcile_ranked_day(
        _input(
            coverage_observations=_coverage(gap=True),
            contributions=(BattleContribution("defense-1", "defense", 10),),
            next_start_trophies=None,
        )
    )

    assert result.automatic_defense_loss is None
    assert result.automatic_defense_evidence_state == "unknown"
    assert result.state == "Malformed"
    assert "automatic_defense_basis_unavailable" in result.failure_reasons


def test_post_boundary_baseline_battle_log_responses_can_form_a_complete_chain() -> (
    None
):
    # A reset-baseline sweep requests its endpoints after the 05:00 UTC
    # boundary, so both baseline battle-log responses are observed strictly
    # after the ranked-day boundary timestamps. The chain must be bound to the
    # exact battle-log observations that the start and end sweeps selected,
    # not to impossible boundary-time observations.
    result = reconcile_ranked_day(
        _input(
            coverage_observations=(
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
                    has_row_gap=False,
                    observation_id=102,
                ),
                CoverageObservation(
                    observed_at=DAY.end + timedelta(seconds=5),
                    row_count=2,
                    battle_identities=("daily",),
                    has_row_gap=False,
                    observation_id=103,
                ),
            ),
            start_baseline_battle_log_observation_id=101,
            end_baseline_battle_log_observation_id=103,
        )
    )

    assert result.state == "Complete"
    assert result.coverage_complete is True
    assert result.failure_reasons == ()


def test_more_than_eight_defenses_is_a_visible_partial_anomaly() -> None:
    result = reconcile_ranked_day(
        _input(
            contributions=tuple(
                BattleContribution(f"defense-{index}", "defense", 10)
                for index in range(9)
            ),
            next_start_trophies=5910,
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.defense_count == 9
    assert result.automatic_defense_loss is None
    assert result.state == "Partial"
    assert "defense_count_exceeds_eight" in result.failure_reasons
