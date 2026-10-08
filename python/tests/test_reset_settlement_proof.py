"""The settlement verdict for one Reset, guard by guard, without a database.

Each case starts from a check that passes every guard, the design's 4,837 to
4,804 example, and breaks exactly what it names. The roots here are
synthetic: they show the rule's behavior, not production coverage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from clashlens.domain import HISTORICAL_TROPHY_ALLOCATION_RULE_VERSION as OLD_RULE
from clashlens.domain import TROPHY_ALLOCATION_RULE_VERSION, ranked_day_for
from clashlens.ranked_day_inputs import Reading
from clashlens.reconciliation import BattleContribution, CoverageObservation
from clashlens.reset_settlement import DayEnd, ProofInputs, Root, evaluate_boundary

RESET = datetime(2026, 8, 5, 5, tzinfo=UTC)  # an ordinary Wednesday Reset
MINUTE, DAY = timedelta(minutes=1), timedelta(days=1)


def reading(observation_id: int, completed: datetime, trophies: int | None) -> Reading:
    return Reading(observation_id, completed - timedelta(seconds=1), completed,
                   "processed", "parser", True, trophies)


def check(*, boundary: datetime = RESET, start: int = 4746,
          prior: list[int] | None = None, defenses: list[int] | None = None,
          attacks: list[int] | None = None) -> ProofInputs:
    """A named check passing every guard, on a settled previous Reset, the
    day before saved with ``prior`` defenses."""
    prior = [37] * 7 + [39] if prior is None else prior  # 8 defenses, 298 lost
    defenses = [30] * 6 + [29] if defenses is None else defenses  # 7, 209 lost
    attacks = [40] * 7 + [20] if attacks is None else attacks  # 300 won
    prior_from = boundary - 2 * DAY + 5 * MINUTE
    ended_from = prior_from + DAY
    reports = [("older", "defense", prior_from - 60 * MINUTE, 30)]
    for prefix, lens, since, amounts in (
        ("prior", "defense", prior_from, prior),
        ("defense", "defense", ended_from, defenses),
        ("attack", "offense", ended_from + 180 * MINUTE, attacks),
    ):
        reports += [(f"{prefix}{i}", lens, since + (i + 1) * 10 * MINUTE, amount)
                    for i, amount in enumerate(amounts)]
    loss = 0
    if 1 <= len(defenses) < 8:
        loss = (sum(prior) + sum(defenses)) // (len(prior) + len(defenses)) * (8 - len(defenses))
    target = start + sum(attacks) - sum(defenses) - loss
    profile = reading(2, boundary + timedelta(minutes=23, seconds=56), target)
    log = reading(3, profile.response_completed_at + timedelta(seconds=2), None)
    return ProofInputs(
        boundary_at=boundary,
        work_status="complete",
        early_recorded=True,
        early=reading(1, boundary + timedelta(seconds=31), target + loss),
        profile=profile,
        battle_log=log,
        log_coverage=CoverageObservation(
            observed_at=log.response_completed_at, row_count=len(reports),
            battle_identities=tuple(r[0] for r in reports), has_row_gap=False,
            observation_id=3,
        ),
        log_reports=tuple(reports),
        battles=tuple(
            BattleContribution(
                battle_identity=identity, lens=lens, trophy_amount=amount,
                source_rule_version=TROPHY_ALLOCATION_RULE_VERSION,
                opponent_tag="#OPP", battle_timestamp=at,
            )
            for identity, lens, at, amount in reports if at >= ended_from
        ),
        previous_defenses=(len(prior), sum(prior)),
        root=Root(boundary - DAY, start, "root-fingerprint", 1, (11, 12, 13)),
    )


def judged(inputs: ProofInputs) -> tuple[str, tuple[str, ...], int | None]:
    verdict = evaluate_boundary(inputs)
    return verdict.state, verdict.reasons, verdict.trophies


def test_4837_to_4804_requires_adjustment_and_independent_catchup() -> None:
    passing = check()
    verdict = evaluate_boundary(passing)
    assert (verdict.state, verdict.reasons, verdict.trophies) == ("settled", (), 4804)
    assert verdict.proof["automatic_loss_basis"] == {
        "prior_defenses": 8, "prior_defense_loss": 298, "defenses": 7,
        "defense_loss": 209, "automatic_loss": 33,
    }
    assert passing.early.trophies == 4837
    assert verdict.proof["catchup"]["target"] == 4804
    # A named 05:20 profile still on 4,837 stays unresolved, even with a
    # second equal reading of 4,837 after it.
    stale = reading(2, RESET + timedelta(minutes=20, seconds=30), 4837)
    stale_log = reading(3, stale.response_completed_at + timedelta(seconds=2), None)
    state, reasons, trophies = judged(replace(
        passing, profile=stale, battle_log=stale_log,
        later_profiles=((RESET + 21 * MINUTE, 4837),),
    ))
    assert (state, trophies) == ("unresolved", None)
    assert {"observed_drop_mismatch", "profile_catchup_unknown"} <= set(reasons)
    # A later ordinary 4,804 read never replaces a failed named profile.
    assert judged(replace(
        passing, profile=None, later_profiles=((RESET + 24 * MINUTE, 4804),),
    )) == ("unresolved", ("settlement_profile_missing",), None)
    # A quiet-window profile left unprocessed could have contradicted the pin.
    assert judged(replace(
        passing, later_profiles=((RESET + 24 * MINUTE, 4804), (RESET + 27 * MINUTE, None)),
    )) == ("unresolved", ("later_profile_unprocessed",), None)


def settles_on(inputs: ProofInputs, loss: int, target: int) -> ProofInputs:
    return replace(inputs, early=replace(inputs.early, trophies=target + loss),
                   profile=replace(inputs.profile, trophies=target))


def test_no_opponent_rows_are_used_slots_for_the_automatic_loss() -> None:
    # 6 defenses for 180 plus one "no opponent, no battle" defense row, and
    # a saved day before with one such row besides its 8 defenses: the loss
    # is floor((298 + 180) / (9 + 7)) for one missing defense, 29, not
    # floor(478 / 14) * 2 = 68.
    inputs = settles_on(replace(check(defenses=[30] * 6), zero_result_slots=frozenset({
        (RESET - DAY + 300 * MINUTE, False),
    }), previous_defenses=(9, 298)), 29, 4746 + 300 - 180 - 29)
    verdict = evaluate_boundary(inputs)

    assert (verdict.state, verdict.trophies) == ("settled", 4837)
    assert verdict.proof["automatic_loss_basis"] == {
        "prior_defenses": 9, "prior_defense_loss": 298, "defenses": 6,
        "defense_loss": 180, "automatic_loss": 29, "zero_result_defenses": 1,
    }
    assert judged(replace(inputs, zero_result_slots=frozenset()))[0] == "unresolved"


def test_season_day_1_reset_leaves_out_the_previous_season() -> None:
    # Day 1 ended at this Reset: 7 defenses for 209 and 8 attacks lose
    # floor(209 / 7) once, 29, not the previous Season's pooled 33.
    day_2 = datetime(2026, 10, 6, 5, tzinfo=UTC)
    inputs = settles_on(check(boundary=day_2), 29, 4746 + 300 - 209 - 29)

    assert judged(inputs) == ("settled", (), 4808)


def test_40_trophy_lag_rejects_stale_target_and_fixed_quiet_margin() -> None:
    """Both days had eight defenses; 5,135 stood until 05:21:55, then 5,095."""
    eight = [30] * 8
    lag = check(prior=eight, defenses=eight, attacks=[], start=5095 + 240)
    profile = reading(2, RESET + timedelta(minutes=21, seconds=55), 5135)
    log = reading(3, profile.response_completed_at + timedelta(seconds=2), None)
    lag = replace(lag, profile=profile, battle_log=log,
                  early=reading(1, RESET + timedelta(minutes=1, seconds=41), 5135),
                  first_new_day_report=RESET + timedelta(minutes=43, seconds=59))
    # The quiet 5,095 read before any new-day battle contradicts the pin.
    contradicted = replace(lag, later_profiles=(
        (RESET + timedelta(minutes=27, seconds=52), 5095),
    ))
    assert judged(contradicted) == ("unresolved", ("later_profile_contradicts",), None)
    # A target rooted in the stale pre-Reset profile is not an independent root.
    for root in (None, Root(RESET, 5135, "stale", 1, (1,))):
        state, reasons, _ = judged(replace(lag, root=root))
        assert state == "unresolved" and reasons[0].startswith("independent_root")
    # Independent arithmetic gives 5,095, but with eight defenses there is no
    # automatic loss to observe, so this stage cannot settle it either way.
    assert judged(replace(lag, root=Root(RESET - DAY, 5335, "f", 1, ()))) == (
        "unresolved", ("no_positive_automatic_loss",), None,
    )
    pinned = evaluate_boundary(contradicted).proof["readings"]["settlement_profile"]
    assert pinned["trophies"] == 5135


def test_163_late_attacks_are_not_auto_loss_or_a_settled_end() -> None:
    """Five old-day attacks after 04:43 were missing from the 4,780 reading."""
    late = check(prior=[30] * 8, defenses=[30] * 7 + [35], attacks=[28, 40, 15, 40, 40, 39, 40],
                 start=4946)
    assert late.profile.trophies == 4946 + 242 - 245 == 4943
    stale_end = replace(late, early=reading(1, RESET + timedelta(minutes=5, seconds=36), 4780))
    state, reasons, _ = judged(stale_end)
    assert state == "unresolved" and "no_positive_automatic_loss" in reasons
    assert evaluate_boundary(stale_end).proof["automatic_loss_basis"]["automatic_loss"] is None
    # Later post-battle totals are never a start: a new-day battle came first.
    after_battles = replace(
        stale_end, profile=reading(2, RESET + 25 * MINUTE, 5106),
        battle_log=reading(3, RESET + 26 * MINUTE, None),
        first_new_day_report=RESET + 10 * MINUTE,
        first_report_after_early=RESET + 10 * MINUTE,
    )
    assert judged(after_battles)[1] == (
        "new_day_battle_before_profile", "battle_between_readings",
    )


def test_old_real_loss_equal_to_auto_loss_does_not_prove_settlement() -> None:
    passing = check()
    # A real 33-trophy defense the 4,837 reading had not caught up with: the
    # drop matches the automatic loss, but the independent target needs both.
    alias = replace(passing, root=replace(passing.root, trophies=4746 - 33))
    state, reasons, trophies = judged(alias)
    assert (state, trophies) == ("unresolved", None)
    assert "observed_drop_mismatch" not in reasons
    assert {"early_reading_mismatch", "profile_catchup_unknown"} <= set(reasons)
    # A missing attack and defense whose effects cancel still change the
    # battle evidence, so the equality alone is never accepted.
    extra = tuple(
        BattleContribution(battle_identity=name, lens=lens, trophy_amount=30,
                           source_rule_version=TROPHY_ALLOCATION_RULE_VERSION,
                           opponent_tag="#OPP", battle_timestamp=RESET - 60 * MINUTE)
        for name, lens in (("hidden-attack", "offense"), ("hidden-defense", "defense"))
    )
    assert judged(replace(passing, battles=passing.battles + extra)) == (
        "unresolved", ("battle_reports_changed_after_log",), None,
    )


def test_target_is_independent_and_rooted() -> None:
    passing = check()
    root = passing.root
    cases = {
        # No settled previous Reset: a Complete day, an early Reset reading or
        # a stale profile is never loaded as a root.
        None: "independent_root_missing",
        replace(root, boundary_at=RESET): "independent_root_circular",
        replace(root, boundary_at=RESET + DAY): "independent_root_circular",
        replace(root, observations=(2,)): "independent_root_circular",
        replace(root, observations=(1, 12)): "independent_root_circular",
    }
    for candidate, reason in cases.items():
        assert judged(replace(passing, root=candidate)) == ("unresolved", (reason,), None)
    assert judged(passing)[0] == "settled"


def test_profile_and_log_order_uses_wire_request_and_both_participants() -> None:
    passing = check()
    profile, log = passing.profile, passing.battle_log
    done = profile.response_completed_at
    cases = [
        # The log's request started before the profile arrived.
        ({"battle_log": replace(log, request_started_at=done - timedelta(seconds=1))},
         "battle_log_requested_before_profile_arrived"),
        # The profile's request started before 05:20, though it finished after.
        ({"profile": replace(profile, request_started_at=RESET + 20 * MINUTE
                             - timedelta(seconds=1))}, "settlement_profile_too_early"),
        ({"profile": replace(profile, response_completed_at=RESET + timedelta(hours=23, minutes=55)),
          "battle_log": replace(log, request_started_at=RESET + timedelta(hours=23, minutes=55),
                                response_completed_at=RESET + timedelta(hours=23, minutes=56))},
         "settlement_check_after_window"),
        # Any report, by either player, of a new-day battle before or at the
        # profile's arrival; a 05:02 old-day report between the readings.
        ({"first_new_day_report": done}, "new_day_battle_before_profile"),
        ({"first_new_day_report": RESET + 5 * MINUTE,
          "first_report_after_early": RESET + 5 * MINUTE},
         "new_day_battle_before_profile"),
        ({"first_report_after_early": RESET + 2 * MINUTE}, "battle_between_readings"),
        ({"early": replace(passing.early, response_completed_at=done)},
         "early_reading_after_profile"),
    ]
    for change, reason in cases:
        state, reasons, _ = judged(replace(passing, **change))
        assert state == "unresolved" and reasons[0] == reason, (change, reasons)
    # A new-day battle after the profile arrived does not matter.
    assert judged(replace(passing, first_new_day_report=done + timedelta(seconds=1),
                          first_report_after_early=done + timedelta(seconds=1)))[0] == "settled"


def test_ended_day_source_proof_requires_complete_classified_history() -> None:
    passing = check()
    coverage, battles = passing.log_coverage, passing.battles
    in_window = RESET - 30 * MINUTE

    def changed(**fields: object) -> ProofInputs:
        return replace(passing, battles=(replace(battles[0], **fields), *battles[1:]))

    cases = [
        # Fifty-row truncation: the log no longer reaches before the ended day.
        (replace(passing, log_reports=tuple(
            report for report in passing.log_reports
            if report[2] >= RESET - DAY + 5 * MINUTE
        )), "battle_log_too_short"),
        (replace(passing, log_coverage=replace(coverage, has_row_gap=True)), "battle_log_unreadable"),
        (replace(passing, log_coverage=replace(coverage, malformed_row_count=1)), "battle_log_unreadable"),
        (replace(passing, log_coverage=replace(coverage, unclassified_row_count=1)), "battle_log_unreadable"),
        (replace(passing, log_coverage=replace(coverage, valid=False)), "battle_log_unreadable"),
        (replace(passing, log_coverage=None), "battle_log_unreadable"),
        (replace(passing, log_coverage=replace(
            coverage, battle_identities=coverage.battle_identities + ("prior0",))),
         "battle_log_unreadable"),
        (changed(disagreement=True), "battle_report_unusable"),
        (changed(opponent_tag=None), "battle_report_unusable"),
        (changed(valid=False, failure_reason="malformed"), "battle_report_unusable"),
        (changed(source_rule_version=OLD_RULE), "rule_correction_pending"),
        # A row saved after the named log: a late battle, a corrected amount,
        # or an unreadable row from the ended day, however late it arrived.
        (changed(battle_identity="late"), "battle_reports_changed_after_log"),
        (changed(trophy_amount=31), "battle_reports_changed_after_log"),
        (replace(passing, late_unreadable=(None,)), "late_battle_log_unreadable"),
        (replace(passing, late_unreadable=(in_window,)), "late_battle_log_unreadable"),
        (check(defenses=[30] * 9), "battle_count_exceeds_eight"),
    ]
    for inputs, reason in cases:
        state, reasons, _ = judged(inputs)
        assert state == "unresolved" and reason in reasons, (reason, reasons)
    # An unreadable row from the new day, or before the ended day, does not
    # matter.
    assert judged(replace(passing, late_unreadable=(
        RESET + 6 * MINUTE, RESET - DAY - 60 * MINUTE, RESET - 3 * DAY,
    )))[0] == "settled"


@pytest.mark.parametrize("prior,defenses,expected", [
    ([37] * 7 + [39], [30] * 6 + [29], 33),  # 507 // 15 = 33, one missing
    ([40] * 8, [10], 252),  # 330 // 9 = 36, seven missing
    ([], [36, 36], 216),  # no prior defense, with history reaching before it
    ([5] * 8, [5] * 7, 5),
])
def test_ordinary_positive_adjustment_has_exact_formula(
    prior: list[int], defenses: list[int], expected: int
) -> None:
    passing = check(prior=prior, defenses=defenses)
    loss = evaluate_boundary(passing).proof["automatic_loss_basis"]["automatic_loss"]
    assert loss == expected
    assert judged(passing) == ("settled", (), passing.profile.trophies)
    # A drop one trophy off either way is not the automatic loss.
    for delta in (-1, 1):
        early = replace(passing.early, trophies=passing.early.trophies + delta)
        assert "observed_drop_mismatch" in judged(replace(passing, early=early))[1]
        moved = replace(passing.profile, trophies=passing.profile.trophies + delta)
        assert "observed_drop_mismatch" in judged(replace(passing, profile=moved))[1]


@pytest.mark.parametrize("defenses", [[], [30] * 8, [0] * 7])
def test_no_positive_adjustment_is_unresolved(defenses: list[int]) -> None:
    """No defense, eight defenses and a zero average have no loss to observe."""
    assert judged(check(prior=[0] * 8 if defenses == [0] * 7 else None, defenses=defenses)) == (
        "unresolved", ("no_positive_automatic_loss",), None,
    )


@pytest.mark.parametrize("boundary", [
    datetime(2026, 8, 3, 5, tzinfo=UTC),  # a Monday weekly Reset
    datetime(2026, 8, 10, 5, tzinfo=UTC),  # a Season Reset, also a Monday
])
@pytest.mark.parametrize("start", [4980, 5600])
def test_monday_and_season_transitions_stay_unresolved(boundary: datetime, start: int) -> None:
    assert judged(check(boundary=boundary, start=start)) == (
        "unresolved", ("special_reset_unsupported",), None,
    )


def test_unfinished_and_unprocessed_checks_stay_provisional() -> None:
    passing = check()
    assert judged(replace(passing, work_status=None)) == (
        "unresolved", ("settlement_check_missing",), None)
    for status in ("pending", "waiting_retry", "claimed"):
        assert judged(replace(passing, work_status=status))[:2] == (
            "provisional", ("settlement_check_pending",))
    assert judged(replace(passing, early_recorded=False))[:2] == (
        "provisional", ("early_reading_pending",))
    unprocessed = replace(passing.battle_log, outcome=None, usable=False)
    assert judged(replace(passing, battle_log=unprocessed))[:2] == (
        "provisional", ("settlement_processing_pending",))
    for name, field in (("early_reading", "early"), ("settlement_profile", "profile"),
                        ("settlement_battle_log", "battle_log")):
        unusable = replace(getattr(passing, field), outcome="malformed", usable=False)
        assert judged(replace(passing, **{field: unusable})) == (
            "unresolved", (f"{name}_unusable",), None)
    assert judged(replace(passing, early=None))[1] == ("early_reading_unusable",)


def test_a_log_reaching_back_only_into_the_ended_day_settles() -> None:
    """On 7 October 2026 a named log reached back to 07:31 the day before,
    two hours short of that day: 10,490 checks failed so. Only the ended
    day's battles need the log; the day before's defenses come from its
    saved day."""
    passing = check()
    ended_from = RESET - DAY + 5 * MINUTE
    shorter = replace(passing, log_reports=(
        ("prior-late", "defense", ended_from - 60 * MINUTE, 30),
        *(report for report in passing.log_reports if report[2] >= ended_from),
    ))
    assert min(report[2] for report in shorter.log_reports) > RESET - 2 * DAY + 5 * MINUTE
    assert judged(shorter) == ("settled", (), 4804)
    # The saved day before's defenses, not the log's, set the loss: 8 for
    # 400 pool with 7 for 209 to floor(609 / 15) = 40 for the missing one.
    saved = settles_on(replace(shorter, previous_defenses=(8, 400)), 40, 4746 + 300 - 209 - 40)
    assert judged(saved) == ("settled", (), 4797)
    assert evaluate_boundary(saved).proof["automatic_loss_basis"]["prior_defense_loss"] == 400
    # A day before saved with a gap in its battle logs or a disputed battle.
    assert judged(replace(shorter, previous_defenses=None)) == (
        "unresolved", ("previous_day_defenses_unknown",), None,
    )


def test_ended_day_start_is_proven_by_the_day_before_or_the_season_rule() -> None:
    passing = check()
    balanced = DayEnd(
        state="Complete", final=4746, automatic_loss=None,
        automatic_state="not_applicable", boundary_kind=None, start=4700,
        end_reading=4746, next_start=4746, official_end=False, settled=None,
        boundary_at=RESET - DAY,
    )
    # No settled previous Reset: the day before's balanced end roots it.
    assert judged(replace(passing, root=None, previous_end=balanced)) == ("settled", (), 4804)
    # A day before disproved by a later reading, or only Partial, roots nothing.
    for unproven in (replace(balanced, later=4786), replace(balanced, state="Partial")):
        assert judged(replace(passing, root=None, previous_end=unproven)) == (
            "unresolved", ("independent_root_missing",), None,
        )
    # Day 1 ended at this Reset: every Legend I player started it at 5,000.
    day_2 = datetime(2026, 10, 6, 5, tzinfo=UTC)
    day_1 = settles_on(check(boundary=day_2, start=5000), 29, 5000 + 300 - 209 - 29)
    assert judged(replace(day_1, root=None, previous_defenses=None)) == ("settled", (), 5062)


def test_two_readings_minutes_apart_do_not_prove_a_days_end() -> None:
    """A Season's Day 1 starts at 5,000, and its eight defenses and its
    attack for 40 at 04:31 total zero, but readings at 05:01 and 05:02 both
    show 4,960 without that attack: the day is Inconsistent, its end is not
    proven and its board entry stays uncertain. Readings 20 minutes apart,
    the later one after 05:20, prove a Partial day's end."""
    from clashlens.boundary_manifest import _reset_total

    reset = ranked_day_for(RESET).season_start + DAY
    day_1 = DayEnd(
        state="Inconsistent", final=5000, automatic_loss=None,
        automatic_state="not_applicable", boundary_kind=None, start=5000,
        end_reading=4960, next_start=4960, official_end=False, settled=None,
        boundary_at=reset, end_read_at=reset + MINUTE, later=4960,
        later_at=reset + 2 * MINUTE, defense_slots=8, coverage_complete=True,
        last_landed=reset - 29 * MINUTE,
    )
    assert day_1.end_proof is None
    assert _reset_total(4960, day_1, (False, 0, 0)) == (4960, False)
    partial = replace(
        day_1, state="Partial", final=None, start=None, end_reading=5000,
        next_start=5000, later=5000, later_at=reset + 21 * MINUTE,
    )
    assert partial.end_proof == 5000
    assert _reset_total(4960, partial, (False, 0, 0)) == (5000, True)
    # 14 minutes after the end Reset reading, or before 05:20, is too soon.
    assert replace(partial, later_at=reset + 15 * MINUTE).end_proof is None
    assert replace(partial, end_read_at=reset, later_at=reset + 19 * MINUTE).end_proof is None
