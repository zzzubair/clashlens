"""Reconciliation settlement behavior."""

from __future__ import annotations

from datetime import UTC, datetime

from reconciliation_support import (
    _attacks,
    _defenses,
    _input,
    _october,
    _season_day_1,
)

from clashlens.reconciliation import (
    PreviousRankedDay,
    reading_rule,
    reconcile_ranked_day,
)


def test_a_reset_reading_taken_before_the_automatic_loss_completes_the_day() -> None:
    # Production, 2 October 2026: player #2C8CULPQJ started at 4,726, won 200
    # in 7 attacks and lost 62 in 2 defenses, after 8 defenses for 273 the day
    # before: floor((273 + 62) / 10) * 6 = 198 for the 6 missing defenses. The
    # Reset reading at 05:02:22 on 3 October, 4,864, was the day's end plus
    # that loss; the reading at 05:16:32, before any new-day battle, was 4,666.
    day = {
        "start_trophies": 4726,
        "contributions": (*_attacks(40, 25, 28, 28, 29, 25, 25), *_defenses(22, 40)),
        "previous_day": PreviousRankedDay(True, 8, 273, 0),
    }
    before_loss = reconcile_ranked_day(_october(2, next_start_trophies=4864, **day))
    after_loss = reconcile_ranked_day(_october(2, next_start_trophies=4666, **day))
    other_gap = reconcile_ranked_day(_october(2, next_start_trophies=4874, **day))

    assert (before_loss.state, before_loss.confidence) == ("Complete", "inferred")
    assert before_loss.automatic_defense_loss == 198
    assert before_loss.automatic_defense_evidence_state == "calculated"
    assert before_loss.final_trophies_before_reset == 4666
    assert before_loss.expected_next_start_trophies == 4666
    # The next day starts from the reading less the loss it had not applied.
    assert before_loss.next_start_trophies == 4666
    assert before_loss.observed_trophy_change == before_loss.net_trophy_change == -60
    assert before_loss.unexplained_residual == 0
    assert before_loss.unsettled_automatic_loss == 198
    assert before_loss.formula_components["next_start_trophies"] == 4666
    assert before_loss.formula_components["next_start_reading_trophies"] == 4864
    assert before_loss.formula_components["unsettled_automatic_loss"] == 198
    assert before_loss.failure_reasons == ()
    # A reading after the loss still proves it, and carries nothing unsettled.
    assert (after_loss.confidence, after_loss.unsettled_automatic_loss) == ("exact", 0)
    assert after_loss.automatic_defense_evidence_state == "confirmed"
    assert after_loss.next_start_trophies == 4666
    assert "unsettled_automatic_loss" not in after_loss.formula_components
    # Any other gap is still a mismatch.
    assert other_gap.state == "Inconsistent"
    assert "trophy_equation_mismatch" in other_gap.failure_reasons
    assert other_gap.unexplained_residual == 208


def test_the_next_day_starts_from_the_reading_less_the_unsettled_loss() -> None:
    # Production, 3 October 2026: player #2VGL0Y9RL's Reset reading at
    # 05:03:39, 4,925, was 155 above the end of 2 October (8 attacks for 189,
    # 3 defenses for 92, 8 defenses for 252 the day before: floor(344 / 11) * 5).
    # Their reading at 05:17:25 was 4,770. On 3 October they won 215 in 8
    # attacks and lost 240 in 8 defenses, and the 4 October reading was 4,745.
    previous = PreviousRankedDay(
        True, 3, 92, 0, unsettled_automatic_loss=155, end_baseline_id=93593
    )
    day = {
        "start_trophies": 4925,
        "next_start_trophies": 4745,
        "contributions": (
            *_attacks(13, 40, 31, 29, 40, 29, 13, 20),
            *_defenses(40, 40, 29, 40, 20, 15, 26, 30),
        ),
        "previous_day": previous,
    }
    settled = reconcile_ranked_day(_october(3, start_baseline_id=93593, **day))
    # Built on another Reset evidence row, the reading stands as read.
    other_reset = reconcile_ranked_day(_october(3, start_baseline_id=93594, **day))

    assert settled.start_trophies == 4770
    assert (settled.state, settled.confidence) == ("Complete", "exact")
    assert settled.final_trophies_before_reset == 4745
    assert settled.net_trophy_change == -25
    assert settled.unexplained_residual == 0
    assert settled.formula_components["start_trophies"] == 4770
    assert settled.formula_components["start_reading_trophies"] == 4925
    assert settled.formula_components["start_unsettled_automatic_loss"] == 155
    assert settled.input_evidence["previous_day"]["unsettled_automatic_loss"] == 155
    assert other_reset.start_trophies == 4925
    assert other_reset.state == "Inconsistent"
    assert other_reset.unexplained_residual == -155


def test_a_day_can_both_start_and_end_on_readings_taken_before_the_loss() -> None:
    # Production, 3 October 2026: player #2C8CULPQJ started from the 4,864
    # reading above, 198 unsettled, won 106 in 6 attacks and lost 113 in 5
    # defenses after 2 defenses for 62 the day before: floor(175 / 7) * 3 = 75.
    # The 4 October reading, 4,659, was again the day's end plus the loss.
    result = reconcile_ranked_day(
        _october(
            3,
            start_baseline_id=82547,
            start_trophies=4864,
            next_start_trophies=4659,
            contributions=(*_attacks(21, 20, 13, 13, 28, 11), *_defenses(31, 22, 14, 14, 32)),
            previous_day=PreviousRankedDay(
                True, 2, 62, 0, unsettled_automatic_loss=198, end_baseline_id=82547
            ),
        )
    )

    assert result.start_trophies == 4666
    assert result.automatic_defense_loss == 75
    assert result.final_trophies_before_reset == 4584
    assert result.unsettled_automatic_loss == 75
    assert (result.state, result.confidence) == ("Complete", "inferred")


def test_a_monday_reading_above_5000_taken_before_the_loss_is_accepted_too() -> None:
    # The default day ends on 5,940 with a calculated loss of 70. A Monday
    # Reset keeps a total above 5,000, so its reading can predate the loss.
    weekly = reconcile_ranked_day(_input(next_start_trophies=6010, boundary_kind="weekly"))
    # At or below 5,000 the Reset hides the end, and 5,000 is never the loss.
    raised = reconcile_ranked_day(
        _input(start_trophies=5060, next_start_trophies=5000, boundary_kind="weekly")
    )

    assert (weekly.state, weekly.confidence) == ("Complete", "inferred")
    assert weekly.unsettled_automatic_loss == 70
    assert weekly.final_trophies_before_reset == 5940
    assert (raised.state, raised.unsettled_automatic_loss) == ("Complete", 0)


def test_an_official_season_total_above_the_end_by_the_loss_is_a_mismatch() -> None:
    # 6,000 + 40 - 30, less 210 for 7 missing defenses at 30 each, ends on
    # 5,800. The game's official total already includes that loss.
    day = {
        "contributions": (*_attacks(40), *_defenses(30)),
        "previous_day": PreviousRankedDay(True, 8, 240, 0),
    }
    reading = reconcile_ranked_day(_input(next_start_trophies=6010, **day))
    official = reconcile_ranked_day(_input(
        next_start_trophies=6010,
        end_baseline_evidence={"official_final_trophies": 6010}, **day,
    ))
    matching = reconcile_ranked_day(_input(
        next_start_trophies=5800,
        end_baseline_evidence={"official_final_trophies": 5800}, **day,
    ))

    assert reading.unsettled_automatic_loss == 210
    assert reading.state == "Complete"
    assert official.state == "Inconsistent"
    assert official.unsettled_automatic_loss == 0
    assert official.unexplained_residual == 210
    assert (matching.state, matching.confidence) == ("Complete", "exact")
    assert matching.final_trophies_before_reset == 5800


def test_season_day_1_charges_attacks_minus_defenses_unless_attacks_are_fewer() -> None:
    # Production, 5 October 2026 (Day 1), profiles read on 6 October after
    # the loss landed and before any Day 2 battle. #P9VPRRJU: 3 attacks for
    # 53, 1 defense for 31; the 05:00 reading, 5,022, was the day's end
    # before the loss, and 05:09:53 read 4,960: 31 * (3 - 1), not 31 * 7.
    more_attacks = _season_day_1((24, 15, 14), (31,), None, 5022)
    # #9CCCRP8L9: 4 attacks for 78 and 4 defenses for 110 lost nothing: the
    # reading, 4,968, still stood at 06:31 with no Day 2 battle before 07:56.
    equal = _season_day_1((11, 15, 28, 24), (15, 40, 15, 40), None, 4968)
    # #QL89G8V2V: 1 attack for 21, 7 defenses for 189: the 05:00 reading was
    # 4,832 and 05:09:25 read 4,805, floor(189 / 7) for the one missing defense.
    fewer_attacks = _season_day_1((21,), (40, 26, 25, 30, 28, 21, 19), None, 4832, readings=(
        reading_rule.Reading(datetime(2026, 10, 6, 5, 9, 25, tzinfo=UTC), 4805),))

    assert more_attacks.automatic_defense_loss == 62
    assert more_attacks.final_trophies_before_reset == 4960
    assert more_attacks.unsettled_automatic_loss == 62
    assert (more_attacks.state, more_attacks.confidence) == ("Complete", "inferred")
    assert equal.automatic_defense_loss == 0
    assert equal.automatic_defense_evidence_state == "confirmed"
    assert equal.final_trophies_before_reset == 4968
    assert (equal.state, equal.confidence) == ("Complete", "exact")
    # With fewer attacks the loss is (8 - defenses) or nothing: the reading shows which.
    assert (fewer_attacks.automatic_defense_loss, fewer_attacks.final_trophies_before_reset) == (27, 4805)
    assert (fewer_attacks.state, fewer_attacks.confidence) == ("Complete", "exact")
    # Any other day keeps 8 - defenses, whatever the attacks.
    ordinary = reconcile_ranked_day(
        _input(contributions=(*_attacks(20, 20), *_defenses(10, 10)), next_start_trophies=5960)
    )
    assert ordinary.automatic_defense_loss == 60
    assert ordinary.state == "Complete"


def test_no_opponent_rows_are_used_slots_for_the_automatic_loss_only() -> None:
    # Production, 5 October 2026 (Day 1), each log holding a "no opponent, no
    # battle" row; profiles read on 6 October after the loss and before any
    # Day 2 battle. #C8UUYRYP: 8 attacks, 6 defenses for 211 and one such
    # defense row; 5,070 at 05:06, then 5,040: floor(211 / 7) for one
    # missing defense, not floor(211 / 6) * 2 = 70.
    extra_defense = _season_day_1(
        (40,) * 7 + (1,), (40, 40, 40, 40, 40, 11), None, 5070,
        zero_result_defense_slots=1,
    )
    # #2VCCCJG9: 7 attacks, one such attack row, 7 defenses for 186; 5,086,
    # then 5,060: 8 used attacks charge floor(186 / 7) once, not nothing.
    extra_attack = _season_day_1(
        (40,) * 6 + (32,), (40, 40, 40, 30, 20, 10, 6), None, 5086,
        zero_result_attack_slots=1,
    )
    # #P2P80LVVJ: 6 attacks, one such attack row, 7 defenses for 194; the
    # 4,991 reading stood: 7 used attacks for 7 defenses lose nothing, not
    # floor(194 / 7) = 27.
    equal = _season_day_1(
        (40, 40, 40, 40, 20, 5), (40, 40, 40, 40, 20, 10, 4), None, 4991,
        zero_result_attack_slots=1,
    )

    assert extra_defense.automatic_defense_loss == 30
    assert extra_defense.final_trophies_before_reset == 5040
    assert extra_defense.unsettled_automatic_loss == 30
    assert extra_attack.automatic_defense_loss == 26
    assert extra_attack.final_trophies_before_reset == 5060
    assert extra_attack.unsettled_automatic_loss == 26
    assert equal.automatic_defense_loss == 0
    assert equal.final_trophies_before_reset == 4991
    assert (equal.state, equal.confidence) == ("Complete", "exact")
    # They are not battles.
    assert (extra_defense.attack_count, extra_defense.defense_count) == (8, 6)
    assert (extra_attack.attack_count, extra_attack.defense_count) == (7, 7)
    assert extra_defense.input_evidence["zero_result_defense_slots"] == 1
    assert "zero_result_attack_slots" not in extra_defense.input_evidence
    assert "zero_result_defense_slots" not in reconcile_ranked_day(
        _input()
    ).input_evidence


def test_no_opponent_defense_rows_join_both_days_of_the_average() -> None:
    # Any other day pools yesterday's and today's used defense slots, such
    # rows counting with no loss: floor((180 + 150) / (7 + 6)) * 2 = 50.
    pooled = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=5800,
            contributions=_defenses(40, 30, 30, 30, 20),
            previous_day=PreviousRankedDay(
                True, 6, 180, 0, zero_result_defense_slots=1
            ),
            zero_result_defense_slots=1,
        )
    )
    # 7 defenses and one such row fill all 8 slots: no loss to calculate.
    full = reconcile_ranked_day(
        _input(
            next_start_trophies=5930,
            contributions=_defenses(10, 10, 10, 10, 10, 10, 10),
            previous_day=None,
            zero_result_defense_slots=1,
        )
    )

    assert pooled.automatic_defense_loss == 50
    assert pooled.final_trophies_before_reset == 5800
    assert pooled.state == "Complete"
    assert pooled.input_evidence["previous_day"]["zero_result_defense_slots"] == 1
    assert full.automatic_defense_loss is None
    assert full.automatic_defense_evidence_state == "not_applicable"
    assert full.final_trophies_before_reset == 5930
    assert full.state == "Complete"


def test_a_disputed_day_never_takes_the_loss_off_its_reading() -> None:
    # The default day ends on 5,940 with a calculated loss of 70, so a 6,010
    # reading looks like one taken before the loss. With the battles disputed,
    # other reports could give another loss, so the reading stands as read.
    disputed = reconcile_ranked_day(
        _input(next_start_trophies=6010, perspective_disagreement=True)
    )
    # A next day built on it, even when the saved result claims a loss.
    next_day = reconcile_ranked_day(
        _input(
            start_baseline_id=11,
            start_trophies=6010,
            next_start_trophies=6010,
            contributions=(),
            previous_day=PreviousRankedDay(
                False, 1, 10, 0, unsettled_automatic_loss=70, end_baseline_id=11
            ),
        )
    )

    assert disputed.state == "Inconsistent"
    assert disputed.unsettled_automatic_loss == 0
    assert disputed.next_start_trophies == 6010
    assert "unsettled_automatic_loss" not in disputed.formula_components
    assert next_day.start_trophies == 6010
    assert "start_unsettled_automatic_loss" not in next_day.formula_components
