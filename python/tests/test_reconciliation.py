from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clashlens.api_db import _screen_events
from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    BATTLE_EVENT_SERIALIZATION_VERSION,
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    reconcile_ranked_day,
    serialize_ranked_day_battles,
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


def test_complete_ranked_day_requires_paired_baselines_continuous_coverage_and_equation() -> (
    None
):
    result = reconcile_ranked_day(_input())

    assert result.state == "Complete"
    assert result.confidence == "exact"
    assert result.attack_count == 1
    assert result.defense_count == 1
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "confirmed"
    assert result.final_trophies_before_reset == 5940
    assert result.failure_reasons == ()


def test_automatic_defense_adjustment_can_be_calculated_without_end_confirmation() -> (
    None
):
    result = reconcile_ranked_day(
        _input(end_baseline_id=None, next_start_trophies=None)
    )

    assert result.state == "Partial"
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "calculated"
    assert "missing_end_baseline" in result.failure_reasons


def test_zero_defenses_does_not_apply_automatic_adjustment_and_has_uncertain_shield_rules() -> (
    None
):
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
    # The day's own change leaves out the reset, so trends see no fake drop.
    assert weekly.net_trophy_change == season.net_trophy_change == 20


def test_5000_after_a_reset_does_not_confirm_the_automatic_loss_or_final_total() -> (
    None
):
    # October 4, day 28: the same battles with two different previous-day
    # losses give two different final totals, and both become 5,000.
    sunday = ranked_day_for(datetime(2026, 10, 4, 12, tzinfo=UTC))
    previous_losses = (20, 80)
    season = [
        reconcile_ranked_day(
            _input(
                ranked_day=sunday,
                now=sunday.end + timedelta(minutes=1),
                next_start_trophies=5000,
                boundary_kind="season",
                previous_day=PreviousRankedDay(True, 2, loss, 0),
            )
        )
        for loss in previous_losses
    ]
    below_floor = reconcile_ranked_day(
        _input(start_trophies=4900, next_start_trophies=5000, boundary_kind="weekly")
    )
    # Ending on exactly 5,000 hides a missed loss just the same.
    at_floor = reconcile_ranked_day(
        _input(start_trophies=5060, next_start_trophies=5000, boundary_kind="weekly")
    )
    season_mismatch = reconcile_ranked_day(
        _input(next_start_trophies=4999, boundary_kind="season")
    )
    above_floor = reconcile_ranked_day(
        _input(next_start_trophies=5940, boundary_kind="weekly")
    )

    assert [r.final_trophies_before_reset for r in season] == [5940, 5800]
    for result in (*season, below_floor, at_floor):
        assert result.state == "Complete"
        assert result.unexplained_residual == 0
        assert result.automatic_defense_evidence_state == "calculated"
        assert result.confidence == "inferred"
    assert below_floor.boundary_adjustment_type == "weekly_reset"
    assert at_floor.final_trophies_before_reset == 5000
    assert (season_mismatch.state, season_mismatch.confidence) == (
        "Inconsistent",
        "uncertain",
    )
    # Above 5,000 a weekly Reset keeps the total, so the reading still proves it.
    assert above_floor.automatic_defense_evidence_state == "confirmed"
    assert above_floor.confidence == "exact"


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


def _season_day_1(attacks, defenses, previous_day, next_start_trophies):
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
        )
    )


def test_season_day_1_automatic_defense_loss_needs_no_previous_season_day() -> None:
    # Production, 5 October 2026 (Day 1): player #QPJURYV8, first tracked
    # during Day 1, so the previous Season's last day was never tracked. Their
    # 05:00 reading on 6 October, 5,009, is 5,000 plus the battles minus
    # floor(202 / 7) for the one missing defense.
    result = _season_day_1(
        (21, 27, 30, 40, 29, 22, 30, 40),
        (40, 28, 40, 15, 11, 28, 40),
        None,
        5009,
    )

    assert result.automatic_defense_loss == 28
    assert result.final_trophies_before_reset == 5009
    assert result.state == "Complete"
    assert "automatic_defense_basis_unavailable" not in result.failure_reasons


def test_season_day_1_automatic_defense_loss_leaves_out_the_previous_season() -> None:
    # Production, 5 October 2026 (Day 1): player #LGLCV8J2U, tracked all
    # along, lost 287 on 8 defenses on the previous Season's last day. Their
    # profile at 05:09 on 6 October, before any Day 2 battle, read 5,075:
    # 5,129 minus floor(165 / 6) * 2 = 54, not the 64 the previous Season's
    # day would give. The 05:00 reading, 5,129, came before the loss.
    result = _season_day_1(
        (40, 40, 28, 40, 26, 40, 40, 40),
        (18, 24, 40, 27, 28, 28),
        PreviousRankedDay(True, 8, 287, 0),
        5129,
    )

    assert result.automatic_defense_loss == 54
    assert result.final_trophies_before_reset == 5075
    assert result.unexplained_residual == 54


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


def test_malformed_battle_log_evidence_is_not_reported_as_a_normal_partial_day() -> (
    None
):
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


def test_shield_is_not_inferred_when_observed_trophies_changed_without_events() -> None:
    # Audit examples #22VRLQ29V (4,885 -> 4,886) and #2QP9LCLU (5,029 -> 5,004).
    for start, next_start in ((4885, 4886), (5029, 5004)):
        result = reconcile_ranked_day(
            _input(
                start_trophies=start,
                next_start_trophies=next_start,
                contributions=(),
                previous_day=PreviousRankedDay(True, 8, 100, 0),
            )
        )

        assert result.final_trophies_before_reset == start
        assert result.unexplained_residual == next_start - start
        assert result.state == "Inconsistent"
        assert result.confidence == "uncertain"
        assert result.shield_state == "unknown"
        assert result.shield_duration_days is None


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
        item for item in result.input_evidence["contributions"] if item["included"]
    ]
    excluded = [
        item for item in result.input_evidence["contributions"] if not item["included"]
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


def test_ranked_day_battle_events_are_canonical_signed_and_ordered() -> None:
    events = serialize_ranked_day_battles(
        (
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
                stars=3,
                destruction_percentage=100,
                opponent_tag=" #8pp ",
                opponent_name="Attacked player",
            ),
            # A two-sided/repeated observation is one canonical event. The
            # second row would also sort after the first if it were retained.
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=2,
                destruction_percentage=67,
                opponent_tag="#8PP",
                opponent_name="Stale name",
            ),
            BattleContribution(
                "defense-1",
                "defense",
                30,
                battle_timestamp=datetime(2026, 8, 4, 13, tzinfo=UTC),
                stars=1,
                destruction_percentage=50,
                opponent_tag="#9PP",
                opponent_name=None,
            ),
        )
    )

    assert BATTLE_EVENT_SERIALIZATION_VERSION == "legend-ranked-day-battle-events-v1"
    assert [event["battle_id"] for event in events] == ["defense-1", "attack-1"]
    assert events == [
        {
            "battle_id": "defense-1",
            "battle_timestamp": "2026-08-04T13:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 1,
            "trophy_change": -30,
        },
        {
            "battle_id": "attack-1",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": "Attacked player"},
            "destruction_percentage": 100,
            "stars": 3,
            "trophy_change": 40,
        },
    ]


def test_ranked_day_battle_events_exclude_invalid_and_unselected_rows() -> None:
    valid = BattleContribution(
        "accepted",
        "offense",
        0,
        battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
        stars=0,
        destruction_percentage=0,
        opponent_tag="#8PP",
    )
    events = serialize_ranked_day_battles(
        (
            valid,
            BattleContribution(
                "bad-stars",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=4,
                destruction_percentage=100,
                opponent_tag="#8PP",
            ),
            BattleContribution(
                "disagreement",
                "defense",
                20,
                battle_timestamp=datetime(2026, 8, 4, 10, tzinfo=UTC),
                stars=2,
                destruction_percentage=50,
                opponent_tag="#9PP",
                disagreement=True,
            ),
            {
                "battle_identity": "excluded",
                "lens": "offense",
                "included": False,
                "valid": True,
                "amount_used": 40,
                "battle_timestamp": "2026-08-04T09:00:00Z",
                "stars": 3,
                "destruction_percentage": 100,
                "opponent_tag": "#8PP",
            },
        )
    )

    # A battle the two sides report differently is counted, so it stays shown
    # and flagged for the website's "Result awaiting confirmation" note.
    assert events == [
        {
            "battle_id": "accepted",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": None},
            "destruction_percentage": 0,
            "stars": 0,
            "trophy_change": 0,
        },
        {
            "battle_id": "disagreement",
            "battle_timestamp": "2026-08-04T10:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 2,
            "trophy_change": -20,
            "disagreement": True,
        },
    ]
    # Publication rows carry each contribution's lens beside its event.
    lenses = {"accepted": "offense", "disagreement": "defense"}
    offense, defense = _screen_events(
        [{"lens": lenses[event["battle_id"]], **event} for event in events]
    )
    assert [event["perspective_disagreement"] for event in offense + defense] == [
        False,
        True,
    ]


def test_ranked_day_battle_events_can_be_empty() -> None:
    assert (
        serialize_ranked_day_battles(
            (
                {
                    "battle_identity": "missing-opponent",
                    "lens": "offense",
                    "included": True,
                    "valid": True,
                    "disagreement": False,
                    "amount_used": 40,
                    "battle_timestamp": "2026-08-04T12:00:00Z",
                    "stars": 3,
                    "destruction_percentage": 100,
                },
            )
        )
        == []
    )



def test_a_finished_day_with_eight_attacks_and_eight_defenses_has_a_known_net() -> (
    None
):
    battles = (
        *(BattleContribution(f"a{n}", "offense", 40) for n in range(7)),
        BattleContribution("a7", "offense", 20),
        *(BattleContribution(f"d{n}", "defense", 40) for n in range(7)),
        BattleContribution("d7", "defense", 31),
    )
    # Prodigi's Day 24: no Reset checks, so no trophy readings and no proof
    # the player was in Legend I, and an unreadable row in a battle log read.
    day = {
        "start_baseline_id": None,
        "end_baseline_id": None,
        "start_trophies": None,
        "next_start_trophies": None,
        "start_baseline_battle_log_observation_id": None,
        "end_baseline_battle_log_observation_id": None,
        "contributions": battles,
        "player_eligible": False,
        "malformed_evidence": True,
    }
    result = reconcile_ranked_day(_input(**day))
    assert result.net_trophy_change == 300 - 311
    assert result.state == "Malformed"
    assert "player_not_eligible" in result.failure_reasons

    ninth = BattleContribution("a8", "offense", 10)
    unknown = [
        # A ninth attack is a data error, not a valid day.
        {"contributions": (*battles, ninth)},
        # Seven defenses leave the automatic defense loss unknown.
        {"contributions": battles[:-1]},
        # Seven attacks may hide a missing one.
        {"contributions": battles[1:]},
        {"perspective_disagreement": True},
        # The day is still in progress.
        {"now": DAY.end - timedelta(hours=1)},
    ]
    results = [
        reconcile_ranked_day(_input(**{**day, **change})) for change in unknown
    ]
    assert [result.net_trophy_change for result in results] == [None] * len(unknown)
    assert "attack_count_exceeds_eight" in results[0].failure_reasons


def test_incomplete_coverage_withholds_the_final_total_and_net() -> None:
    no_overlap = list(_coverage())
    no_overlap[1] = CoverageObservation(
        observed_at=no_overlap[1].observed_at,
        row_count=50,
        battle_identities=("different",),
        has_row_gap=False,
        observation_id=102,
    )
    attack = BattleContribution("attack-1", "offense", 20)
    attacks = tuple(BattleContribution(f"a{n}", "offense", 30) for n in range(8))
    defenses = tuple(BattleContribution(f"d{n}", "defense", 25) for n in range(8))
    sunday = ranked_day_for(datetime(2026, 10, 4, 12, tzinfo=UTC))
    days = {
        # One attack and no defenses were kept, but a log page was missed.
        "missing battles": {"contributions": (attack,), "next_start_trophies": None},
        # Eight attacks and no defenses: a missed defense would not show.
        "missing defense": {"contributions": attacks, "next_start_trophies": None},
        # Seven attacks and eight defenses: a missed attack would not show.
        "missing attack": {
            "contributions": (*attacks[1:], *defenses),
            "next_start_trophies": None,
        },
        # Day 28: the Season reset makes any total 5,000, so the next start
        # cannot expose the missed battles.
        "season close": {
            "ranked_day": sunday,
            "now": sunday.end + timedelta(minutes=1),
            "contributions": (attack,),
            "next_start_trophies": 5000,
            "boundary_kind": "season",
        },
    }
    for name, day in days.items():
        result = reconcile_ranked_day(
            _input(coverage_observations=tuple(no_overlap), **day)
        )
        assert result.coverage_complete is False, name
        assert result.final_trophies_before_reset is None, name
        assert result.net_trophy_change is None, name
        assert result.expected_next_start_trophies is None, name
        assert result.boundary_adjustment_type is None, name
        assert result.formula_components["final_trophies_before_reset"] is None, name
        assert (result.state, result.confidence) == ("Partial", "uncertain"), name
        assert "battle_log_overlap_gap" in result.failure_reasons, name
    assert reconcile_ranked_day(
        _input(coverage_observations=tuple(no_overlap), **days["season close"])
    ).failure_reasons == ("battle_log_overlap_gap",)

    # With every battle captured the same day keeps its total.
    complete = reconcile_ranked_day(_input(**days["season close"]))
    assert (complete.final_trophies_before_reset, complete.net_trophy_change) == (
        6020,
        20,
    )
    # All 8 attacks and 8 defenses on record leave nothing to miss.
    full = reconcile_ranked_day(
        _input(
            coverage_observations=tuple(no_overlap),
            contributions=(*attacks, *defenses),
            next_start_trophies=None,
        )
    )
    assert (full.final_trophies_before_reset, full.net_trophy_change) == (6040, 40)
