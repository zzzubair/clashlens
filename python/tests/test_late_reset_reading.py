"""A Reset reading taken after the new day's first battle, read less the new
day's battles that had reached the profile (``reset_settlement``).

The day starts at 6,000 with a +20 attack, a 10-trophy defense and a 70
automatic loss, so it ends on 5,940. The profile is read at 05:41 after a
new-day +30 attack and a 15-trophy defense; the Reset battle log just after.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from test_reconciliation import DAY, _coverage, _input

from clashlens import domain
from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    BattleContribution,
    LateEndReading,
    PreviousRankedDay,
    reconcile_ranked_day,
)

READ_AT = DAY.end + timedelta(minutes=41)
DAY_BATTLES = (
    BattleContribution("attack-1", "offense", 20, battle_timestamp=DAY.start + timedelta(hours=2)),
    BattleContribution("defense-1", "defense", 10, battle_timestamp=DAY.start + timedelta(hours=3),
                       battle_seconds=120),
)
NEW_DAY = (
    # Lands 05:24 and 05:34, both before the reading.
    BattleContribution("next-attack", "offense", 30, battle_timestamp=DAY.end + timedelta(minutes=20)),
    BattleContribution("next-defense", "defense", 15, battle_timestamp=DAY.end + timedelta(minutes=30),
                       battle_seconds=120),
    # Reported after the reading: not in it.
    BattleContribution("later-attack", "offense", 40, battle_timestamp=READ_AT + timedelta(minutes=10)),
)


def late_day(reading: int = 5955, new_day=NEW_DAY, log_after: bool = True, **overrides):
    first, middle, last = _coverage()
    values = {
        "next_start_trophies": None,
        "end_baseline_complete": False,
        "end_baseline_evidence": {"failure_reasons": ["profile_after_first_event"]},
        "coverage_observations": (first, middle, replace(
            last, observed_at=READ_AT + timedelta(seconds=1 if log_after else -60))),
        "contributions": DAY_BATTLES,
        "late_end_reading": LateEndReading(reading, READ_AT, tuple(new_day), log_after),
    }
    return reconcile_ranked_day(_input(**{**values, **overrides}))


def test_late_reading_less_landed_new_day_battles_verifies_the_end() -> None:
    result = late_day()

    assert (result.state, result.confidence, result.failure_reasons) == ("Complete", "exact", ())
    assert result.final_trophies_before_reset == result.next_start_trophies == 5940
    late = result.input_evidence["late_end_reading"]
    assert (late["outcome"], late["basis"], late["landing_change"]) == ("verified", "landing", 15)


def test_report_times_verify_when_a_defense_had_not_landed() -> None:
    # The defense started at 05:38: by report time it is in the 05:41
    # reading, by landing (05:42) it is not, and here the game had counted it.
    defense = replace(NEW_DAY[1], battle_timestamp=READ_AT - timedelta(minutes=3))
    result = late_day(new_day=(NEW_DAY[0], defense))

    assert (result.state, result.next_start_trophies) == ("Complete", 5940)
    assert result.input_evidence["late_end_reading"]["basis"] == "report_time"


def test_a_late_reading_before_the_automatic_loss_leaves_it_unsettled() -> None:
    result = late_day(reading=5955 + 70)

    assert (result.state, result.confidence) == ("Complete", "inferred")
    assert (result.next_start_trophies, result.unsettled_automatic_loss) == (5940, 70)


def test_a_battle_near_an_unmatched_reading_leaves_the_calculated_end() -> None:
    # A +40 attack reported 3 minutes before the reading, not yet landed.
    near = (*NEW_DAY, BattleContribution(
        "close-attack", "offense", 40, battle_timestamp=READ_AT - timedelta(minutes=3)))
    result = late_day(reading=5960, new_day=near)

    assert (result.state, result.confidence) == ("Complete", "inferred")
    assert result.final_trophies_before_reset == 5940
    assert result.next_start_trophies is None
    assert result.input_evidence["late_end_reading"]["outcome"] == "calculated_not_reset_verified"


@pytest.mark.parametrize("change", [
    # A Reset battle log read before the profile can miss battles before it.
    {"log_after": False},
    # A disputed new-day battle before the reading leaves its trophies unsure.
    {"new_day": (*NEW_DAY, BattleContribution(
        "disputed", "offense", 30, disagreement=True,
        battle_timestamp=DAY.end + timedelta(minutes=12)))},
])
def test_new_day_battles_that_cannot_be_judged_leave_the_calculated_end(change) -> None:
    result = late_day(**change)

    assert (result.state, result.confidence, result.next_start_trophies) == (
        "Complete", "inferred", None)
    late = result.input_evidence["late_end_reading"]
    assert (late["outcome"], late["unclear"]) == ("calculated_not_reset_verified", True)


def test_a_disputed_battle_after_the_reading_does_not_matter() -> None:
    later = (*NEW_DAY, BattleContribution(
        "disputed-later", "offense", 30, disagreement=True,
        battle_timestamp=READ_AT + timedelta(minutes=30)))
    assert late_day(reading=5956, new_day=later).state == "Inconsistent"
    assert late_day(new_day=later).state == "Complete"


def test_a_late_reading_nothing_explains_makes_the_day_inconsistent() -> None:
    result = late_day(reading=5956)

    assert result.state == "Inconsistent"
    assert "trophy_equation_mismatch" in result.failure_reasons
    assert result.input_evidence["late_end_reading"]["outcome"] == "contradicted"


def test_a_day_with_no_used_defense_slot_is_never_calculated() -> None:
    # Its automatic loss, for all 8 slots or none, only a reading shows.
    near = (*NEW_DAY, BattleContribution(
        "close-attack", "offense", 40, battle_timestamp=READ_AT - timedelta(minutes=3)))
    result = late_day(reading=5960, new_day=near, contributions=DAY_BATTLES[:1])

    assert result.state == "Partial"
    assert result.failure_reasons == ("missing_end_baseline",)
    assert "late_end_reading" not in result.input_evidence


def test_rejecting_late_readings_leaves_the_day_partial(monkeypatch) -> None:
    monkeypatch.setattr(domain, "LATE_RESET_READING", "reject")
    result = late_day()

    assert (result.state, result.failure_reasons) == ("Partial", ("missing_end_baseline",))


def test_the_next_day_starts_from_a_verified_late_reading() -> None:
    next_day = ranked_day_for(DAY.end)

    def start(previous: PreviousRankedDay):
        return reconcile_ranked_day(_input(
            ranked_day=next_day, now=next_day.end + timedelta(minutes=1),
            start_baseline_id=11, start_trophies=None, start_baseline_complete=False,
            previous_day=previous,
        ))

    verified = PreviousRankedDay(True, 1, 10, 0, end_baseline_id=11, late_reading_start=5940)
    result = start(verified)
    assert result.start_trophies == 5940
    assert "missing_start_baseline" not in result.failure_reasons
    # A calculated or rejected end starts nothing.
    assert "missing_start_baseline" in start(replace(verified, late_reading_start=None)).failure_reasons
