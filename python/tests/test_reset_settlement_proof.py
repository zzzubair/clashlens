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
from clashlens.domain import TROPHY_ALLOCATION_RULE_VERSION
from clashlens.ranked_day_inputs import Reading
from clashlens.reconciliation import BattleContribution, CoverageObservation
from clashlens.reset_settlement import ProofInputs, Root, evaluate_boundary

RESET = datetime(2026, 8, 5, 5, tzinfo=UTC)  # an ordinary Wednesday Reset
MINUTE, DAY = timedelta(minutes=1), timedelta(days=1)


def reading(observation_id: int, completed: datetime, trophies: int | None) -> Reading:
    return Reading(observation_id, completed - timedelta(seconds=1), completed,
                   "processed", "parser", True, trophies)


def check(*, boundary: datetime = RESET, start: int = 4746,
          prior: list[int] | None = None, defenses: list[int] | None = None,
          attacks: list[int] | None = None) -> ProofInputs:
    """A named check passing every guard, on a settled previous Reset."""
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
            for identity, lens, at, amount in reports if at >= prior_from
        ),
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


def test_two_day_source_proof_requires_complete_classified_history() -> None:
    passing = check()
    coverage, battles = passing.log_coverage, passing.battles
    in_window = RESET - 30 * MINUTE

    def changed(**fields: object) -> ProofInputs:
        return replace(passing, battles=(replace(battles[0], **fields), *battles[1:]))

    cases = [
        # Fifty-row truncation: the log no longer reaches before both days.
        (replace(passing, log_reports=passing.log_reports[1:]), "battle_log_too_short"),
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
        # or an unreadable row from the two days, however late it arrived.
        (changed(battle_identity="late"), "battle_reports_changed_after_log"),
        (changed(trophy_amount=31), "battle_reports_changed_after_log"),
        (replace(passing, late_unreadable=(None,)), "late_battle_log_unreadable"),
        (replace(passing, late_unreadable=(in_window,)), "late_battle_log_unreadable"),
        (check(defenses=[30] * 9), "battle_count_exceeds_eight"),
    ]
    for inputs, reason in cases:
        state, reasons, _ = judged(inputs)
        assert state == "unresolved" and reason in reasons, (reason, reasons)
    # An unreadable row from the new day, or before both days, does not matter.
    assert judged(replace(passing, late_unreadable=(RESET + 6 * MINUTE,
                                                    RESET - 3 * DAY)))[0] == "settled"


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
