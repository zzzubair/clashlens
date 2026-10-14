"""The automatic defense loss: what a first Legend day is charged, and which
way the day before's loss went at the next day's start.

The default day starts at 6,000 with a +20 attack and a 10-trophy defense;
the day before's 2 defenses lost 20, so 7 missing defenses cost 10 each.
"""

from __future__ import annotations

from datetime import timedelta

from test_reconciliation import DAY, _input

from clashlens.reading_rule import Reading
from clashlens.reconciliation import BattleContribution, PreviousRankedDay, reconcile_ranked_day

SEASON_RULE = {"start_trophies_source": "season_rule"}
TWO_DEFENSES = (
    BattleContribution("attack-1", "offense", 20),
    BattleContribution("defense-1", "defense", 10),
    BattleContribution("defense-2", "defense", 10),
)


def _first_played_day(later: int, **overrides):
    """From 5,000 by the Season rule: one attack of 20 and two defenses of 10
    end it at 5,000, read so at the Reset and at ``later`` at 05:20."""
    return reconcile_ranked_day(_input(**{
        "start_trophies": 5000, "start_baseline_evidence": SEASON_RULE,
        "contributions": TWO_DEFENSES, "next_start_trophies": 5000,
        "readings": (Reading(DAY.end + timedelta(minutes=20), later),), **overrides}))


def test_a_first_played_day_with_fewer_attacks_is_charged_only_as_a_reading_shows() -> None:
    # 6 missing defenses at its own average of 10 may cost 60, or nothing.
    kept = _first_played_day(5000)
    charged = _first_played_day(4940)

    assert (kept.state, kept.automatic_defense_loss, kept.next_start_trophies) == (
        "Complete", None, 5000)
    assert (charged.state, charged.automatic_defense_loss, charged.next_start_trophies) == (
        "Complete", 60, 4940)
    # Day 1 still loses (8 - defenses), as 24 of 34 such days did.
    day_1 = _first_played_day(4940, season_first_day=True, start_baseline_evidence={})
    assert (day_1.state, day_1.automatic_defense_loss) == ("Complete", 60)


def _next_day(previous: PreviousRankedDay, *readings: Reading, end_reading: int):
    """A day whose 6,000 Reset reading ended a day with an automatic loss of
    70. Its battles, shown from its start, add 10, before its own loss of
    70."""
    return reconcile_ranked_day(_input(
        previous_day=previous, start_baseline_id=10,
        next_start_trophies=end_reading, readings=readings))


def test_a_next_day_starts_without_a_loss_that_never_landed() -> None:
    # The day before took its loss to land after its Reset reading. It never
    # did: this day's own Reset reading, before its own loss, shows 6,010.
    previous = PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                                 unsettled_automatic_loss=70)
    result = _next_day(previous, end_reading=6010)

    assert (result.state, result.start_trophies) == ("Complete", 6000)
    assert result.formula_components["start_automatic_loss_charged"] is False
    # The same day with the loss landed starts from 5,930.
    landed = _next_day(previous, end_reading=5940)
    assert (landed.state, landed.start_trophies) == ("Complete", 5930)
    assert "start_automatic_loss_charged" not in landed.formula_components


def test_a_next_day_starts_less_the_loss_of_a_day_before_that_is_not_complete() -> None:
    # The day before is Inconsistent, its battles all known. Its result
    # took the loss to land after the Reset reading, or a reading after the
    # Reset settled nothing; a reading at 05:20 shows 5,940, 5,930 and the
    # day's battles, either way.
    unsettled = PreviousRankedDay(False, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                                  unsettled_automatic_loss=70, state="Inconsistent",
                                  battles_known=True)
    unread = PreviousRankedDay(False, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                               state="Inconsistent", battles_known=True)
    reading = Reading(DAY.start + timedelta(minutes=20), 5940)

    for previous in (unsettled, unread):
        result = _next_day(previous, reading, end_reading=5870)
        assert (result.state, result.start_trophies) == ("Complete", 5930)
