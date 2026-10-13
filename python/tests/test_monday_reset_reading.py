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
    # Sunday's eight attacks for 320 and eight defenses losing 280 take it
    # from 4,900 to 4,940, all known, but its Reset battle log failed, so it
    # is Partial; the raise takes it to 5,000. Monday's 05:13 Reset reading
    # came before the raise landed: 4,940, or 5,020 once it shows two Monday
    # attacks of 40. Monday starts at 5,000 either way. A player dropped to
    # Legend II at the Reset gets no raise.
    monday = ranked_day_for(DAY.start - timedelta(hours=12))
    assert monday.start.weekday() == 0

    def start(expected_next_start: int, attacks: int = 0) -> int | None:
        return reconcile_ranked_day(_input(
            ranked_day=monday, now=monday.end + timedelta(minutes=1),
            coverage_observations=tuple(
                replace(item, observed_at=item.observed_at - timedelta(days=1))
                for item in _coverage()),
            start_trophies=4940 + 40 * attacks, next_start_trophies=5000 + 40 * attacks,
            contributions=tuple(
                BattleContribution(f"monday-{n}", "offense", 40,
                                   battle_timestamp=monday.start + timedelta(minutes=5 + n))
                for n in range(attacks)),
            previous_day=PreviousRankedDay(
                False, 8, 280, 0, state="Partial", final_trophies=4940,
                expected_next_start=expected_next_start, battles_known=True),
        )).start_trophies

    assert start(5000) == 5000
    assert start(5000, attacks=2) == 5000
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


@pytest.mark.parametrize(("reset_reading", "charged"), [(4960, True), (5000, False)])
def test_a_reading_before_the_raise_charges_a_quiet_sunday_from_above_5000(
    reset_reading: int, charged: bool,
) -> None:
    # Sunday, at 5,200 with no battle, can be charged 240 for its 8 empty
    # defense slots. The Reset reading at 05:04 shows 4,960 before the raise,
    # then one at 05:20 shows 5,000: the charge landed and took Sunday below
    # 5,000, so Monday starts at 5,000. Readings of 5,000 alone show only the
    # raise, which fits the charge crossing 5,000 and proves nothing.
    result = reconcile_ranked_day(_input(
        start_trophies=5200,
        next_start_trophies=reset_reading,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        contributions=(),
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
        readings=(Reading(DAY.end + timedelta(minutes=20), 5000),),
    ))

    if not charged:
        assert (result.state, result.failure_reasons) == ("Partial", ("end_reading_unverified",))
        assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (5200, None)
        return
    assert result.state == "Complete"
    assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (4960, 240)
    assert result.automatic_defense_evidence_state == "calculated"
    assert (result.next_start_trophies, result.expected_next_start_trophies) == (5000, 5000)
    assert result.boundary_adjustment == 40


@pytest.mark.parametrize(("boundary", "proven"), [("weekly", True), ("season", False)])
def test_a_later_reading_before_the_raise_proves_the_end(boundary: str, proven: bool) -> None:
    # 8 defenses of 30 and an attack of 40 reported at 04:54:30 end the day
    # at 4,800. The Reset reading of 4,800 at 05:04 may or may not show the
    # attack, so it proves nothing; one at 05:20 shows 4,800 before the
    # weekly raise and proves the end, the Daily board's total; one at 05:40
    # shows the raise to 5,000, which proves nothing.
    result = reconcile_ranked_day(_input(
        start_trophies=5000,
        next_start_trophies=4800,
        boundary_kind=boundary,
        previous_day=PreviousRankedDay(True, 0, 0, 0, battles_known=True),
        contributions=_battles("defense", (30,) * 8) + (BattleContribution(
            "offense-late", "offense", 40, battle_timestamp=DAY.end - timedelta(seconds=330)),),
        start_baseline_evidence={"start_trophies_source": "season_rule"},
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
        readings=(Reading(DAY.end + timedelta(minutes=20), 4800), Reading(DAY.end + timedelta(minutes=40), 5000)),
    ))

    assert (result.final_trophies_before_reset, result.next_start_trophies) == (4800, 5000)
    assert result.input_evidence.get("end_reading") == ({
        "read_at": (DAY.end + timedelta(minutes=20)).isoformat(), "trophies": 4800,
        "outcome": "verified", "exact": False, "new_day_change": 0, "clean": True,
    } if proven else None)


@pytest.mark.parametrize(("sunday", "monday_battles"), [
    (5190, (("offense", 40), ("offense", 40), ("defense", 30))),
    (5340, (("defense", 40), ("defense", 30), ("defense", 30))),
])
def test_a_5000_reading_before_the_raise_takes_no_charge_even_after_new_day_battles(
    sunday: int, monday_battles: tuple[tuple[str, int], ...],
) -> None:
    # Sunday, at 5,190 or 5,340 with no battle, can be charged 240 for its 8
    # empty defense slots; its Reset reading at 05:02 shows that total.
    # Monday's battles have landed by 05:30, when a reading shows 5,000:
    # 4,950 plus Monday's 50, or 5,100 less Monday's 100. A 5,000 reading
    # never proves the charge, so Sunday stays uncertain and uncharged.
    monday = tuple(
        BattleContribution(f"monday-{n}", lens, amount, battle_timestamp=DAY.end + timedelta(minutes=6 + n))
        for n, (lens, amount) in enumerate(monday_battles))
    result = reconcile_ranked_day(_input(
        start_trophies=sunday,
        next_start_trophies=sunday,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        contributions=(),
        new_day_contributions=monday,
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=2)).isoformat()}},
        readings=(Reading(DAY.end + timedelta(minutes=30), 5000),),
    ))

    assert (result.state, result.failure_reasons) == ("Partial", ("end_reading_unverified",))
    assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (sunday, None)
    assert result.automatic_defense_evidence_state == "not_applicable"
    assert result.input_evidence["end_reading"]["clean"] is False


def test_a_charge_shown_before_the_raise_stands_on_the_day_s_earlier_readings() -> None:
    # Sunday starts at 4,960 and six attacks of 40 take it to 5,200, which a
    # reading at 17:00 shows. It can be charged 240 for its 8 empty defense
    # slots. Monday reads 4,960 at 05:04, then 5,000: alone, 4,960 could be
    # the six attacks not credited yet, but the 17:00 reading showed them,
    # so the charge landed and Monday starts at 5,000.
    result = reconcile_ranked_day(_input(
        start_trophies=4960,
        next_start_trophies=4960,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        contributions=_battles("offense", (40,) * 6),
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=4)).isoformat()}},
        readings=(Reading(DAY.start + timedelta(hours=12), 5200), Reading(DAY.end + timedelta(minutes=20), 5000)),
    ))

    assert result.state == "Complete"
    assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (4960, 240)
    assert result.automatic_defense_evidence_state == "calculated"
    assert (result.next_start_trophies, result.expected_next_start_trophies) == (5000, 5000)
    assert result.input_evidence["end_reading"]["trophies"] == 4960


def test_a_reading_that_may_show_an_unreported_defense_proves_no_end() -> None:
    # Sunday ends at 4,900 with no battle and can be charged 240. A Monday
    # defense of 40 at 05:01 that only the opponent has reported may show in
    # any reading from then: the Reset reading of 4,860 at 05:13 fits
    # Sunday's end less that defense, but it proves nothing.
    result = reconcile_ranked_day(_input(
        start_trophies=4900,
        next_start_trophies=4860,
        boundary_kind="weekly",
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        contributions=(),
        new_day_contributions=(BattleContribution(
            "monday-defense", "defense", 40, battle_timestamp=DAY.end + timedelta(minutes=1)),),
        first_unshown_report=DAY.end + timedelta(minutes=1),
        end_baseline_evidence={"profile": {"observed_at": (DAY.end + timedelta(minutes=13)).isoformat()}},
    ))

    assert (result.final_trophies_before_reset, result.next_start_trophies) == (4900, 5000)
    assert result.input_evidence.get("end_reading") is None
