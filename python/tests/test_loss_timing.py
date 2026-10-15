"""The automatic defense loss: what a first Legend day is charged, and which
way the day before's loss went at the next day's start.

The default day starts at 6,000 with a +20 attack and a 10-trophy defense;
the day before's 2 defenses lost 20, so 7 missing defenses cost 10 each.
"""

from __future__ import annotations

from datetime import timedelta

from test_reconciliation import DAY, _input

from clashlens.reading_rule import Reading
from clashlens.reconciliation import (
    BattleContribution,
    PreviousRankedDay,
    reconcile_ranked_day,
)

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
    # 6 missing defenses at its own average of 10: 22 of 28 such days lost
    # nothing and 6 lost their average once or twice; none lost all 6.
    kept = _first_played_day(5000)
    once = _first_played_day(4990)
    all_six = _first_played_day(4940)

    assert (kept.state, kept.automatic_defense_loss, kept.next_start_trophies) == (
        "Complete", None, 5000)
    assert (once.state, once.automatic_defense_loss, once.next_start_trophies) == (
        "Complete", 10, 4990)
    assert all_six.state == "Inconsistent"


def test_day_1_with_fewer_attacks_loses_8_less_its_defenses_or_nothing() -> None:
    # 24 of 34 such Day 1s lost (8 - defenses), 10 nothing: a reading decides.
    charged = _first_played_day(4940, season_first_day=True, start_baseline_evidence={})
    kept = _first_played_day(5000, season_first_day=True, start_baseline_evidence={})

    assert (charged.state, charged.automatic_defense_loss, charged.next_start_trophies) == (
        "Complete", 60, 4940)
    assert (kept.state, kept.automatic_defense_loss, kept.next_start_trophies) == (
        "Complete", None, 5000)


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


def test_a_next_day_keeps_the_other_start_only_when_its_readings_prove_it() -> None:
    # The 6,010 Reset reading at 05:03 fits a start of 6,000, but a new-day
    # attack of +5 at 05:01 may already show in it, so it proves nothing:
    # the day stays Inconsistent from 5,930, not Complete from 6,000.
    previous = PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10, automatic_loss=70,
                                 unsettled_automatic_loss=70)
    reset_at = (DAY.end + timedelta(minutes=3)).isoformat()
    result = reconcile_ranked_day(_input(
        previous_day=previous, start_baseline_id=10, next_start_trophies=6010,
        end_baseline_evidence={"profile": {"observed_at": reset_at}},
        new_day_contributions=(BattleContribution(
            "next-attack", "offense", 5, battle_timestamp=DAY.end + timedelta(minutes=1)),)))

    assert (result.state, result.start_trophies) == ("Inconsistent", 5930)


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


def test_a_stale_reading_is_set_aside_only_when_no_start_fits_without_it() -> None:
    # The day before's 30 never landed: this day starts at its 5,310 Reset
    # reading, not 5,280. Its 8 attacks gain 289 and 7 defenses lose 210,
    # the last at 04:39, so it ends at 5,389 before its own loss of 30. The
    # 05:03 Reset reading shows 5,389 and the 05:19 one 5,359. From 5,280,
    # 5,389 is the total before that defense, a profile running late, and
    # 5,359 the end before the loss: proven, from the wrong start.
    battles = tuple(
        BattleContribution(f"attack-{n}", "offense", 40 if n < 7 else 9,
                           battle_timestamp=DAY.start + timedelta(hours=1 + n))
        for n in range(8)
    ) + tuple(
        BattleContribution(f"defense-{n}", "defense", 30, battle_seconds=120,
                           battle_timestamp=DAY.end - timedelta(minutes=21 + 60 * n))
        for n in range(7)
    )
    previous = PreviousRankedDay(True, 7, 210, 0, end_baseline_id=10, automatic_loss=30,
                                 unsettled_automatic_loss=30)
    reset_at = (DAY.end + timedelta(minutes=3)).isoformat()
    result = reconcile_ranked_day(_input(
        previous_day=previous, start_trophies=5310, contributions=battles,
        next_start_trophies=5389, now=DAY.end + timedelta(hours=1),
        end_baseline_evidence={"profile": {"observed_at": reset_at}},
        readings=(Reading(DAY.end + timedelta(minutes=19), 5359),)))

    assert (result.state, result.confidence, result.start_trophies) == ("Complete", "exact", 5310)
    assert (result.final_trophies_before_reset, result.automatic_defense_loss) == (5359, 30)


def _ended_day(reset: int, next_day_start: int | None, **overrides):
    """The default day, 6,010 before its loss of 70, read at ``reset`` at
    05:03, with the next day's saved start."""
    reset_at = (DAY.end + timedelta(minutes=3)).isoformat()
    return reconcile_ranked_day(_input(**{
        "next_start_trophies": reset, "next_day_start": next_day_start,
        "end_baseline_evidence": {"profile": {"observed_at": reset_at}}, **overrides}))


def test_a_day_records_how_its_loss_went_as_the_next_days_start_shows() -> None:
    # The 6,010 reading came before the loss: the next day starts at 5,940,
    # unless that day, proven from 6,010, shows the loss never landed.
    unsettled = _ended_day(6010, None)
    never = _ended_day(6010, 6010)

    assert (unsettled.automatic_defense_loss, unsettled.next_start_trophies) == (70, 5940)
    assert (never.state, never.automatic_defense_loss, never.final_trophies_before_reset,
            never.next_start_trophies) == ("Complete", 0, 6010, 6010)
    assert never.formula_components["automatic_loss_never_landed"] == 70
    # A 6,003 reading the day's battles do not explain is taken as is, unless
    # the next day, proven from 5,933, shows the loss landed after it.
    as_read = _ended_day(6003, None)
    late = _ended_day(6003, 5933)

    assert (as_read.state, as_read.next_start_trophies) == ("Inconsistent", 6003)
    assert (late.state, late.next_start_trophies, late.unsettled_automatic_loss) == (
        "Inconsistent", 5933, 70)
    # So is the reading of a day with no start.
    unknown = _ended_day(6003, 5933, start_trophies=None)
    assert (unknown.state, unknown.next_start_trophies, unknown.unsettled_automatic_loss) == (
        "Partial", 5933, 70)
    # A next day that started some other way says nothing of this one.
    assert _ended_day(6010, 6000).next_start_trophies == 5940
