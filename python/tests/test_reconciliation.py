from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from reconciliation_support import (
    DAY,
    _counted_battles,
    _coverage,
    _input,
    _season_day_1,
)

from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    reading_rule,
    reconcile_ranked_day,
)


def test_active_ranked_day_is_live_without_claiming_complete_evidence() -> None:
    result = reconcile_ranked_day(
        _input(now=DAY.start + timedelta(hours=1), end_baseline_id=None)
    )

    assert result.state == "Live"
    assert result.confidence == "partial"
    assert result.reconciliation_rule_version == RECONCILIATION_RULE_VERSION


def test_complete_ranked_day_requires_paired_baselines_continuous_coverage_and_equation() -> (
    None
):
    result = reconcile_ranked_day(_input())

    assert result.state == "Complete"
    assert result.confidence == "exact"
    assert result.attack_count == 1
    assert result.defense_count == 1
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "confirmed"
    assert result.final_trophies_before_reset == 5940
    assert result.failure_reasons == ()


def test_automatic_defense_adjustment_can_be_calculated_without_end_confirmation() -> (
    None
):
    result = reconcile_ranked_day(
        _input(end_baseline_id=None, next_start_trophies=None)
    )

    assert result.state == "Partial"
    assert result.automatic_defense_loss == 70
    assert result.automatic_defense_evidence_state == "calculated"
    assert "missing_end_baseline" in result.failure_reasons


def test_zero_defenses_does_not_apply_automatic_adjustment_and_shields_stack() -> (
    None
):
    first = reconcile_ranked_day(
        _input(
            next_start_trophies=6000,
            contributions=(),
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )
    third = reconcile_ranked_day(
        _input(
            next_start_trophies=6000,
            contributions=(),
            previous_day=PreviousRankedDay(True, 0, 0, 2),
        )
    )

    assert first.automatic_defense_loss is None
    assert first.automatic_defense_evidence_state == "not_applicable"
    assert first.state == "Complete"
    assert (first.shield_state, first.shield_duration_days) == ("inferred_shielded", 1)
    assert (third.state, third.confidence, third.shield_state) == ("Complete", "exact", "inferred_shielded")
    assert third.shield_duration_days == 3


def test_zero_defense_day_takes_the_full_automatic_loss_its_next_reading_shows() -> None:
    # 6 October 2026: no defenses, previous day 8 defenses; the next Reset
    # reading is exactly 8 times the previous day's average below the end.
    cases = (
        # tag, previous loss, attack gains, start, next Reset reading
        ("#9R2LRYY8V", 305, (), 4695, 4391),
        ("#L998PVYV0", 275, (), 4725, 4453),
        ("#Q0RUJC9J2", 249, (31, 31, 31, 31, 31, 31, 32, 32), 4751, 4753),
    )
    for tag, previous_loss, gains, start, reading in cases:
        result = reconcile_ranked_day(_input(
            start_trophies=start, next_start_trophies=reading,
            contributions=tuple(BattleContribution(f"{tag}-attack-{index}", "offense", gain)
                                for index, gain in enumerate(gains)),
            previous_day=PreviousRankedDay(True, 8, previous_loss, 0),
        ))

        assert result.state == "Complete", tag
        assert result.confidence == "exact"
        assert result.automatic_defense_loss == start + sum(gains) - reading
        assert result.automatic_defense_evidence_state == "confirmed"
        assert result.final_trophies_before_reset == reading
        assert result.unexplained_residual == 0
        assert result.shield_state == "not_inferred"

    # One of the 86 quiet days kept its trophies, so it is still uncharged.
    quiet = reconcile_ranked_day(_input(
        start_trophies=4991, next_start_trophies=4991, contributions=(),
        previous_day=PreviousRankedDay(True, 7, 232, 0),
    ))
    # Any other drop is still a mismatch, and Day 1 has no previous day.
    other = reconcile_ranked_day(_input(
        start_trophies=4695, next_start_trophies=4392, contributions=(),
        previous_day=PreviousRankedDay(True, 8, 305, 0),
    ))
    day_1 = reconcile_ranked_day(_input(
        start_trophies=4695, next_start_trophies=4391, contributions=(),
        previous_day=PreviousRankedDay(True, 8, 305, 0), season_first_day=True,
    ))

    assert (quiet.state, quiet.automatic_defense_loss) == ("Complete", None)
    assert quiet.shield_state == "inferred_shielded"
    assert other.state == day_1.state == "Inconsistent"
    assert other.automatic_defense_loss is day_1.automatic_defense_loss is None


def test_zero_defense_day_read_before_its_loss_takes_it_from_a_later_reading() -> None:
    # As #9R2LRYY8V on 6 October 2026, but with the Reset reading at 05:02
    # before the game charged 8 times the previous day's average (305 / 8),
    # and a reading at 05:10, before any new-day battle, after it.
    day = _input(
        start_trophies=4695, next_start_trophies=4695, contributions=(),
        previous_day=PreviousRankedDay(True, 8, 305, 0),
    )
    later_at = DAY.end + timedelta(minutes=10)
    charged = reconcile_ranked_day(
        replace(day, readings=(reading_rule.Reading(later_at, 4391),))
    )
    # A quiet day's later reading still shows its start: it stays uncharged.
    quiet = reconcile_ranked_day(
        replace(day, readings=(reading_rule.Reading(later_at, 4695),))
    )

    assert (charged.state, charged.confidence) == ("Complete", "exact")
    assert charged.automatic_defense_loss == 304
    assert charged.automatic_defense_evidence_state == "confirmed"
    assert charged.final_trophies_before_reset == 4391
    assert charged.next_start_trophies == 4391
    assert charged.unsettled_automatic_loss == 0
    assert charged.failure_reasons == ()
    assert (quiet.state, quiet.automatic_defense_loss) == ("Complete", None)
    assert quiet.next_start_trophies == 4695

    # As #Q0RUJC9J2, but its Reset reading also missed the day's 250 attack
    # gain: only the later reading shows both the gain and the 248 loss.
    gains = (31, 31, 31, 31, 31, 31, 32, 32)
    both = _input(
        start_trophies=4751, next_start_trophies=4751,
        contributions=tuple(
            BattleContribution(f"#Q0RUJC9J2-attack-{index}", "offense", gain)
            for index, gain in enumerate(gains)
        ),
        previous_day=PreviousRankedDay(True, 8, 249, 0),
    )
    unread = reconcile_ranked_day(both)
    settled = reconcile_ranked_day(
        replace(both, readings=(reading_rule.Reading(later_at, 4753),))
    )

    assert (unread.state, unread.failure_reasons) == ("Partial", ("end_reading_unverified",))
    assert (settled.state, settled.confidence) == ("Complete", "exact")
    assert settled.automatic_defense_loss == 248
    assert settled.final_trophies_before_reset == settled.next_start_trophies == 4753
    assert settled.formula_components["next_start_reading_correction"] == 2

    # The next day starts from the Reset reading less the loss.
    next_day = reconcile_ranked_day(
        _input(
            ranked_day=ranked_day_for(DAY.end + timedelta(hours=1)),
            now=DAY.end + timedelta(days=1, minutes=1),
            start_baseline_id=11,
            end_baseline_id=12,
            start_trophies=4695,
            next_start_trophies=4391 + 40,
            coverage_observations=tuple(
                replace(item, observed_at=item.observed_at + timedelta(days=1))
                for item in _coverage()
            ),
            contributions=(
                *_counted_battles("#9R2LRYY8V-2", "offense", 8, 280),
                *_counted_battles("#9R2LRYY8V-2", "defense", 8, 240),
            ),
            previous_day=PreviousRankedDay(
                True, 0, 0, 0, end_baseline_id=11, unsettled_automatic_loss=304,
            ),
        )
    )

    assert (next_day.state, next_day.start_trophies) == ("Complete", 4391)


def test_dropped_zero_defense_day_stays_partial() -> None:
    # Dropped from Legend I at a weekly Reset: later profiles show Legend II,
    # and a Reset reading below the day's end can be the automatic loss or a
    # credit the game had not added yet, so no reading proves the loss.
    later_at = DAY.end + timedelta(minutes=9)
    day = _input(
        start_trophies=4900, next_start_trophies=4940,
        contributions=(BattleContribution("attack-1", "offense", 40),),
        previous_day=PreviousRankedDay(True, 8, 240, 0),
        end_baseline_evidence={"dropped_from_legend_i": True},
    )
    # The Reset reading misses the 240 credit, the size of the loss; a later
    # Legend I reading shows the credit and no loss.
    delayed = replace(
        day, start_trophies=4700, next_start_trophies=4700,
        contributions=(BattleContribution("attack-1", "offense", 240),),
    )
    cases = (
        day, replace(day, readings=(reading_rule.Reading(later_at, 4700),)),
        replace(day, previous_day=None),
        replace(day, previous_day=PreviousRankedDay(False, 8, 240, 0)),
        delayed, replace(delayed, readings=(reading_rule.Reading(later_at, 4940),)),
    )
    for case in cases:
        result = reconcile_ranked_day(case)
        assert result.state == "Partial"
        assert "automatic_defense_basis_unavailable" in result.failure_reasons
        assert result.automatic_defense_loss is None
    assert reconcile_ranked_day(replace(day, end_baseline_evidence={})).state == (
        "Complete"
    )


def test_later_reading_settles_a_reset_reading_missing_the_days_credit() -> None:
    # 5 October 2026, Season Day 1, then 6 October. The Reset reading left
    # out attack gains the battles prove; a reading after it, before any new
    # day battle, is exactly the calculated next start.
    cases = (
        # tag, each attack's gain in report order (production logs), defenses,
        # defense loss, reading, later reading, next day's gain, loss and end
        ("#P20G0CUJY", (40, 40, 40, 40, 40, 40, 28, 40), 8, 234, 4766, 5074, 267, 273, 5068),
        ("#8Q20CULJP", (40, 31, 29, 40, 40, 31, 40, 30), 8, 299, 4701, 4982, 277, 216, 5043),
        # 7 defenses (a loss in the later reading); the Reset one lacks 6 attacks.
        ("#L9L82J90J", (30, 15, 40, 29, 40, 40, 40, 40), 7, 204, 4841, 5041, 245, 276, 5010),
    )
    later_at = DAY.end + timedelta(minutes=10)
    for tag, gain, defenses, loss, reading, later, gain_2, loss_2, end_2 in cases:
        day_1 = _input(
            start_trophies=5000,
            next_start_trophies=reading,
            contributions=(
                *(BattleContribution(f"{tag}-attack-{i}", "offense", g) for i, g in enumerate(gain)),
                *_counted_battles(tag, "defense", defenses, loss),
            ),
            season_first_day=True,
        )
        unsettled = reconcile_ranked_day(day_1)
        wrong_later = reconcile_ranked_day(
            replace(day_1, readings=(reading_rule.Reading(later_at, later + 1),))
        )
        first = reconcile_ranked_day(
            replace(day_1, readings=(reading_rule.Reading(later_at, later),))
        )

        assert (unsettled.state, wrong_later.state) == ("Partial", "Inconsistent"), tag
        # A later reading that equals the calculated end proves the day.
        assert (first.state, first.confidence) == ("Complete", "exact")
        assert first.final_trophies_before_reset == later
        assert first.next_start_trophies == later
        assert first.unexplained_residual == 0
        assert first.formula_components["next_start_reading_trophies"] == reading
        assert first.formula_components["next_start_reading_correction"] == later - reading

        second = reconcile_ranked_day(
            _input(
                ranked_day=ranked_day_for(DAY.end + timedelta(hours=1)),
                now=DAY.end + timedelta(days=1, minutes=1),
                start_baseline_id=11,
                end_baseline_id=12,
                start_trophies=reading,
                next_start_trophies=end_2,
                coverage_observations=tuple(
                    replace(item, observed_at=item.observed_at + timedelta(days=1))
                    for item in _coverage()
                ),
                contributions=(
                    *_counted_battles(f"{tag}-2", "offense", 8, gain_2),
                    *_counted_battles(f"{tag}-2", "defense", 8, loss_2),
                ),
                previous_day=PreviousRankedDay(
                    True, defenses, loss, 0, end_baseline_id=11,
                    reset_reading_correction=later - reading,
                ),
            )
        )

        assert (second.state, second.confidence) == ("Complete", "exact"), tag
        assert second.start_trophies == later
        assert second.final_trophies_before_reset == end_2
        assert second.formula_components["start_reading_trophies"] == reading
        assert second.formula_components["start_reading_correction"] == later - reading


def test_battles_landing_after_the_reset_reading_settle_the_day() -> None:
    # 7 October 2026 Reset readings taken before the ended day's last battle
    # reached the profile, with no later reading before the next battle.
    reading_at = DAY.end + timedelta(seconds=31)

    def day(late: BattleContribution, start: int, reading: int, *others, **overrides):
        values = {
            "start_trophies": start,
            "next_start_trophies": reading,
            "end_baseline_evidence": {"profile": {"observed_at": reading_at.isoformat()}},
            "contributions": (*others, late),
            "previous_day": PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10),
        }
        return reconcile_ranked_day(_input(**(values | overrides)))

    defenses = tuple(
        BattleContribution(f"defense-{index}", "defense", 10,
                           battle_timestamp=DAY.start + timedelta(hours=index + 1))
        for index in range(7)
    )
    # #2GL8CJL: read 4,839 at 05:00:31; its attack reported at 05:02:15 adds 40.
    attack = BattleContribution(
        "attack-late", "offense", 40, battle_timestamp=DAY.end + timedelta(seconds=135)
    )
    noon_attack = BattleContribution(
        "attack-noon", "offense", 70, battle_timestamp=DAY.start + timedelta(hours=7)
    )
    defense_8 = BattleContribution(
        "defense-7", "defense", 10, battle_timestamp=DAY.start + timedelta(hours=9)
    )
    late_attack = day(attack, 4849, 4839, noon_attack, *defenses, defense_8)
    # #82RV9CV8C: read 5,113 at 05:01:00; its 155-second defense from 04:57:32
    # cost 32 and had not reached the profile.
    defense = BattleContribution(
        "defense-late", "defense", 32, battle_timestamp=DAY.end - timedelta(seconds=148),
        battle_seconds=155,
    )
    late_defense = day(defense, 5183, 5113, *defenses)
    # Short of an attack landed 15 minutes before, the reading shows the lag.
    early = replace(attack, battle_timestamp=DAY.end - timedelta(minutes=15))
    too_early = day(early, 4849, 4839, noon_attack, *defenses, defense_8)

    assert (late_attack.state, late_attack.confidence) == ("Complete", "inferred")
    assert late_attack.next_start_trophies == 4879
    assert late_attack.formula_components["next_start_reading_correction"] == 40
    assert late_attack.formula_components["next_start_battles_after_reading"] == [
        "attack-late"
    ]
    assert (late_defense.state, late_defense.next_start_trophies) == ("Complete", 5081)
    assert late_defense.formula_components["next_start_reading_correction"] == -32
    assert (too_early.state, too_early.failure_reasons) == ("Partial", ("end_reading_unverified",))

    # Start 5,000, 8 attacks for 280, 7 defenses for 280 after 8 for 320 the
    # day before: an automatic loss of 40. Read 5,040 at 05:00:31, before the
    # last defense's 40, landing at 05:01:00, and the automatic 40.
    attacks = tuple(
        BattleContribution(f"attack-{hour}", "offense", 35,
                           battle_timestamp=DAY.start + timedelta(hours=hour, minutes=15))
        for hour in range(8)
    )
    defenses_40 = tuple(replace(item, trophy_amount=40) for item in defenses)
    last_defense = replace(
        defense, trophy_amount=40, battle_timestamp=DAY.end - timedelta(seconds=60),
        battle_seconds=120,
    )
    pending = day(
        last_defense, 5000, 5040, *attacks, *defenses_40[:6],
        previous_day=PreviousRankedDay(True, 8, 320, 0, end_baseline_id=10),
    )
    assert (pending.state, pending.confidence) == ("Complete", "inferred")
    assert (pending.automatic_defense_loss, pending.unsettled_automatic_loss) == (40, 40)
    assert pending.next_start_trophies == pending.final_trophies_before_reset == 4960
    assert pending.formula_components["next_start_reading_correction"] == -40
    assert pending.formula_components["next_start_battles_after_reading"] == ["defense-late"]
    next_day = reconcile_ranked_day(_input(
        ranked_day=ranked_day_for(DAY.end + timedelta(hours=1)),
        start_baseline_id=11,
        start_trophies=5040,
        previous_day=PreviousRankedDay(
            True, 7, 280, 0, end_baseline_id=11, unsettled_automatic_loss=40,
            reset_reading_correction=-40,
        ),
    ))
    assert next_day.start_trophies == 4960

    # A start its Inconsistent previous day left 40 gains short of 6,040: this
    # day, 8 attacks for 280 and 8 defenses for 320, really ends at 6,000, as
    # read. A last 40 defense landing at 04:58 only looks missed by it.
    unproven_day = (
        replace(last_defense, battle_timestamp=DAY.end - timedelta(minutes=4)),
        6000, 6000, *attacks, *defenses_40,
    )
    proven = day(*unproven_day)
    unproven = day(
        *unproven_day,
        previous_day=PreviousRankedDay(
            False, 8, 320, 0, state="Inconsistent", end_baseline_id=10
        ),
    )
    assert (proven.state, proven.next_start_trophies) == ("Complete", 5960)
    assert (unproven.state, unproven.unexplained_residual) == ("Partial", None)
    assert unproven.failure_reasons == ("end_reading_unverified",)
    assert "next_start_battles_after_reading" not in unproven.formula_components


def test_a_later_reading_other_than_the_end_disproves_missed_battles() -> None:
    # Season Day 1 from a proven 5,000: 7 attacks for 224 and 7 defenses for
    # 224 calculate an end of 5,000, but the game charged 32 for the missing
    # defense. The last attack, +32, reported at 05:02, after the 05:00:31
    # reading of 4,968; readings at 05:10 and 05:20 show 4,968 too.
    reading_at = DAY.end + timedelta(seconds=31)
    day = _input(
        start_trophies=5000,
        next_start_trophies=4968,
        season_first_day=True,
        end_baseline_evidence={"profile": {"observed_at": reading_at.isoformat()}},
        contributions=(
            *(BattleContribution(f"attack-{hour}", "offense", 32,
                                 battle_timestamp=DAY.start + timedelta(hours=hour))
              for hour in range(6)),
            BattleContribution("attack-late", "offense", 32,
                               battle_timestamp=DAY.end + timedelta(minutes=2)),
            *(BattleContribution(f"defense-{hour}", "defense", 32,
                                 battle_timestamp=DAY.start + timedelta(hours=hour, minutes=30))
              for hour in range(7)),
        ),
    )
    guessed = reconcile_ranked_day(day)
    disproved = reconcile_ranked_day(
        replace(day, readings=(reading_rule.Reading(DAY.end + timedelta(minutes=20), 4968),))
    )

    assert (guessed.state, guessed.next_start_trophies) == ("Complete", 5000)
    assert disproved.state == "Partial"
    assert disproved.input_evidence["end_reading"]["lagged_credits"] == ["attack-late"]
    assert "next_start_battles_after_reading" not in disproved.formula_components


def test_coverage_gap_or_missing_overlap_makes_ended_day_partial() -> None:
    no_overlap = list(_coverage())
    no_overlap[1] = CoverageObservation(
        observed_at=no_overlap[1].observed_at,
        row_count=50,
        battle_identities=("different",),
        has_row_gap=False,
    )

    gap_result = reconcile_ranked_day(_input(coverage_observations=_coverage(gap=True)))
    overlap_result = reconcile_ranked_day(
        _input(coverage_observations=tuple(no_overlap))
    )

    assert gap_result.state == "Malformed"
    assert "battle_log_row_gap" in gap_result.failure_reasons
    assert overlap_result.state == "Partial"
    assert "battle_log_overlap_gap" in overlap_result.failure_reasons


def test_shared_non_legend_rows_prove_full_logs_overlap() -> None:
    # Player 24106 on 6 October 2026: two full logs of non-Legend battles,
    # no Legend battle in either, 48 of 50 saved rows the same.
    first, _, last = _coverage()
    full_logs = (
        CoverageObservation(
            observed_at=first.observed_at, row_count=50, battle_identities=(),
            has_row_gap=False, observation_id=101,
            source_row_ids=tuple(range(1, 51)),
        ),
        CoverageObservation(
            observed_at=first.observed_at + timedelta(minutes=18), row_count=50,
            battle_identities=(), has_row_gap=False, observation_id=102,
            source_row_ids=(51, 52, *range(1, 49)),
        ),
        last,
    )
    turned_over = (
        *full_logs[:1],
        CoverageObservation(
            observed_at=full_logs[1].observed_at, row_count=50,
            battle_identities=(), has_row_gap=False, observation_id=102,
            source_row_ids=tuple(range(51, 101)),
        ),
        last,
    )

    overlapping = reconcile_ranked_day(_input(coverage_observations=full_logs))
    gap = reconcile_ranked_day(_input(coverage_observations=turned_over))

    assert overlapping.state == "Complete"
    assert gap.state == "Partial"
    assert "battle_log_overlap_gap" in gap.failure_reasons


def test_weekly_and_season_reset_adjustments_reconcile_against_5000_baseline() -> None:
    weekly = reconcile_ranked_day(
        _input(
            start_trophies=4900,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            next_start_trophies=5000,
            boundary_kind="weekly",
        )
    )
    season = reconcile_ranked_day(
        _input(
            contributions=(BattleContribution("attack-1", "offense", 20),),
            next_start_trophies=5000,
            boundary_kind="season",
        )
    )

    assert weekly.state == "Complete"
    assert weekly.final_trophies_before_reset == 4920
    assert weekly.boundary_adjustment == 80
    assert weekly.boundary_adjustment_type == "weekly_reset"
    assert season.state == "Complete"
    assert season.final_trophies_before_reset == 6020
    assert season.boundary_adjustment == -1020
    assert season.boundary_adjustment_type == "season_reset"
    # The day's own change leaves out the reset, so trends see no fake drop.
    assert weekly.net_trophy_change == season.net_trophy_change == 20


def test_5000_after_a_reset_does_not_confirm_the_automatic_loss_or_final_total() -> (
    None
):
    # October 4, day 28: the same battles with two different previous-day
    # losses give two different final totals, and both become 5,000.
    sunday = ranked_day_for(datetime(2026, 10, 4, 12, tzinfo=UTC))
    previous_losses = (20, 80)
    season = [
        reconcile_ranked_day(
            _input(
                ranked_day=sunday,
                now=sunday.end + timedelta(minutes=1),
                next_start_trophies=5000,
                boundary_kind="season",
                previous_day=PreviousRankedDay(True, 2, loss, 0),
            )
        )
        for loss in previous_losses
    ]
    below_floor = reconcile_ranked_day(
        _input(start_trophies=4900, next_start_trophies=5000, boundary_kind="weekly")
    )
    # Ending on exactly 5,000 hides a missed loss just the same.
    at_floor = reconcile_ranked_day(
        _input(start_trophies=5060, next_start_trophies=5000, boundary_kind="weekly")
    )
    season_mismatch = reconcile_ranked_day(
        _input(next_start_trophies=4999, boundary_kind="season")
    )
    above_floor = reconcile_ranked_day(
        _input(next_start_trophies=5940, boundary_kind="weekly")
    )

    assert [r.final_trophies_before_reset for r in season] == [5940, 5800]
    for result in (*season, below_floor, at_floor):
        assert result.state == "Complete"
        assert result.unexplained_residual == 0
        assert result.automatic_defense_evidence_state == "calculated"
        assert result.confidence == "inferred"
    assert below_floor.boundary_adjustment_type == "weekly_reset"
    assert at_floor.final_trophies_before_reset == 5000
    assert (season_mismatch.state, season_mismatch.confidence) == (
        "Inconsistent",
        "uncertain",
    )
    # Above 5,000 a weekly Reset keeps the total, so the reading still proves it.
    assert above_floor.automatic_defense_evidence_state == "confirmed"
    assert above_floor.confidence == "exact"


def test_automatic_defense_uses_previous_and_current_observed_losses() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=5858,
            contributions=(BattleContribution("defense-1", "defense", 30),),
            previous_day=PreviousRankedDay(
                complete=True,
                observed_defense_count=2,
                observed_defense_loss=20,
                shield_run_length=0,
            ),
        )
    )

    assert result.automatic_defense_loss == 112
    assert result.final_trophies_before_reset == 5858
    assert result.state == "Complete"


def test_season_day_1_automatic_defense_loss_needs_no_previous_season_day() -> None:
    # Production, 5 October 2026 (Day 1): player #QPJURYV8, first tracked
    # during Day 1, so the previous Season's last day was never tracked. Their
    # 05:00 reading on 6 October, 5,009, is 5,000 plus the battles minus
    # floor(202 / 7) for the one missing defense.
    result = _season_day_1(
        (21, 27, 30, 40, 29, 22, 30, 40),
        (40, 28, 40, 15, 11, 28, 40),
        None,
        5009,
    )

    assert result.automatic_defense_loss == 28
    assert result.final_trophies_before_reset == 5009
    assert result.state == "Complete"
    assert "automatic_defense_basis_unavailable" not in result.failure_reasons


def test_season_day_1_automatic_defense_loss_leaves_out_the_previous_season() -> None:
    # Production, 5 October 2026 (Day 1): player #LGLCV8J2U, tracked all
    # along, lost 287 on 8 defenses on the previous Season's last day. Their
    # profile at 05:09 on 6 October, before any Day 2 battle, read 5,075:
    # 5,129 minus floor(165 / 6) * 2 = 54, not the 64 the previous Season's
    # day would give. The 05:00 reading, 5,129, came before the loss.
    result = _season_day_1(
        (40, 40, 28, 40, 26, 40, 40, 40),
        (18, 24, 40, 27, 28, 28),
        PreviousRankedDay(True, 8, 287, 0),
        5129,
    )

    assert result.automatic_defense_loss == 54
    assert result.final_trophies_before_reset == 5075
    assert result.unsettled_automatic_loss == 54
    assert (result.state, result.confidence) == ("Complete", "inferred")


def test_shield_is_not_inferred_when_the_player_has_an_attack() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            previous_day=PreviousRankedDay(
                complete=True,
                observed_defense_count=8,
                observed_defense_loss=100,
                shield_run_length=0,
            ),
        )
    )

    assert result.shield_state == "not_inferred"
    assert result.shield_duration_days is None


def test_weekly_reset_is_not_applied_when_final_trophies_are_at_or_above_5000() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6020,
            contributions=(BattleContribution("attack-1", "offense", 20),),
            boundary_kind="weekly",
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.final_trophies_before_reset == 6020
    assert result.boundary_adjustment == 0
    assert result.boundary_adjustment_type is None
    assert result.state == "Complete"


def test_reconciliation_exposes_net_change_boundary_and_unexplained_residual() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=5941,
            contributions=(
                BattleContribution("attack-1", "offense", 20),
                BattleContribution("defense-1", "defense", 10),
            ),
        )
    )

    assert result.attack_trophy_gain == 20
    assert result.observed_defense_loss == 10
    assert result.net_trophy_change == -60
    assert result.boundary_adjustment == 0
    assert result.unexplained_residual == 1
    assert result.formula_components["expected_next_start_trophies"] == 5940
    assert result.state == "Inconsistent"
    assert "trophy_equation_mismatch" in result.failure_reasons


def test_malformed_battle_log_evidence_is_not_reported_as_a_normal_partial_day() -> (
    None
):
    result = reconcile_ranked_day(
        _input(
            coverage_observations=_coverage(gap=True),
        )
    )

    assert result.state == "Malformed"
    assert "battle_log_row_gap" in result.failure_reasons


def test_shield_requires_observation_coverage() -> None:
    result = reconcile_ranked_day(
        _input(
            start_trophies=6000,
            next_start_trophies=6000,
            contributions=(),
            coverage_observations=(),
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.shield_state == "unknown"
    assert result.shield_duration_days is None
    assert result.automatic_defense_evidence_state == "not_applicable"


def test_shield_is_not_inferred_when_observed_trophies_changed_without_events() -> None:
    # Audit examples #22VRLQ29V (4,885 -> 4,886) and #2QP9LCLU (5,029 -> 5,004).
    for start, next_start in ((4885, 4886), (5029, 5004)):
        result = reconcile_ranked_day(
            _input(
                start_trophies=start,
                next_start_trophies=next_start,
                contributions=(),
                previous_day=PreviousRankedDay(True, 8, 100, 0),
            )
        )

        assert result.final_trophies_before_reset == start
        assert result.unexplained_residual == next_start - start
        assert result.state == "Inconsistent"
        assert result.confidence == "uncertain"
        assert result.shield_state == "unknown"
        assert result.shield_duration_days is None


def test_repeated_and_two_sided_contributions_count_once_per_own_perspective() -> None:
    result = reconcile_ranked_day(
        _input(
            contributions=(
                BattleContribution("shared-battle", "offense", 20),
                BattleContribution("shared-battle", "offense", 20),
                BattleContribution("shared-battle", "defense", 10),
                BattleContribution("shared-battle", "defense", 10),
            )
        )
    )

    assert result.state == "Complete"
    assert result.attack_count == 1
    assert result.defense_count == 1
    assert result.attack_trophy_gain == 20
    assert result.observed_defense_loss == 10
    included = [
        item for item in result.input_evidence["contributions"] if item["included"]
    ]
    excluded = [
        item for item in result.input_evidence["contributions"] if not item["included"]
    ]
    assert {(item["lens"], item["included"]) for item in included} == {
        ("offense", True),
        ("defense", True),
    }
    assert len(excluded) == 2


def test_incomplete_coverage_keeps_one_to_seven_defense_adjustment_unknown() -> None:
    result = reconcile_ranked_day(
        _input(
            coverage_observations=_coverage(gap=True),
            contributions=(BattleContribution("defense-1", "defense", 10),),
            next_start_trophies=None,
        )
    )

    assert result.automatic_defense_loss is None
    assert result.automatic_defense_evidence_state == "unknown"
    assert result.state == "Malformed"
    assert "automatic_defense_basis_unavailable" in result.failure_reasons


def test_post_boundary_baseline_battle_log_responses_can_form_a_complete_chain() -> (
    None
):
    # A reset-baseline sweep requests its endpoints after the 05:00 UTC
    # boundary, so both baseline battle-log responses are observed strictly
    # after the ranked-day boundary timestamps. The chain must be bound to the
    # exact battle-log observations that the start and end sweeps selected,
    # not to impossible boundary-time observations.
    result = reconcile_ranked_day(
        _input(
            coverage_observations=(
                CoverageObservation(
                    observed_at=DAY.start + timedelta(seconds=5),
                    row_count=50,
                    battle_identities=("older", "shared"),
                    has_row_gap=False,
                    observation_id=101,
                ),
                CoverageObservation(
                    observed_at=DAY.start + timedelta(hours=12),
                    row_count=50,
                    battle_identities=("shared", "daily"),
                    has_row_gap=False,
                    observation_id=102,
                ),
                CoverageObservation(
                    observed_at=DAY.end + timedelta(seconds=5),
                    row_count=2,
                    battle_identities=("daily",),
                    has_row_gap=False,
                    observation_id=103,
                ),
            ),
            start_baseline_battle_log_observation_id=101,
            end_baseline_battle_log_observation_id=103,
        )
    )

    assert result.state == "Complete"
    assert result.coverage_complete is True
    assert result.failure_reasons == ()


def test_more_than_eight_defenses_is_a_visible_partial_anomaly() -> None:
    result = reconcile_ranked_day(
        _input(
            contributions=tuple(
                BattleContribution(f"defense-{index}", "defense", 10)
                for index in range(9)
            ),
            next_start_trophies=5910,
            previous_day=PreviousRankedDay(True, 8, 100, 0),
        )
    )

    assert result.defense_count == 9
    assert result.automatic_defense_loss is None
    assert result.state == "Partial"
    assert "defense_count_exceeds_eight" in result.failure_reasons


def test_a_finished_day_with_eight_attacks_and_eight_defenses_has_a_known_net() -> (
    None
):
    battles = (
        *(BattleContribution(f"a{n}", "offense", 40) for n in range(7)),
        BattleContribution("a7", "offense", 20),
        *(BattleContribution(f"d{n}", "defense", 40) for n in range(7)),
        BattleContribution("d7", "defense", 31),
    )
    # Prodigi's Day 24: no Reset checks, so no trophy readings and no proof
    # the player was in Legend I, and an unreadable row in a battle log read.
    day = {
        "start_baseline_id": None,
        "end_baseline_id": None,
        "start_trophies": None,
        "next_start_trophies": None,
        "start_baseline_battle_log_observation_id": None,
        "end_baseline_battle_log_observation_id": None,
        "contributions": battles,
        "player_eligible": False,
        "malformed_evidence": True,
    }
    result = reconcile_ranked_day(_input(**day))
    assert result.net_trophy_change == 300 - 311
    assert result.state == "Malformed"
    assert "player_not_eligible" in result.failure_reasons

    ninth = BattleContribution("a8", "offense", 10)
    unknown = [
        # A ninth attack is a data error, not a valid day.
        {"contributions": (*battles, ninth)},
        # Seven defenses leave the automatic defense loss unknown.
        {"contributions": battles[:-1]},
        # Seven attacks may hide a missing one.
        {"contributions": battles[1:]},
        {"perspective_disagreement": True},
        # The day is still in progress.
        {"now": DAY.end - timedelta(hours=1)},
    ]
    results = [
        reconcile_ranked_day(_input(**{**day, **change})) for change in unknown
    ]
    assert [result.net_trophy_change for result in results] == [None] * len(unknown)
    assert "attack_count_exceeds_eight" in results[0].failure_reasons


def test_incomplete_coverage_withholds_the_final_total_and_net() -> None:
    no_overlap = list(_coverage())
    no_overlap[1] = CoverageObservation(
        observed_at=no_overlap[1].observed_at,
        row_count=50,
        battle_identities=("different",),
        has_row_gap=False,
        observation_id=102,
    )
    attack = BattleContribution("attack-1", "offense", 20)
    attacks = tuple(BattleContribution(f"a{n}", "offense", 30) for n in range(8))
    defenses = tuple(BattleContribution(f"d{n}", "defense", 25) for n in range(8))
    sunday = ranked_day_for(datetime(2026, 10, 4, 12, tzinfo=UTC))
    days = {
        # One attack and no defenses were kept, but a log page was missed.
        "missing battles": {"contributions": (attack,), "next_start_trophies": None},
        # Eight attacks and no defenses: a missed defense would not show.
        "missing defense": {"contributions": attacks, "next_start_trophies": None},
        # Seven attacks and eight defenses: a missed attack would not show.
        "missing attack": {
            "contributions": (*attacks[1:], *defenses),
            "next_start_trophies": None,
        },
        # Day 28: the Season reset makes any total 5,000, so the next start
        # cannot expose the missed battles.
        "season close": {
            "ranked_day": sunday,
            "now": sunday.end + timedelta(minutes=1),
            "contributions": (attack,),
            "next_start_trophies": 5000,
            "boundary_kind": "season",
        },
    }
    for name, day in days.items():
        result = reconcile_ranked_day(
            _input(coverage_observations=tuple(no_overlap), **day)
        )
        assert result.coverage_complete is False, name
        assert result.final_trophies_before_reset is None, name
        assert result.net_trophy_change is None, name
        assert result.expected_next_start_trophies is None, name
        assert result.boundary_adjustment_type is None, name
        assert result.formula_components["final_trophies_before_reset"] is None, name
        assert (result.state, result.confidence) == ("Partial", "uncertain"), name
        assert "battle_log_overlap_gap" in result.failure_reasons, name
    assert reconcile_ranked_day(
        _input(coverage_observations=tuple(no_overlap), **days["season close"])
    ).failure_reasons == ("battle_log_overlap_gap",)

    # With every battle captured the same day keeps its total.
    complete = reconcile_ranked_day(_input(**days["season close"]))
    assert (complete.final_trophies_before_reset, complete.net_trophy_change) == (
        6020,
        20,
    )
    # All 8 attacks and 8 defenses on record leave nothing to miss.
    full = reconcile_ranked_day(
        _input(
            coverage_observations=tuple(no_overlap),
            contributions=(*attacks, *defenses),
            next_start_trophies=None,
        )
    )
    assert (full.final_trophies_before_reset, full.net_trophy_change) == (6040, 40)


def test_a_monday_after_an_unfinished_day_starts_at_5000_when_its_readings_prove_the_raise() -> None:
    # The Legend day from Monday 9 November 2026's weekly Reset, after a
    # Sunday left Inconsistent, so it gave no next start to raise. Monday's
    # Reset reading, 4,884, came before the game raised the player to 5,000.
    # 8 attacks for 320 and 7 defenses for 210, after 8 defenses for 240,
    # lose floor(450 / 15) = 30 for the missing defense: 5,080 from 5,000,
    # 4,964 from the reading, 116 short.
    monday = ranked_day_for(datetime(2026, 11, 9, 12, tzinfo=UTC))
    sunday = PreviousRankedDay(
        complete=False, observed_defense_count=8, observed_defense_loss=240,
        shield_run_length=0, state="Inconsistent", end_baseline_id=10, battles_known=True)
    day = _input(
        ranked_day=monday, now=monday.end + timedelta(minutes=5), start_trophies=4884,
        contributions=(*_counted_battles("a", "offense", 8, 320),
                       *_counted_battles("d", "defense", 7, 210)),
        previous_day=sunday)

    raised = reconcile_ranked_day(replace(day, next_start_trophies=5080))
    as_read = reconcile_ranked_day(replace(day, next_start_trophies=4964))
    neither = reconcile_ranked_day(replace(day, next_start_trophies=5070))
    tuesday = ranked_day_for(datetime(2026, 11, 10, 12, tzinfo=UTC))
    not_monday = reconcile_ranked_day(replace(
        day, ranked_day=tuesday, now=tuesday.end + timedelta(minutes=5), next_start_trophies=5080))
    expected = reconcile_ranked_day(replace(
        day, next_start_trophies=5080, previous_day=replace(sunday, expected_next_start=4884)))

    assert (raised.state, raised.confidence) == ("Complete", "exact")
    assert (raised.start_trophies, raised.final_trophies_before_reset) == (5000, 5080)
    assert raised.unexplained_residual == 0
    assert raised.formula_components["start_reading_trophies"] == 4884
    assert raised.formula_components["start_reading_correction"] == 116
    assert raised.formula_components["start_weekly_raise_proven"] is True
    # A reading that fits the start as read keeps it.
    assert (as_read.state, as_read.start_trophies) == ("Complete", 4884)
    assert "start_weekly_raise_proven" not in as_read.formula_components
    # One that fits neither proves no raise: the day stays as read.
    assert (neither.state, neither.start_trophies) == ("Inconsistent", 4884)
    assert neither.unexplained_residual == 106
    assert "start_weekly_raise_proven" not in neither.formula_components
    # Only a weekly Reset raises, and a day before with a next start says
    # what it is.
    assert (not_monday.state, not_monday.start_trophies) == ("Inconsistent", 4884)
    assert (expected.state, expected.start_trophies) == ("Inconsistent", 4884)
