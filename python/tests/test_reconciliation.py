from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from clashlens.api_db import _screen_events
from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    BATTLE_EVENT_SERIALIZATION_VERSION,
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    PreviousRankedDay,
    ReconciliationInput,
    reads_later_reading,
    reconcile_ranked_day,
    serialize_ranked_day_battles,
)

DAY = ranked_day_for(datetime(2026, 8, 4, 12, tzinfo=UTC))


def _coverage(*, gap: bool = False) -> tuple[CoverageObservation, ...]:
    # Reset-baseline sweeps request their endpoints after the 05:00 UTC
    # boundary, so the chain head and tail are observed strictly after the
    # ranked-day boundary timestamps and are identified by their observation
    # ids rather than by boundary-time equality.
    return (
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
            has_row_gap=gap,
            observation_id=102,
        ),
        CoverageObservation(
            observed_at=DAY.end + timedelta(seconds=5),
            row_count=2,
            battle_identities=("daily",),
            has_row_gap=False,
            observation_id=103,
        ),
    )


def _input(**overrides) -> ReconciliationInput:
    values = {
        "ranked_day": DAY,
        "now": DAY.end + timedelta(minutes=1),
        "start_baseline_id": 10,
        "end_baseline_id": 11,
        "start_trophies": 6000,
        "next_start_trophies": 5940,
        "start_baseline_battle_log_observation_id": 101,
        "end_baseline_battle_log_observation_id": 103,
        "coverage_observations": _coverage(),
        "contributions": (
            BattleContribution("attack-1", "offense", 20),
            BattleContribution("defense-1", "defense", 10),
        ),
        "previous_day": PreviousRankedDay(
            complete=True,
            observed_defense_count=2,
            observed_defense_loss=20,
            shield_run_length=0,
        ),
        "boundary_kind": None,
        "season_anchor_valid": True,
    }
    values.update(overrides)
    return ReconciliationInput(**values)


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


def test_zero_defenses_does_not_apply_automatic_adjustment_and_has_uncertain_shield_rules() -> (
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
    assert first.shield_state == "inferred_shielded"
    assert first.shield_duration_days == 1
    assert third.shield_state == "uncertain_sequence"
    assert "shield_sequence_longer_than_two_days" in third.failure_reasons


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
        result = reconcile_ranked_day(
            _input(
                start_trophies=start,
                next_start_trophies=reading,
                contributions=tuple(
                    BattleContribution(f"{tag}-attack-{index}", "offense", gain)
                    for index, gain in enumerate(gains)
                ),
                previous_day=PreviousRankedDay(True, 8, previous_loss, 0),
            )
        )

        assert result.state == "Complete", tag
        assert result.confidence == "exact"
        assert result.automatic_defense_loss == start + sum(gains) - reading
        assert result.automatic_defense_evidence_state == "confirmed"
        assert result.final_trophies_before_reset == reading
        assert result.unexplained_residual == 0
        assert result.shield_state == "not_inferred"

    # One of the 86 quiet days kept its trophies, so it is still uncharged.
    quiet = reconcile_ranked_day(
        _input(
            start_trophies=4991,
            next_start_trophies=4991,
            contributions=(),
            previous_day=PreviousRankedDay(True, 7, 232, 0),
        )
    )
    # Any other drop is still a mismatch, and Day 1 has no previous day.
    other = reconcile_ranked_day(
        _input(
            start_trophies=4695, next_start_trophies=4392, contributions=(),
            previous_day=PreviousRankedDay(True, 8, 305, 0),
        )
    )
    day_1 = reconcile_ranked_day(
        _input(
            start_trophies=4695, next_start_trophies=4391, contributions=(),
            previous_day=PreviousRankedDay(True, 8, 305, 0), season_first_day=True,
        )
    )

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
        replace(day, later_next_start_reading=(later_at, 4391))
    )
    # A quiet day's later reading still shows its start: it stays uncharged.
    quiet = reconcile_ranked_day(
        replace(day, later_next_start_reading=(later_at, 4695))
    )

    assert (charged.state, charged.confidence) == ("Complete", "inferred")
    assert charged.automatic_defense_loss == 304
    assert charged.automatic_defense_evidence_state == "calculated"
    assert charged.final_trophies_before_reset == 4391
    assert charged.next_start_trophies == 4391
    assert charged.unsettled_automatic_loss == 304
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
        replace(both, later_next_start_reading=(later_at, 4753))
    )

    assert unread.state == "Inconsistent"
    assert (settled.state, settled.confidence) == ("Complete", "inferred")
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
                *_battles("#9R2LRYY8V-2", "offense", 8, 280),
                *_battles("#9R2LRYY8V-2", "defense", 8, 240),
            ),
            previous_day=PreviousRankedDay(
                True, 0, 0, 0, end_baseline_id=11, unsettled_automatic_loss=304, proven_end=4391,
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
        day, replace(day, later_next_start_reading=(later_at, 4700)),
        replace(day, previous_day=None),
        replace(day, previous_day=PreviousRankedDay(False, 8, 240, 0)),
        delayed, replace(delayed, later_next_start_reading=(later_at, 4940)),
    )
    for case in cases:
        result = reconcile_ranked_day(case)
        assert result.state == "Partial"
        assert "automatic_defense_basis_unavailable" in result.failure_reasons
        assert result.automatic_defense_loss is None
    assert reconcile_ranked_day(replace(day, end_baseline_evidence={})).state == (
        "Complete"
    )


def _battles(tag: str, lens: str, count: int, total: int) -> tuple[BattleContribution, ...]:
    amounts = [total // count] * (count - 1) + [total - total // count * (count - 1)]
    return tuple(
        BattleContribution(f"{tag}-{lens}-{index}", lens, amount)
        for index, amount in enumerate(amounts)
    )


def test_later_reading_settles_a_reset_reading_missing_the_days_credit() -> None:
    # 5 October 2026, Season Day 1, then 6 October. The Reset reading left
    # out attack gains the battles prove; a reading after it, before any new
    # day battle, is exactly the calculated next start.
    cases = (
        # tag, attacks gain, defenses, defense loss, reading, later reading,
        # next day's gain, loss and end reading
        ("#P20G0CUJY", 308, 8, 234, 4766, 5074, 267, 273, 5068),
        ("#8Q20CULJP", 281, 8, 299, 4701, 4982, 277, 216, 5043),
        # 7 defenses: the later reading also includes the automatic loss.
        ("#L9L82J90J", 274, 7, 204, 4841, 5041, 245, 276, 5010),
    )
    later_at = DAY.end + timedelta(minutes=10)
    for tag, gain, defenses, loss, reading, later, gain_2, loss_2, end_2 in cases:
        day_1 = _input(
            start_trophies=5000,
            next_start_trophies=reading,
            contributions=(
                *_battles(tag, "offense", 8, gain),
                *_battles(tag, "defense", defenses, loss),
            ),
            season_first_day=True,
        )
        unsettled = reconcile_ranked_day(day_1)
        wrong_later = reconcile_ranked_day(
            replace(day_1, later_next_start_reading=(later_at, later + 1))
        )
        first = reconcile_ranked_day(
            replace(day_1, later_next_start_reading=(later_at, later))
        )

        assert unsettled.state == wrong_later.state == "Inconsistent", tag
        assert (first.state, first.confidence) == ("Complete", "inferred")
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
                    *_battles(f"{tag}-2", "offense", 8, gain_2),
                    *_battles(f"{tag}-2", "defense", 8, loss_2),
                ),
                previous_day=PreviousRankedDay(
                    True, defenses, loss, 0, end_baseline_id=11,
                    reset_reading_correction=later - reading, proven_end=later,
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
            "previous_day": PreviousRankedDay(True, 2, 20, 0, end_baseline_id=10, proven_end=start),
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
    # A battle that landed 15 minutes before the reading was in it.
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
    assert too_early.state == "Inconsistent"

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
        previous_day=PreviousRankedDay(True, 8, 320, 0, end_baseline_id=10, proven_end=5000),
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
            reset_reading_correction=-40, proven_end=4960,
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
    assert unproven.state == "Inconsistent"
    assert unproven.unexplained_residual == 40
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
        replace(day, later_next_start_reading=(DAY.end + timedelta(minutes=20), 4968))
    )

    assert (guessed.state, guessed.next_start_trophies) == ("Complete", 5000)
    assert reads_later_reading(day, guessed)
    assert disproved.state == "Inconsistent"
    assert disproved.unexplained_residual == -32
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


def _season_day_1(attacks, defenses, previous_day, next_start_trophies, **overrides):
    day_1 = ranked_day_for(datetime(2026, 10, 5, 12, tzinfo=UTC))
    return reconcile_ranked_day(
        _input(
            ranked_day=day_1,
            now=day_1.end + timedelta(minutes=1),
            start_trophies=5000,
            next_start_trophies=next_start_trophies,
            contributions=(
                *(BattleContribution(f"a{i}", "offense", n) for i, n in enumerate(attacks)),
                *(BattleContribution(f"d{i}", "defense", n) for i, n in enumerate(defenses)),
            ),
            previous_day=previous_day,
            season_first_day=True,
            **overrides,
        )
    )


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


def test_ranked_day_battle_events_are_canonical_signed_and_ordered() -> None:
    events = serialize_ranked_day_battles(
        (
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
                stars=3,
                destruction_percentage=100,
                opponent_tag=" #8pp ",
                opponent_name="Attacked player",
            ),
            # A two-sided/repeated observation is one canonical event. The
            # second row would also sort after the first if it were retained.
            BattleContribution(
                "attack-1",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=2,
                destruction_percentage=67,
                opponent_tag="#8PP",
                opponent_name="Stale name",
            ),
            BattleContribution(
                "defense-1",
                "defense",
                30,
                battle_timestamp=datetime(2026, 8, 4, 13, tzinfo=UTC),
                stars=1,
                destruction_percentage=50,
                opponent_tag="#9PP",
                opponent_name=None,
            ),
        )
    )

    assert BATTLE_EVENT_SERIALIZATION_VERSION == "legend-ranked-day-battle-events-v1"
    assert [event["battle_id"] for event in events] == ["defense-1", "attack-1"]
    assert events == [
        {
            "battle_id": "defense-1",
            "battle_timestamp": "2026-08-04T13:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 1,
            "trophy_change": -30,
        },
        {
            "battle_id": "attack-1",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": "Attacked player"},
            "destruction_percentage": 100,
            "stars": 3,
            "trophy_change": 40,
        },
    ]


def test_ranked_day_battle_events_exclude_invalid_and_unselected_rows() -> None:
    valid = BattleContribution(
        "accepted",
        "offense",
        0,
        battle_timestamp=datetime(2026, 8, 4, 12, tzinfo=UTC),
        stars=0,
        destruction_percentage=0,
        opponent_tag="#8PP",
    )
    events = serialize_ranked_day_battles(
        (
            valid,
            BattleContribution(
                "bad-stars",
                "offense",
                40,
                battle_timestamp=datetime(2026, 8, 4, 11, tzinfo=UTC),
                stars=4,
                destruction_percentage=100,
                opponent_tag="#8PP",
            ),
            BattleContribution(
                "disagreement",
                "defense",
                20,
                battle_timestamp=datetime(2026, 8, 4, 10, tzinfo=UTC),
                stars=2,
                destruction_percentage=50,
                opponent_tag="#9PP",
                disagreement=True,
            ),
            {
                "battle_identity": "excluded",
                "lens": "offense",
                "included": False,
                "valid": True,
                "amount_used": 40,
                "battle_timestamp": "2026-08-04T09:00:00Z",
                "stars": 3,
                "destruction_percentage": 100,
                "opponent_tag": "#8PP",
            },
        )
    )

    # A battle the two sides report differently is counted, so it stays shown
    # and flagged for the website's "Result awaiting confirmation" note.
    assert events == [
        {
            "battle_id": "accepted",
            "battle_timestamp": "2026-08-04T12:00:00Z",
            "opponent": {"tag": "#8PP", "name": None},
            "destruction_percentage": 0,
            "stars": 0,
            "trophy_change": 0,
        },
        {
            "battle_id": "disagreement",
            "battle_timestamp": "2026-08-04T10:00:00Z",
            "opponent": {"tag": "#9PP", "name": None},
            "destruction_percentage": 50,
            "stars": 2,
            "trophy_change": -20,
            "disagreement": True,
        },
    ]
    # Publication rows carry each contribution's lens beside its event.
    lenses = {"accepted": "offense", "disagreement": "defense"}
    offense, defense = _screen_events(
        [{"lens": lenses[event["battle_id"]], **event} for event in events]
    )
    assert [event["perspective_disagreement"] for event in offense + defense] == [
        False,
        True,
    ]


def test_ranked_day_battle_events_can_be_empty() -> None:
    assert (
        serialize_ranked_day_battles(
            (
                {
                    "battle_identity": "missing-opponent",
                    "lens": "offense",
                    "included": True,
                    "valid": True,
                    "disagreement": False,
                    "amount_used": 40,
                    "battle_timestamp": "2026-08-04T12:00:00Z",
                    "stars": 3,
                    "destruction_percentage": 100,
                },
            )
        )
        == []
    )



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


def _attacks(*amounts: int) -> tuple[BattleContribution, ...]:
    return tuple(BattleContribution(f"a{i}", "offense", n) for i, n in enumerate(amounts))


def _defenses(*amounts: int) -> tuple[BattleContribution, ...]:
    return tuple(BattleContribution(f"d{i}", "defense", n) for i, n in enumerate(amounts))


def _october(day: int, **overrides) -> ReconciliationInput:
    ranked_day = ranked_day_for(datetime(2026, 10, day, 12, tzinfo=UTC))
    return _input(ranked_day=ranked_day, now=ranked_day.end + timedelta(minutes=5), **overrides)


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
                True, 2, 62, 0, unsettled_automatic_loss=198, end_baseline_id=82547, proven_end=4666
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
    fewer_attacks = _season_day_1((21,), (40, 26, 25, 30, 28, 21, 19), None, 4832)

    assert more_attacks.automatic_defense_loss == 62
    assert more_attacks.final_trophies_before_reset == 4960
    assert more_attacks.unsettled_automatic_loss == 62
    assert (more_attacks.state, more_attacks.confidence) == ("Complete", "inferred")
    assert equal.automatic_defense_loss == 0
    assert equal.automatic_defense_evidence_state == "confirmed"
    assert equal.final_trophies_before_reset == 4968
    assert (equal.state, equal.confidence) == ("Complete", "exact")
    assert fewer_attacks.automatic_defense_loss == 27
    assert fewer_attacks.final_trophies_before_reset == 4805
    assert fewer_attacks.unsettled_automatic_loss == 27
    assert (fewer_attacks.state, fewer_attacks.confidence) == ("Complete", "inferred")
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
