"""The Reset total, decided in one place (``reset_total``): each row of its
table, and a proof that later readings cannot take back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from clashlens import reading_rule
from clashlens.reading_rule import Effect, Reading
from clashlens.reset_total import Battle, ResetTotal, reset_total

RESET = datetime(2026, 10, 7, 5, tzinfo=UTC)
DAY_START = RESET - timedelta(days=1)


def _at(hour: int, minute: int = 0) -> datetime:
    return RESET.replace(hour=hour, minute=minute)


def _after(minutes: int) -> datetime:
    return RESET + timedelta(minutes=minutes)


# The day starts at 5,000, attacks for 40 at 04:11 and loses a defense for
# 20 at 04:40: it ends at 5,020 before an automatic loss of 30.
BATTLES = (Battle(40, _at(4, 11), False), Battle(-20, _at(4, 40), True))
EFFECTS = (Effect("attack", 40, _at(4, 11)), Effect("defense", -20, _at(4, 40)))
# The newest reading before the Reset, before the defense showed.
OLD = Reading(_at(4, 30), 5040, requested_at=_at(4, 30))
LEDGER = {"loss_candidates": (30,), "loss_certain": True, "day_effects": EFFECTS,
          "new_day_effects": (), "floor": None, "reset": False}


def _total(*readings: Reading, state: str = "Complete", failures: tuple[str, ...] = (),
           old: Reading | None = OLD,
           **changes: Any) -> ResetTotal:
    inputs: dict[str, Any] = {
        "reset_at": RESET, "day_start": DAY_START, "state": state, "failure_reasons": failures,
        "end_before_loss": 5020, "eod": 4990, "season_end": False, "official_final": None,
        "battles": BATTLES, "battles_usable": True, "old_reading": old,
        "end_proof": {"readings": readings, "reset_at": RESET, "start_proven": True, **LEDGER},
        "reading_proof": {"readings": readings, "reset_at": RESET, **LEDGER},
    }
    return reset_total(**(inputs | changes))


def _decide(*readings: Reading) -> reading_rule.Verdict:
    return reading_rule.decide(readings, reset_at=RESET, end_before_loss=5020,
                               start_proven=True, **LEDGER)


def test_a_seasons_last_day_shows_the_games_official_final() -> None:
    assert _total(state="Inconsistent", season_end=True, official_final=5111) == ResetTotal(
        5111, True, "official_final")


def test_a_complete_day_shows_its_calculated_end_proven_by_its_first_reading() -> None:
    assert _total(Reading(_after(4), 5020)) == ResetTotal(5020, True, "calculated_end", _after(4))


def test_a_complete_day_no_reading_proves_is_uncertain() -> None:
    assert _total() == ResetTotal(5020, False, "calculated_end")


def test_an_unproven_day_shows_its_calculated_end_uncertain() -> None:
    assert _total(state="Partial", failures=("end_reading_unverified",)) == ResetTotal(
        5020, False, "calculated_end")


def test_an_unproven_day_shows_the_reading_side_once_a_later_reading_proves_it() -> None:
    """The game never credits the 40 for the attack: the old reading,
    5,000, plus the defense after it, 4,980, is what a later reading shows,
    which the day's own end reads only as the attacker's credit lagging."""
    later = Reading(_after(44), 4950)
    old = Reading(_at(4, 30), 5000, requested_at=_at(4, 30))
    assert _decide(later).outcome == "unverified"
    assert _total(later, state="Partial", failures=("end_reading_unverified",), old=old) == (
        ResetTotal(4980, True, "reading_side", _after(44), _at(4, 30)))


def test_proven_once_a_later_proof_of_the_other_answer_takes_nothing_back() -> None:
    """At 05:44 the profile shows 4,950: the reading side's end less the
    loss, or the day's own end lacking the attack's credit. At 06:30 the
    credit has landed, 4,990, which proves the day's own end and makes the
    day Complete; the reading side proven at 05:44 stands."""
    old = Reading(_at(4, 30), 5000, requested_at=_at(4, 30))
    readings = (Reading(_after(44), 4950), Reading(_after(90), 4990))
    assert _decide(*readings).outcome == "verified"
    proven = ResetTotal(4980, True, "reading_side", _after(44), _at(4, 30))
    assert _total(*readings[:1], state="Partial", failures=("end_reading_unverified",),
                  old=old) == proven
    assert _total(*readings, old=old) == proven
    # Proven by the same reading, the day's own end stands.
    assert _total(Reading(_after(44), 4990), old=Reading(_at(4, 30), 5040)) == ResetTotal(
        5020, True, "calculated_end", _after(44))


def test_a_contradicted_day_shows_the_old_reading_plus_the_battles_after_it() -> None:
    contradicting = Reading(_after(4), 5030)
    old = Reading(_at(4, 30), 5050, requested_at=_at(4, 30))
    assert _decide(contradicting).outcome == "contradicted"
    assert _total(contradicting, state="Inconsistent", failures=("trophy_equation_mismatch",),
                  old=old) == ResetTotal(5030, True, "reading_side", _after(4), _at(4, 30))
    assert _total(state="Inconsistent", failures=("trophy_equation_mismatch",), old=old) == (
        ResetTotal(5030, False, "reading_side", reading_at=_at(4, 30)))


def test_the_reading_side_is_proven_only_once_every_other_way_to_read_it_is_ruled_out() -> None:
    """The old reading at 04:42 may or may not show the defense reported at
    04:40: the day ends at 5,020 if it does, 5,000 if not. At 05:04, while
    a new-day attack for 30 may or may not show, 5,020 rules 5,000 out but
    proves nothing; 5,050 at 05:20 proves 5,020."""
    ledger = {**LEDGER, "new_day_effects": (Effect("new", 30, _after(2)),)}
    readings = (Reading(_after(4), 5020), Reading(_after(20), 5050))
    contradicted: dict[str, Any] = {
        "state": "Inconsistent", "failures": ("trophy_equation_mismatch",),
        "old": Reading(_at(4, 42), 5020, requested_at=_at(4, 41)), "end_before_loss": 4900}
    assert _total(**contradicted, reading_proof={
        "readings": readings[:1], "reset_at": RESET, **ledger}).proven is False
    assert _total(**contradicted, reading_proof={
        "readings": readings, "reset_at": RESET, **ledger}) == ResetTotal(
        5020, True, "reading_side", _after(20), _at(4, 42))


def test_without_usable_battles_the_old_reading_stands_alone() -> None:
    assert _total(state="Partial", failures=("battle_log_row_gap",), battles_usable=False) == (
        ResetTotal(5040, False, "old_reading", reading_at=_at(4, 30)))
    early = Reading(DAY_START + timedelta(minutes=10), 5000)
    assert _total(state="Partial", failures=("missing_start_baseline",), old=early) == (
        ResetTotal(5000, False, "old_reading", reading_at=early.read_at))
    assert _total(state="Partial", old=None, end_before_loss=None) == ResetTotal(
        None, False, "none")


def test_a_battle_ending_after_the_reset_counts_for_the_day_but_not_the_total() -> None:
    battles = (*BATTLES, Battle(30, _after(1), False),
               Battle(-10, _at(4, 58), True, attack_ended_at=_after(1)))
    assert _total(state="Partial", failures=("end_reading_unverified",), battles=battles,
                  end_before_loss=5040) == ResetTotal(5020, False, "calculated_end")


def test_a_defense_just_before_the_reset_without_its_attackers_report_is_not_proven() -> None:
    late = (*BATTLES, Battle(-10, _at(4, 58), True))
    effects = (*EFFECTS, Effect("late", -10, _at(4, 58)))
    proof = {"readings": (Reading(_after(30), 4980),), "reset_at": RESET, "start_proven": True,
             **LEDGER, "day_effects": effects}
    assert _total(battles=late, end_before_loss=5010, end_proof=proof) == ResetTotal(
        5010, False, "calculated_end")
    reported = (*BATTLES, Battle(-10, _at(4, 58), True, attack_ended_at=_at(4, 59)))
    assert _total(battles=reported, end_before_loss=5010, end_proof=proof) == ResetTotal(
        5010, True, "calculated_end", _after(30))


def test_proven_once_a_reading_that_fits_nothing_takes_nothing_back() -> None:
    """Proven at 05:04 before the loss landed; at 05:18 a reading 37 below
    what the loss rule expects contradicts the day, not its Reset total."""
    proving, misfit = Reading(_after(4), 5020), Reading(_after(18), 4983)
    assert _decide(proving, misfit).outcome == "contradicted"
    assert _total(proving, misfit, state="Inconsistent",
                  failures=("trophy_equation_mismatch",)) == ResetTotal(
        5020, True, "calculated_end", _after(4))
    # Read the other way round, the misfit comes first and nothing is proven.
    assert _total(Reading(_after(4), 4983), Reading(_after(18), 4990), state="Inconsistent",
                  failures=("trophy_equation_mismatch",)).proven is False


def test_proven_once_a_lagging_reading_takes_nothing_back() -> None:
    """A new-day attack for 30 lands from 05:10; at 06:00 the profile still
    lacks its credit, the game's attacker-profile lag."""
    ledger = {**LEDGER, "new_day_effects": (Effect("new", 30, _after(10)),)}
    readings = (Reading(_after(4), 5020), Reading(_after(60), 4990))
    proof = {"readings": readings, "reset_at": RESET, "start_proven": True, **ledger}
    assert reading_rule.decide(readings, reset_at=RESET, end_before_loss=5020, start_proven=True,
                               **ledger).lagged == ("new",)
    assert _total(state="Partial", failures=("end_reading_unverified",), end_proof=proof) == (
        ResetTotal(5020, True, "calculated_end", _after(4)))


def test_proven_once_whatever_the_order_the_day_is_judged_in() -> None:
    readings = [Reading(_after(4), 5020), Reading(_after(18), 4983), Reading(_after(40), 4990)]
    assert _total(*readings) == _total(*reversed(readings)) == _total(*readings[:1])


def test_a_seasons_last_day_without_its_official_final_keeps_the_older_rule() -> None:
    assert _total(Reading(_after(4), 5020), season_end=True) == ResetTotal(
        4990, False, "calculated_end")
    old = Reading(_at(4, 30), 5050, requested_at=_at(4, 30))
    assert _total(state="Inconsistent", season_end=True, old=old) == ResetTotal(
        5030, False, "reading_side", reading_at=_at(4, 30))


def test_an_unproven_reading_side_counts_the_battles_its_reading_lags() -> None:
    """The 04:55 reading still shows 5,040, the total before the defense
    reported at 04:40, as the day's own end, 5,020, shows: unproven, the
    reading side counts that defense. A reading after the Reset that proves
    5,040 proves the reading showed it after all."""
    contradicted: dict[str, Any] = {
        "state": "Inconsistent", "failures": ("trophy_equation_mismatch",),
        "old": Reading(_at(4, 55), 5040, requested_at=_at(4, 55))}
    assert _total(**contradicted) == ResetTotal(5020, False, "reading_side",
                                                reading_at=_at(4, 55))
    proving = Reading(_after(44), 5010)
    assert _total(proving, **contradicted) == ResetTotal(5040, True, "reading_side",
                                                         _after(44), _at(4, 55))
    # Read 41 minutes after the attack, the reading lagging it is not counted.
    late = Reading(_at(4, 52), 4980, requested_at=_at(4, 52))
    assert _total(**{**contradicted, "old": late}) == ResetTotal(4980, False, "reading_side",
                                                                 reading_at=_at(4, 52))
