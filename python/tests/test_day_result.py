"""A Legend day's named states: its evidence state and confidence, its
automatic loss evidence and settlement, and its inferred shield run, as
``reconcile_ranked_day`` decides them from the day's evidence.

The default day starts at 6,000 with a +20 attack and a 10-trophy defense;
the day before's 2 defenses lost 20, so 7 missing defenses cost 70 and the
day ends at 5,940.
"""

from __future__ import annotations

from datetime import timedelta

from test_reconciliation import DAY, _coverage, _input

from clashlens import loss_rule
from clashlens.day_result import (
    Confidence,
    DayState,
    ShieldState,
    assess,
    shield_run_length,
)
from clashlens.loss_rule import Landing, LossEvidence, StartCharge
from clashlens.reading_rule import Reading
from clashlens.reconciliation import PreviousRankedDay, reconcile_ranked_day

RESET_AT = {"profile": {"observed_at": (DAY.end + timedelta(minutes=3)).isoformat()}}


def _states(**overrides) -> tuple[DayState, Confidence, LossEvidence]:
    result = reconcile_ranked_day(_input(**overrides))
    return result.state, result.confidence, result.automatic_defense_evidence_state


def test_a_day_moves_between_states_as_its_evidence_changes() -> None:
    # Each calculation starts again from all the day's evidence, so later
    # evidence can move a saved day either way.
    assert _states(now=DAY.start + timedelta(hours=1), end_baseline_id=None) == (
        DayState.LIVE, Confidence.PARTIAL, LossEvidence.CALCULATED)
    # No end reading: Partial, and with no end its shield cannot be told.
    assert _states(end_baseline_id=None, next_start_trophies=None) == (
        DayState.PARTIAL, Confidence.UNCERTAIN, LossEvidence.CALCULATED)
    assert _states() == (DayState.COMPLETE, Confidence.EXACT, LossEvidence.CONFIRMED)
    # The Reset reading came before the loss: the end is calculated.
    assert _states(next_start_trophies=6010, end_baseline_evidence=RESET_AT) == (
        DayState.COMPLETE, Confidence.INFERRED, LossEvidence.CALCULATED)
    assert _states(next_start_trophies=5900)[:2] == (DayState.INCONSISTENT, Confidence.UNCERTAIN)
    assert _states(malformed_evidence=True)[:2] == (DayState.MALFORMED, Confidence.UNCERTAIN)
    # A gap between log rows is unreadable evidence, and leaves the
    # automatic loss without a basis.
    assert _states(coverage_observations=_coverage(gap=True)) == (
        DayState.MALFORMED, Confidence.UNCERTAIN, LossEvidence.UNKNOWN)


def test_a_complete_day_whose_shield_cannot_be_told_is_uncertain_unless_calculated() -> None:
    common = {"failures": (), "coverage_complete": True, "start_available": True,
              "end_available": True}
    assert assess(True, shield=ShieldState.UNKNOWN, **common) == (
        DayState.COMPLETE, Confidence.UNCERTAIN)
    assert assess(True, shield=ShieldState.UNKNOWN, calculated=True, **common) == (
        DayState.COMPLETE, Confidence.INFERRED)
    # A trophy mismatch outranks a gap; malformed evidence outranks both.
    assert assess(True, failures=("battle_log_row_gap", "trophy_equation_mismatch")) == (
        DayState.INCONSISTENT, Confidence.UNCERTAIN)
    assert assess(True, malformed=True, inconsistent=True) == (
        DayState.MALFORMED, Confidence.UNCERTAIN)


def _standing_still(run: int, **overrides):
    """A day with no battle and the same trophies at both Resets, after a
    saved day leaving a shield run of ``run``."""
    return reconcile_ranked_day(_input(**{
        "next_start_trophies": 6000, "contributions": (),
        "previous_day": PreviousRankedDay(True, 0, 0, run), **overrides}))


def test_a_shield_run_grows_day_by_day_and_ends_at_a_battle_or_doubt() -> None:
    first = _standing_still(0)
    assert (first.shield_state, first.shield_duration_days) == (ShieldState.INFERRED_SHIELDED, 1)
    # The next day reads the saved day before, as ``load_previous_day`` does.
    second = _standing_still(shield_run_length(first.shield_state, first.shield_duration_days))
    assert (second.shield_state, second.shield_duration_days) == (ShieldState.INFERRED_SHIELDED, 2)

    battled = reconcile_ranked_day(_input(previous_day=PreviousRankedDay(
        True, 2, 20, shield_run_length(second.shield_state, second.shield_duration_days))))
    assert (battled.shield_state, battled.shield_duration_days) == (ShieldState.NOT_INFERRED, None)
    assert shield_run_length(battled.shield_state, battled.shield_duration_days) == 0

    # Doubt ends the run too: the next shielded day starts a new one.
    doubted = _standing_still(2, coverage_observations=_coverage(gap=True))
    assert (doubted.shield_state, doubted.confidence) == (ShieldState.UNKNOWN, Confidence.UNCERTAIN)
    restarted = _standing_still(shield_run_length(doubted.shield_state, doubted.shield_duration_days))
    assert restarted.shield_duration_days == 1
    # Trophies that moved with no battle are not a shield.
    moved = _standing_still(1, next_start_trophies=5990)
    assert moved.shield_state is not ShieldState.INFERRED_SHIELDED
    # A live day is never inferred.
    live = _standing_still(1, now=DAY.start + timedelta(hours=1), end_baseline_id=None)
    assert live.shield_state is ShieldState.NOT_INFERRED


def test_a_loss_only_a_reading_can_show_is_taken_from_the_reading() -> None:
    # No defense used: the game may charge all 8 at the day before's average
    # of 10, or nothing; only a reading tells. The Reset reading showing it
    # makes it calculated, and, isolating it exactly, confirmed.
    kept = _standing_still(0, previous_day=PreviousRankedDay(True, 2, 20, 0))
    charged = _standing_still(0, previous_day=PreviousRankedDay(True, 2, 20, 0),
                              next_start_trophies=5920)
    assert (kept.automatic_defense_evidence_state, kept.automatic_defense_loss) == (
        LossEvidence.NOT_APPLICABLE, None)
    assert (charged.automatic_defense_evidence_state, charged.automatic_defense_loss) == (
        LossEvidence.CONFIRMED, 80)


def test_the_day_before_loss_settles_through_the_next_days_start() -> None:
    # Taken to land after the Reset reading: the next day is tried from the
    # whole loss off first, then none of it.
    unsettled = PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                                  unsettled_automatic_loss=70)
    unread = PreviousRankedDay(False, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                               state="Inconsistent", battles_known=True)
    settled = PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10, automatic_loss=70)
    assert loss_rule.other_start(_input(previous_day=unsettled)) is StartCharge.NOT_CHARGED
    assert loss_rule.other_start(_input(previous_day=unread)) is StartCharge.CHARGED
    assert loss_rule.other_start(_input(previous_day=settled)) is None
    never = reconcile_ranked_day(_input(previous_day=unsettled, next_start_trophies=6010))
    assert never.formula_components["start_automatic_loss_charged"] is False
    assert (never.state, never.start_trophies) == (DayState.COMPLETE, 6000)

    # This day's own loss: a reading before it carries it into the next
    # day; the next day's proven start then says it never landed, or that a
    # reading taken as is came before it (late).
    def ended(reading: int, next_day_start: int | None):
        return reconcile_ranked_day(_input(
            next_start_trophies=reading, next_day_start=next_day_start,
            end_baseline_evidence=RESET_AT))

    carried = ended(6010, None)
    assert (carried.unsettled_automatic_loss, carried.next_start_trophies) == (70, 5940)
    data = _input(next_day_start=6010)
    assert loss_rule.next_day_landing(data, 70, 70, 5940) is Landing.NEVER
    assert loss_rule.next_day_landing(_input(next_day_start=5933), 70, 0, 6003) is Landing.LATE
    assert loss_rule.next_day_landing(_input(next_day_start=6000), 70, 70, 5940) is None
    never_landed = ended(6010, 6010)
    assert (never_landed.automatic_defense_loss, never_landed.unsettled_automatic_loss) == (0, 0)
    late = ended(6003, 5933)
    assert (late.next_start_trophies, late.unsettled_automatic_loss) == (5933, 70)


def test_calculating_a_day_again_from_the_same_evidence_gives_the_same_result() -> None:
    data = _input(next_start_trophies=6010, end_baseline_evidence=RESET_AT,
                  readings=(Reading(DAY.end + timedelta(minutes=20), 5940),))
    assert reconcile_ranked_day(data) == reconcile_ranked_day(data)
