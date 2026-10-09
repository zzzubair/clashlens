"""One rule for every reading of a Legend day (``reading_rule``).

The day ends on 5,940 before its automatic loss of 70: start 6,000, a +20
attack reported at 07:00 and a 10-trophy defense started at 08:00. The Reset
is at 05:00 the next morning; the loss can land from 05:07 on.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import test_reconciliation

from clashlens.domain import allocate_trophies
from clashlens.reading_rule import Effect, Reading, contradiction_during_day, decide
from clashlens.reconciliation import (
    BattleContribution,
    PreviousRankedDay,
    reconcile_ranked_day,
)

RESET = datetime(2026, 8, 5, 5, tzinfo=UTC)
DAY_BATTLES = (
    Effect("attack-1", 20, RESET - timedelta(hours=22)),
    Effect("defense-1", -10, RESET - timedelta(hours=21)),
)
END_BEFORE_LOSS = 5940
LOSS = 70


def verdict(*readings: Reading, loss=(LOSS,), certain=True, new_day=(), day=DAY_BATTLES,
            start_proven=True, end=END_BEFORE_LOSS, unknown_from=None):
    return decide(
        readings, reset_at=RESET, end_before_loss=end,
        loss_candidates=loss, loss_certain=certain, day_effects=day,
        new_day_effects=new_day, unknown_from=unknown_from, start_proven=start_proven,
    )


def at(minutes: float) -> datetime:
    return RESET + timedelta(minutes=minutes)


def test_a_reset_reading_before_the_loss_confirms_the_battles_but_not_the_loss() -> None:
    result = verdict(Reading(at(2), 5940, reset_reading=True))

    assert (result.outcome, result.loss, result.exact) == ("verified", 0, False)
    # It still proves the day's end before the loss, the Daily board's number.
    assert result.clean is True


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


def test_a_reading_short_of_some_attack_gains_waits_for_a_later_match() -> None:
    # The Reset reading lacks the +20 attack's gain, reported hours before:
    # the game had not credited the attacker's profile yet. It is read like a
    # reading with that attack in flight, and the 05:20 reading decides.
    result = verdict(Reading(at(2), 5920, reset_reading=True), Reading(at(20), 5870))

    assert (result.outcome, result.exact, result.clean) == ("verified", True, True)
    assert result.reading is not None and result.reading.read_at == at(20)
    assert result.lagged == ("attack-1",)
    # Alone, it confirms nothing and contradicts nothing.
    alone = verdict(Reading(at(2), 5920, reset_reading=True))
    assert (alone.outcome, alone.lagged) == ("unverified", ("attack-1",))


def test_a_shortfall_no_attack_gains_explain_stays_a_disagreement() -> None:
    # 1 trophy short is no set of the day's attack gains (only +20), so no
    # later match erases it.
    result = verdict(Reading(at(2), 5939, reset_reading=True), Reading(at(20), 5870))

    assert (result.outcome, result.residual) == ("contradicted", 69)
    assert result.reading is not None and result.reading.read_at == at(2)


def test_only_the_latest_attacks_can_lag() -> None:
    # The profile catches up when the player stops attacking, so it can lack
    # the +30 attack at 09:00, or both, but not the earlier +20 alone.
    day = (*DAY_BATTLES, Effect("attack-2", 30, RESET - timedelta(hours=20)))
    lag_30 = verdict(Reading(at(2), 5940, reset_reading=True), Reading(at(20), 5900),
                     day=day, end=5970)
    lag_50 = verdict(Reading(at(2), 5920, reset_reading=True), Reading(at(20), 5900),
                     day=day, end=5970)
    earlier = verdict(Reading(at(2), 5950, reset_reading=True), Reading(at(20), 5900),
                      day=day, end=5970)

    assert (lag_30.outcome, lag_30.lagged) == ("verified", ("attack-2",))
    assert (lag_50.outcome, lag_50.lagged) == ("verified", ("attack-1", "attack-2"))
    assert (earlier.outcome, earlier.residual) == ("contradicted", 50)


def test_a_clean_reading_after_an_exact_match_still_contradicts_the_day() -> None:
    result = verdict(Reading(at(20), 5870), Reading(at(40), 5880))

    assert (result.outcome, result.residual) == ("contradicted", 10)
    assert result.reading is not None and result.reading.read_at == at(40)
    # Readings of 5,880 and then 5,870 against 5,870: the first decides.
    settled = verdict(Reading(at(20), 5880), Reading(at(30), 5875), Reading(at(40), 5870))
    assert (settled.outcome, settled.residual) == ("contradicted", 10)


def test_a_reading_after_new_day_battles_the_logs_hold_still_contradicts() -> None:
    # A +30 attack is reported at 05:38; continuous logs hold it, so a 05:55
    # reading of 5,910 fits nothing the ledger allows: neither 5,940 + 30 nor
    # 5,870 + 30.
    new_day = (Effect("next-attack", 30, at(38)),)
    result = verdict(Reading(at(55), 5910), new_day=new_day)

    assert (result.outcome, result.residual) == ("contradicted", 10)
    # Only the opponent's log has a defense reported at 05:10 yet: a 05:15
    # reading may show it, so it cannot contradict.
    assert verdict(Reading(at(15), 5880), unknown_from=at(10)).outcome == "unverified"


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
    # 5,885 is 5,940 - 70 + 30 - 15. One trophy off, with the logs holding
    # every battle, it contradicts, as it does before the first new-day battle.
    assert verdict(Reading(at(41), 5886), new_day=new_day).outcome == "contradicted"
    assert verdict(Reading(at(15), 5871), new_day=new_day).outcome == "contradicted"


def test_a_new_day_battle_in_flight_at_the_reading_leaves_it_read_both_ways() -> None:
    new_day = (Effect("next-attack", 30, at(38)),)
    # Read 3 minutes after the attack's report: with or without it.
    assert verdict(Reading(at(41), 5870), new_day=new_day).exact is False
    assert verdict(Reading(at(41), 5900), new_day=new_day).exact is False
    # Neither fits, with or without the attack, before or after the loss, nor
    # short the +20 attack's late credit: a disagreement, which a later match
    # does not erase.
    assert verdict(Reading(at(41), 5885), new_day=new_day).outcome == "contradicted"
    result = verdict(Reading(at(41), 5885), Reading(at(60), 5900), new_day=new_day)
    assert (result.outcome, result.reading.read_at) == ("contradicted", at(41))


def test_a_disputed_battle_in_flight_makes_the_reading_unjudgeable() -> None:
    new_day = (Effect("disputed", 30, at(20), disputed=True),)
    assert verdict(Reading(at(41), 5900), new_day=new_day).outcome == "unverified"
    # Reported after the reading, it does not matter.
    assert verdict(Reading(at(15), 5870), new_day=new_day).outcome == "verified"


def test_a_season_zero_reading_can_confirm_but_never_contradict() -> None:
    assert verdict(Reading(at(20), 5870, confirm_only=True)).outcome == "verified"
    assert verdict(Reading(at(20), 5000, confirm_only=True)).outcome == "unverified"
    # Nor can its match overturn a trusted reading's contradiction.
    result = verdict(Reading(at(20), 5880), Reading(at(40), 5870, confirm_only=True))
    assert (result.outcome, result.residual) == ("contradicted", 10)


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


def test_a_confirm_only_reading_never_settles_that_a_possible_loss_missed() -> None:
    # With no defense slot used the day may be charged 304, which can land
    # after a reading: a Season 0 profile showing none proves nothing.
    kept = verdict(Reading(at(2), 5940, confirm_only=True), loss=(304,), certain=False)
    charged = verdict(Reading(at(20), 5636, confirm_only=True), loss=(304,), certain=False)

    assert kept.outcome == "unverified"
    assert (charged.outcome, charged.loss, charged.exact) == ("verified", 304, True)


def test_a_reading_after_an_opponent_only_battle_never_settles_that_a_possible_loss_missed() -> None:
    # A zero-defense day ends at 6,000 before a possible 320 charge. The
    # opponent reports a new-day attack at 05:10 that the player's own log
    # lacks; the 05:20 profile may show it, so its 6,000 proves no charge missed.
    result = verdict(Reading(at(20), 6000), loss=(320,), certain=False, end=6000,
                     unknown_from=at(10))

    assert result.outcome == "unverified"


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


def test_a_loss_shown_landed_never_un_lands() -> None:
    # A 05:02 reading matches the 304 charge; a covered 05:20 reading shows
    # 5,940, no charge. One of them is wrong, so the day is Uncertain.
    result = verdict(Reading(at(2), 5636), Reading(at(20), 5940), loss=(304,),
                     certain=False)

    assert (result.outcome, result.residual) == ("contradicted", 304)
    assert result.reading is not None and result.reading.read_at == at(20)


def test_a_later_reading_confirms_the_loss_after_a_new_day_battle() -> None:
    # A zero-defense day ends at 6,000 before its possible 320 charge. 05:02
    # reads 6,000; a +40 attack at 05:08 lands; a covered 05:30 reading of
    # 5,720 shows the charge, so the day ends at 5,680.
    new_day = (Effect("next-attack", 40, at(8)),)
    result = verdict(Reading(at(2), 6000, reset_reading=True), Reading(at(30), 5720),
                     loss=(320,), certain=False, end=6000, new_day=new_day)

    assert (result.outcome, result.loss, result.exact) == ("verified", 320, True)
    assert result.reading is not None and result.reading.read_at == at(30)


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


def test_a_reading_during_the_day_must_show_the_battles_landed_by_then() -> None:
    # The day starts at 6,000 at 05:00; the +20 attack is reported at 07:00
    # and the 10-trophy defense starts at 08:00 (DAY_BATTLES).
    day_start = RESET - timedelta(days=1)

    def during(*readings: Reading, pending_loss: int = 0):
        return contradiction_during_day(
            readings, day_start=day_start, reset_at=RESET, start=6000,
            pending_loss=pending_loss, day_effects=DAY_BATTLES,
        )

    noon = day_start + timedelta(hours=7)
    assert during(Reading(noon, 6010)) is None
    found = during(Reading(noon, 6100))
    assert found is not None and (found.reading.read_at, found.residual) == (noon, 90)
    # At 07:03 the attack may or may not have landed: either way fits, and
    # a value fitting neither way disagrees.
    assert during(Reading(day_start + timedelta(hours=2, minutes=3), 6000)) is None
    assert during(Reading(day_start + timedelta(hours=2, minutes=3), 6020)) is None
    found = during(Reading(day_start + timedelta(hours=2, minutes=3), 6010))
    assert found is not None and found.residual == -10
    # The day before's automatic loss may not have landed at 05:20.
    assert during(Reading(day_start + timedelta(minutes=20), 6070), pending_loss=70) is None
    # Once a reading shows it landed, a later one without it disagrees.
    landed = Reading(day_start + timedelta(minutes=20), 6000)
    unlanded = Reading(day_start + timedelta(minutes=90), 6080)
    assert during(landed, pending_loss=80) is None
    found = during(landed, unlanded, pending_loss=80)
    assert found is not None and (found.reading, found.residual) == (unlanded, 80)
    # Before 05:15 the day before's last credits may still be landing, and a
    # Season 0 reading can only confirm.
    assert during(Reading(day_start + timedelta(minutes=10), 5900)) is None
    assert during(Reading(noon, 6100, confirm_only=True)) is None


def test_the_weekly_raise_hides_a_possible_charge_a_reading_may_show() -> None:
    # A Sunday ending at 5,310 with no defense slot used may be charged 320,
    # which the Monday raise lifts from 4,990 to 5,000: a 5,000 reading, or
    # 5,030 after a new-day +30 attack, fits but cannot prove the charge.
    day = test_reconciliation._input(
        start_trophies=5310, next_start_trophies=5000, contributions=(),
        previous_day=PreviousRankedDay(True, 8, 320, 0), boundary_kind="weekly",
    )
    attack = BattleContribution("next-attack", "offense", 30,
                                battle_timestamp=day.ranked_day.end + timedelta(minutes=5))
    later = replace(day, next_start_trophies=5310, new_day_contributions=(attack,),
                    readings=(Reading(day.ranked_day.end + timedelta(minutes=20), 5030),))

    for data in (day, later):
        result = reconcile_ranked_day(data)
        assert (result.state, result.failure_reasons) == ("Partial", ("end_reading_unverified",))
        assert result.final_trophies_before_reset == 5310


def test_a_delayed_attack_credit_and_a_battle_in_flight_read_together() -> None:
    # The +20 attack landed at 04:44 but its credit has not shown; a
    # 10-trophy defense started at 04:55 may not have either. 6,000 lacks both.
    day = (Effect("attack", 20, at(-20)), Effect("defense", -10, at(-5)))
    result = verdict(Reading(at(2), 6000, reset_reading=True), day=day, end=6010)

    assert (result.outcome, result.lagged) == ("unverified", ("attack",))


def test_a_loss_every_reading_of_it_shows_landed_never_un_lands() -> None:
    # A new-day +30 attack reported at 05:17 is in flight at 05:20, whose
    # 5,870 fits only without it and with the 70 loss. At 05:40 5,970 fits
    # only without the loss: it disagrees.
    new_day = (Effect("next-attack", 30, at(17)),)
    result = verdict(Reading(at(20), 5870), Reading(at(40), 5970), new_day=new_day)

    assert (result.outcome, result.residual) == ("contradicted", 70)
    assert result.reading is not None and result.reading.read_at == at(40)

    # So too during the day: from a settled 6,000 with the day before's 80
    # loss pending, 05:20 reads 6,000 while a +30 attack reported at 05:17
    # is in flight, which only fits with the loss landed.
    day_start = RESET - timedelta(days=1)
    attack = (Effect("attack", 30, day_start + timedelta(minutes=17)),)
    found = contradiction_during_day(
        (Reading(day_start + timedelta(minutes=20), 6000),
         Reading(day_start + timedelta(hours=1), 6110)),
        day_start=day_start, reset_at=RESET, start=6000, pending_loss=80,
        day_effects=attack,
    )
    assert found is not None and (found.reading.trophies, found.residual) == (6110, 80)


def test_a_reading_after_an_opponent_only_battle_never_shows_a_possible_charge() -> None:
    # The day may be charged 40. An opponent reports a new-day 40-trophy
    # defense the player's log lacks; the 05:20 profile's 5,960 may be that.
    result = verdict(Reading(at(20), 5960), loss=(40,), certain=False, end=6000,
                     unknown_from=at(10))

    assert result.outcome == "unverified"


def test_a_monday_reading_before_the_loss_shows_the_day_before_unraised() -> None:
    # Sunday ended at 4,900 after its 200 loss, so Monday starts at 5,000.
    # Before the loss lands the profile shows 5,100, never 5,200.
    def monday(trophies: int):
        return reconcile_ranked_day(test_reconciliation._input(
            start_trophies=5000, next_start_trophies=5000, contributions=(),
            previous_day=PreviousRankedDay(True, 8, 200, 0, automatic_loss=200,
                                           final_trophies=4900),
            readings=(Reading(test_reconciliation.DAY.start + timedelta(minutes=20),
                              trophies),),
        ))

    assert "trophy_equation_mismatch" not in monday(5100).failure_reasons
    assert "trophy_equation_mismatch" in monday(5200).failure_reasons


def test_a_reading_after_a_reset_that_hides_the_end_must_show_the_reset_total() -> None:
    # The day ends at 4,840 and the weekly raise takes it to 5,000, which
    # hides its end. A later 5,010, with no new-day battle, disagrees.
    day = test_reconciliation._input(start_trophies=4900, next_start_trophies=5000,
                                     boundary_kind="weekly")
    later = test_reconciliation.DAY.end + timedelta(minutes=20)
    kept = reconcile_ranked_day(replace(day, readings=(Reading(later, 5000),)))
    wrong = reconcile_ranked_day(replace(day, readings=(Reading(later, 5010),)))

    assert kept == reconcile_ranked_day(day)
    assert "trophy_equation_mismatch" not in kept.failure_reasons
    assert "trophy_equation_mismatch" in wrong.failure_reasons
