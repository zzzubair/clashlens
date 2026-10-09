"""One rule for every reading after a Legend day ends (``reading_rule``).

The day ends on 5,940 before its automatic loss of 70: start 6,000, a +20
attack reported at 07:00 and a 10-trophy defense started at 08:00. The Reset
is at 05:00 the next morning; the loss can land from 05:07 on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from clashlens.domain import allocate_trophies
from clashlens.reading_rule import Effect, Reading, decide

RESET = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_BATTLES = (
    Effect("attack-1", 20, RESET - timedelta(hours=22)),
    Effect("defense-1", -10, RESET - timedelta(hours=21)),
)
END_BEFORE_LOSS = 5940
LOSS = 70


def verdict(*readings: Reading, loss=(LOSS,), certain=True, new_day=(), day=DAY_BATTLES,
            start_proven=True, end=END_BEFORE_LOSS, new_day_from=None):
    return decide(
        readings, reset_at=RESET, end_before_loss=end,
        loss_candidates=loss, loss_certain=certain, day_effects=day,
        new_day_effects=new_day, new_day_from=new_day_from, start_proven=start_proven,
    )


def at(minutes: float) -> datetime:
    return RESET + timedelta(minutes=minutes)


def test_a_reset_reading_before_the_loss_confirms_the_battles_but_not_the_loss() -> None:
    result = verdict(Reading(at(2), 5940, reset_reading=True))

    assert (result.outcome, result.loss, result.exact) == ("verified", 0, False)


def test_a_reading_showing_the_loss_proves_the_day() -> None:
    result = verdict(Reading(at(2), 5940, reset_reading=True), Reading(at(20), 5870))

    assert (result.outcome, result.loss, result.exact) == ("verified", LOSS, True)
    assert result.reading is not None and result.reading.read_at == at(20)


def test_the_loss_can_show_at_any_time_after_the_reset() -> None:
    # Measured from 7 minutes 38 seconds after the Reset to 05:29, neither
    # bound official: the value says whether the loss had landed.
    assert verdict(Reading(at(2), 5870)).exact is True
    assert verdict(Reading(at(29), 5870)).exact is True
    assert verdict(Reading(at(29), 5940)).loss == 0


def test_a_clean_reading_that_fits_nothing_contradicts_the_day() -> None:
    result = verdict(Reading(at(2), 5938, reset_reading=True))

    # The residual is against the day's end after its loss, 5,870.
    assert (result.outcome, result.residual) == ("contradicted", 68)


def test_a_later_reading_settles_a_reset_reading_taken_too_early() -> None:
    # The Reset reading missed the +20 attack's credit, which should have
    # landed hours before; the 05:20 reading shows the day complete.
    result = verdict(Reading(at(2), 5920, reset_reading=True), Reading(at(20), 5870))

    assert (result.outcome, result.exact, result.earlier_contradictions) == (
        "verified", True, 1)


def test_a_clean_reading_after_an_exact_match_still_contradicts_the_day() -> None:
    result = verdict(Reading(at(20), 5870), Reading(at(40), 5880))

    assert (result.outcome, result.residual) == ("contradicted", 10)
    assert result.reading is not None and result.reading.read_at == at(40)
    # A later match outranks every contradiction before it.
    settled = verdict(Reading(at(20), 5880), Reading(at(30), 5875), Reading(at(40), 5870))
    assert (settled.outcome, settled.exact, settled.earlier_contradictions) == (
        "verified", True, 2)


def test_no_reading_after_the_first_new_day_battle_overturns_a_contradiction() -> None:
    # 05:20 fits neither 5,940 nor 5,870. A +30 attack is reported at 05:38;
    # the 05:55 reading of 5,900 fits it, but can only confirm.
    new_day = (Effect("next-attack", 30, at(38)),)
    result = verdict(Reading(at(20), 5880), Reading(at(55), 5900), new_day=new_day)

    assert (result.outcome, result.residual) == ("contradicted", 10)
    # Only the opponent's log has a defense reported at 05:10 yet: a 05:15
    # reading may show it, so it cannot contradict.
    assert verdict(Reading(at(15), 5880), new_day_from=at(10)).outcome == "unverified"


def test_a_reading_in_a_battles_landing_span_is_read_both_ways() -> None:
    late_attack = Effect("attack-late", 40, RESET - timedelta(minutes=2))
    day = (*DAY_BATTLES, late_attack)
    # The day ends on 5,980 before the loss. Read 4 minutes after the
    # attack's report, within its landing span: without the attack it fits,
    # so it confirms, but only as read one way.
    result = verdict(Reading(at(2), 5940, reset_reading=True), day=day, end=5980)

    assert (result.outcome, result.exact, result.missed) == (
        "verified", False, ("attack-late",))
    # The same guess needs a proven start.
    assert verdict(Reading(at(2), 5940, reset_reading=True), day=day, end=5980,
                   start_proven=False).outcome == "unverified"
    # With the attack shown, the reading is clean and exact once the loss landed.
    assert verdict(Reading(at(20), 5910), day=day, end=5980).exact is True


def test_a_clean_later_reading_disproves_a_reading_read_one_way() -> None:
    late_attack = Effect("attack-late", 40, RESET - timedelta(minutes=2))
    result = verdict(
        Reading(at(2), 5940, reset_reading=True), Reading(at(20), 5900),
        day=(*DAY_BATTLES, late_attack), end=5980,
    )

    assert result.outcome == "contradicted"


def test_a_late_reading_less_the_new_day_battles_it_shows_verifies_the_day() -> None:
    # Read at 05:41 after a new-day +30 attack (05:20) and a 15-trophy
    # defense started at 05:26 lasting 2 minutes, both landed; a +40 attack
    # reported after the reading is not in it.
    new_day = (
        Effect("next-attack", 30, at(20)),
        Effect("next-defense", -15, at(28)),
        Effect("later-attack", 40, at(51)),
    )
    result = verdict(Reading(at(41), 5885), new_day=new_day)

    assert (result.outcome, result.exact, result.new_day_change) == ("verified", True, 15)
    # 5,885 is 5,940 - 70 + 30 - 15. One trophy off, it may show a new-day
    # battle not known yet, so it settles nothing.
    assert verdict(Reading(at(41), 5886), new_day=new_day).outcome == "unverified"
    # Before the first new-day battle it can still contradict.
    assert verdict(Reading(at(15), 5871), new_day=new_day).outcome == "contradicted"


def test_a_new_day_battle_in_flight_at_the_reading_leaves_it_read_both_ways() -> None:
    new_day = (Effect("next-attack", 30, at(38)),)
    # Read 3 minutes after the attack's report: with or without it.
    assert verdict(Reading(at(41), 5870), new_day=new_day).exact is False
    assert verdict(Reading(at(41), 5900), new_day=new_day).exact is False
    # Neither fits: not evidence on its own.
    assert verdict(Reading(at(41), 5880), new_day=new_day).outcome == "unverified"


def test_a_disputed_battle_in_flight_makes_the_reading_unjudgeable() -> None:
    new_day = (Effect("disputed", 30, at(20), disputed=True),)
    assert verdict(Reading(at(41), 5900), new_day=new_day).outcome == "unverified"
    # Reported after the reading, it does not matter.
    assert verdict(Reading(at(15), 5870), new_day=new_day).outcome == "verified"


def test_a_season_zero_reading_can_confirm_but_never_contradict() -> None:
    assert verdict(Reading(at(20), 5870, confirm_only=True)).outcome == "verified"
    assert verdict(Reading(at(20), 5000, confirm_only=True)).outcome == "unverified"
    # Its match outranks a Reset reading missing the attack's 20 before it.
    stale = Reading(at(2), 5920, reset_reading=True)
    result = verdict(stale, Reading(at(20), 5870, confirm_only=True))
    assert (result.outcome, result.earlier_contradictions) == ("verified", 1)


def test_a_day_with_no_used_defense_slot_is_charged_only_when_a_reading_shows_it() -> None:
    # The charge for all 8 slots, 304, is possible but not certain.
    charged = verdict(Reading(at(20), 5636), loss=(304,), certain=False)
    kept = verdict(Reading(at(20), 5940), loss=(304,), certain=False)
    early = verdict(Reading(at(2), 5940, reset_reading=True), loss=(304,), certain=False)

    assert (charged.loss, charged.exact) == (304, True)
    assert (kept.loss, kept.exact) == (0, True)
    assert (early.outcome, early.loss, early.exact) == ("verified", 0, True)
    # A later reading showing the charge outranks an earlier one without it.
    later = verdict(Reading(at(2), 5940, reset_reading=True), Reading(at(20), 5636),
                    loss=(304,), certain=False)
    assert (later.loss, later.exact) == (304, True)


def test_no_confirm_only_reading_undoes_a_charge_a_reading_showed() -> None:
    # 05:20 shows the 304 charge. At 13:00, after battles no saved log holds
    # yet, the profile is back at 5,940: it can only confirm, and the charge
    # cannot be undone, nor by one confirm-only reading after another.
    charged = Reading(at(20), 5636)
    uncharged = Reading(at(480), 5940, confirm_only=True)
    for readings in ((charged, uncharged),
                     (replace(charged, confirm_only=True), uncharged)):
        result = verdict(*readings, loss=(304,), certain=False)
        assert (result.outcome, result.loss, result.exact) == ("verified", 304, True)
        assert result.reading.read_at == charged.read_at


def test_a_later_trustworthy_reading_corrects_an_earlier_charge() -> None:
    # A stale 05:02 reading missing the day's 304 of credits matches the 304
    # charge; a covered 05:20 reading before any new-day battle shows the
    # credits landed and no charge.
    result = verdict(Reading(at(2), 5636), Reading(at(20), 5940), loss=(304,),
                     certain=False)

    assert (result.outcome, result.loss, result.exact) == ("verified", 0, True)
    assert result.reading is not None and result.reading.read_at == at(20)


def test_a_zero_star_attack_gains_the_attacker_what_the_defender_does_not_lose() -> None:
    # 0 stars at 10%: the attacker gains 1, the defender loses nothing, and
    # each player's day takes their own amount.
    allocation = allocate_trophies(0, 10)
    assert (allocation.attacker_gain, allocation.defender_loss) == (1, 0)
    at_two = RESET - timedelta(hours=2)
    attacker = verdict(Reading(at(20), 5871), end=5941, day=(
        *DAY_BATTLES, Effect("zero-star", allocation.attacker_gain, at_two)))
    defender = verdict(Reading(at(20), 5870), day=(
        *DAY_BATTLES, Effect("zero-star", -allocation.defender_loss, at_two)))

    assert (attacker.outcome, attacker.exact) == ("verified", True)
    assert (defender.outcome, defender.exact) == ("verified", True)


def test_no_reading_leaves_the_day_unverified() -> None:
    assert verdict().outcome == "unverified"
