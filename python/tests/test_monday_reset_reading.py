"""A Monday day whose Reset reading fits but a later reading does not.

On 12 October 2026 four players' Sunday ended below 5,000 only after its
automatic defense loss, so the weekly raise took them back to 5,000. Each
Reset reading, taken before the loss, showed the day's end before it and fit;
a reading about 05:15 then showed one missing defense's share of the loss
taken, which fits no way the rule allows, so the day is Inconsistent. Its
saved numbers must still add up, or the database rejects the day and the
Reset's board waits for it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from test_reconciliation import DAY, _coverage, _input

from clashlens.domain import ranked_day_for
from clashlens.reading_rule import Reading
from clashlens.reconciliation import (
    BattleContribution,
    PreviousRankedDay,
    reconcile_ranked_day,
)


def _battles(lens: str, amounts: tuple[int, ...]) -> tuple[BattleContribution, ...]:
    return tuple(
        BattleContribution(f"{lens}-{n}", lens, amount, battle_timestamp=DAY.start + timedelta(hours=n + 1))
        for n, amount in enumerate(amounts)
    )


@pytest.mark.parametrize(
    ("attacks", "defenses", "reset_reading", "later_reading"),
    [
        # Player 41426: 6 defenses lose 183, so 2 missing cost 30 each.
        ((31, 40, 2, 31, 40, 40), (40, 40, 40, 40, 0, 23), 5001, 4971),
        # Player 39803153: 3 defenses lose 61, so 5 missing cost 20 each.
        ((26, 40, 29, 40), (30, 31, 0), 5074, 5054),
        # Player 29465: 1 defense loses 30, so 7 missing cost 30 each.
        ((40, 1), (30,), 5011, 4981),
        # Player 32707: 3 defenses lose 102, so 5 missing cost 34 each.
        ((19, 40, 40, 40), (40, 31, 31), 5037, 5003),
    ],
)
def test_monday_day_contradicted_after_a_fitting_reset_reading_still_adds_up(
    attacks: tuple[int, ...], defenses: tuple[int, ...],
    reset_reading: int, later_reading: int,
) -> None:
    # The Season rule starts the day at 5,000; the day before had no battles.
    # The Reset reading is the day's end before its automatic loss.
    assert reset_reading == 5000 + sum(attacks) - sum(defenses)
    result = reconcile_ranked_day(_input(
        start_trophies=5000,
        next_start_trophies=reset_reading,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 0, 0, 0, battles_known=True),
        contributions=_battles("offense", attacks) + _battles("defense", defenses),
        start_baseline_evidence={"start_trophies_source": "season_rule"},
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
        readings=(Reading(DAY.end + timedelta(minutes=15), later_reading),),
    ))

    final = result.final_trophies_before_reset
    assert final is not None and final < 5000 < reset_reading
    assert result.state == "Inconsistent"
    # The Reset reading showed the end before the loss: the next day starts
    # at the raised 5,000 and the reading's difference is its correction.
    assert result.next_start_trophies == result.expected_next_start_trophies == 5000
    assert result.formula_components["next_start_reading_correction"] == 5000 - reset_reading
    assert result.input_evidence["end_reading"]["trophies"] == later_reading
    # What ranked_day_versions_formula_v2_check requires of the saved day.
    assert result.net_trophy_change == final - result.start_trophies
    assert result.expected_next_start_trophies == final + result.boundary_adjustment
    assert result.observed_boundary_adjustment == result.next_start_trophies - final
    assert result.unexplained_residual == (
        result.next_start_trophies - result.expected_next_start_trophies
    )


def test_monday_read_before_the_raise_starts_at_5000_after_a_partial_sunday() -> None:
    # Sunday's battles are all known and end at 4,940, but no reading proved
    # it, so it is Partial; the raise takes it to 5,000. Monday's Reset
    # reading of 4,940 came before the raise landed: Monday starts at 5,000.
    # A player dropped to Legend II at the Reset gets no raise.
    monday = ranked_day_for(DAY.start - timedelta(hours=12))
    assert monday.start.weekday() == 0

    def start(expected_next_start: int) -> int | None:
        return reconcile_ranked_day(_input(
            ranked_day=monday, now=monday.end + timedelta(minutes=1),
            coverage_observations=tuple(
                replace(item, observed_at=item.observed_at - timedelta(days=1))
                for item in _coverage()),
            start_trophies=4940, next_start_trophies=5000, contributions=(),
            previous_day=PreviousRankedDay(
                False, 8, 240, 0, state="Partial", final_trophies=4940,
                expected_next_start=expected_next_start, battles_known=True),
        )).start_trophies

    assert start(5000) == 5000
    assert start(4940) == 4940


@pytest.mark.parametrize(("boundary", "proven"), [("weekly", True), ("season", False)])
def test_only_a_weekly_reset_reading_before_the_raise_proves_the_end(
    boundary: str, proven: bool,
) -> None:
    # An attack of 40 and 8 defenses of 30 end the day at 4,800, with no
    # automatic loss, and the Reset reading at 05:04 shows 4,800. Before a
    # weekly raise it proves that end, the Daily board's total. Before a
    # Season's reset it proves nothing until the official Season total.
    result = reconcile_ranked_day(_input(
        start_trophies=5000,
        next_start_trophies=4800,
        boundary_kind=boundary,
        previous_day=PreviousRankedDay(True, 0, 0, 0, battles_known=True),
        contributions=_battles("offense", (40,)) + _battles("defense", (30,) * 8),
        start_baseline_evidence={"start_trophies_source": "season_rule"},
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
    ))

    assert (result.final_trophies_before_reset, result.next_start_trophies) == (4800, 5000)
    assert result.input_evidence.get("end_reading") == ({
        "read_at": (DAY.end + timedelta(minutes=4)).isoformat(), "trophies": 4800,
        "outcome": "verified", "exact": False, "new_day_change": 0, "clean": True,
    } if proven else None)


@pytest.mark.parametrize(("reset_reading", "later", "charged"), [
    (4660, (), True), (4900, (4660,), True), (5000, (), False)])
def test_a_reading_before_the_raise_shows_a_quiet_sunday_charged(
    reset_reading: int, later: tuple[int, ...], charged: bool,
) -> None:
    # Saturday's 8 defenses lost 240, so Sunday, at 4,900 with no battle,
    # can be charged 240 for its 8 empty defense slots. The Reset reading at
    # 05:04, or one at 05:10 after a Reset reading taken before the charge,
    # shows 4,660 before the raise: the charge landed. A reading at 5,000
    # shows only the raise, which fits a charge or none alike.
    result = reconcile_ranked_day(_input(
        start_trophies=4900,
        next_start_trophies=reset_reading,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        contributions=(),
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
        readings=(*(Reading(DAY.end + timedelta(minutes=10), trophies) for trophies in later),
                  Reading(DAY.end + timedelta(minutes=20), 5000)),
    ))

    assert result.state == "Complete"
    assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (
        (4660, 240) if charged else (4900, None))
    assert result.automatic_defense_evidence_state == ("calculated" if charged else "not_applicable")
    assert (result.next_start_trophies, result.expected_next_start_trophies) == (5000, 5000)
    assert result.boundary_adjustment == 5000 - result.final_trophies_before_reset
