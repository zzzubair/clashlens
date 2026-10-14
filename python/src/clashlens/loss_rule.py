"""The game's automatic defense loss: trophies it takes at the Reset for
the defense slots a Legend day left unused."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .domain import MAX_DAILY_DEFENSES

if TYPE_CHECKING:
    from .reconciliation import PreviousRankedDay, ReconciliationInput


def adjustment(
    data: ReconciliationInput,
    *,
    coverage_complete: bool,
    attack_count: int,
    defense_count: int,
    observed_defense_loss: int,
    failures: list[str],
) -> tuple[int | None, str]:
    """The day's automatic defense loss and its evidence state."""
    # The game fills missing defense slots, counting its "no opponent, no
    # battle" rows as used slots with no loss. Of 3,643 Day 1 results checked
    # against a profile read after the loss on 6 October 2026, 3,623 matched
    # counting them and 3,581 without.
    attack_count += data.zero_result_attack_slots
    defense_count += data.zero_result_defense_slots
    if defense_count == 0 and "dropped_from_legend_i" in data.end_baseline_evidence:
        failures.append("automatic_defense_basis_unavailable")
        return None, "unknown"
    if defense_count == 0 or defense_count >= MAX_DAILY_DEFENSES:
        return None, "not_applicable"
    previous = data.previous_day
    # Day 1, and a promoted player's first Legend I day, average their own
    # defenses only: 115 of 119 such 12 October 2026 days lost exactly that.
    first_day = data.season_first_day or "promoted_into_legend_i" in data.start_baseline_evidence
    if not coverage_complete or not first_day and (
        previous is None
        or not battles_known(previous)
        or previous.observed_defense_count < 0
    ):
        failures.append("automatic_defense_basis_unavailable")
        return None, "unknown"
    return automatic_defense_loss(
        attacks=attack_count,
        defenses=defense_count,
        defense_loss=observed_defense_loss,
        previous_defenses=0 if first_day else (
            previous.observed_defense_count + previous.zero_result_defense_slots
        ),
        previous_defense_loss=0 if first_day else previous.observed_defense_loss,
        season_first_day=first_day,
    ), "calculated"


def zero_defense_loss(data: ReconciliationInput, defense_count: int) -> int:
    """The automatic loss for all 8 defense slots of a day with none used,
    averaged over the previous day alone, or 0 without a complete previous
    day to average. Charged only when the next Reset reading, or a later
    one before any new-day battle, shows it."""
    previous = data.previous_day
    if (
        defense_count + data.zero_result_defense_slots
        or data.season_first_day
        or previous is None
        or not battles_known(previous)
    ):
        return 0
    previous_defenses = (
        previous.observed_defense_count + previous.zero_result_defense_slots
    )
    if previous_defenses <= 0:
        return 0
    return automatic_defense_loss(
        attacks=0,
        defenses=0,
        defense_loss=0,
        previous_defenses=previous_defenses,
        previous_defense_loss=previous.observed_defense_loss,
        season_first_day=False,
    )


def loss_candidates(
    data: ReconciliationInput,
    automatic_loss: int | None,
    automatic_state: str,
    defense_count: int,
) -> tuple[int, ...]:
    """The automatic losses a reading may show applied: the calculated loss,
    or, for a day with no used defense slot, the charge for all 8 that only
    a reading can show."""
    if automatic_state == "calculated" and automatic_loss:
        return (automatic_loss,)
    zero = zero_defense_loss(data, defense_count)
    return (zero,) if zero else ()


def battles_known(previous: PreviousRankedDay) -> bool:
    """Whether the day before's battles are all known, so its defenses can
    average the automatic loss: a missing reading does not change that."""
    if previous.battles_known is None:
        return previous.complete and previous.coverage_complete
    return previous.battles_known


def automatic_defense_loss(
    *,
    attacks: int,
    defenses: int,
    defense_loss: int,
    previous_defenses: int,
    previous_defense_loss: int,
    season_first_day: bool,
) -> int:
    """The game's automatic defense loss for a day with 0 to 7 used defense
    slots. Attacks and defenses are used slots, "no opponent, no battle" rows
    included; losses are observed battle losses, without the previous day's
    automatic loss."""
    if season_first_day:
        # The game averages Day 1's own defenses only: the previous Season's
        # last day takes no part.
        previous_defenses = previous_defense_loss = 0
    average_loss = (previous_defense_loss + defense_loss) // (
        previous_defenses + defenses
    )
    missing_defenses = MAX_DAILY_DEFENSES - defenses
    if season_first_day and attacks >= defenses:
        # On a Season's Day 1 the game charges a player with at least as
        # many attacks as defenses for (attacks - defenses) missing defenses,
        # so equal counts lose nothing. Profiles read on 6 October 2026
        # after the loss and before any Day 2 battle: 66 of 69 players with
        # more attacks than defenses lost that, 3 lost (8 - defenses); of
        # 120 with equal counts, 59 kept the Reset reading past 05:14 and 6
        # lost (8 - defenses). Fewer attacks than defenses lost
        # (8 - defenses) in 37 of 38. Recounted with "no opponent, no battle"
        # rows on 1,615 Day 1 results, this still matched 1,597, against
        # 1,442 for (8 - defenses) and 1,566 for (attacks - defenses) alone.
        missing_defenses = attacks - defenses
    return average_loss * missing_defenses
