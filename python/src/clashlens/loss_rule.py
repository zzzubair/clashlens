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
    first = first_day(data)
    if not coverage_complete or not first and (
        previous is None
        or not battles_known(previous)
        or previous.observed_defense_count < 0
    ):
        failures.append("automatic_defense_basis_unavailable")
        return None, "unknown"
    if first and not data.season_first_day and attack_count < defense_count:
        # Only a reading shows it (``possible_loss``).
        return None, "not_applicable"
    return automatic_defense_loss(
        attacks=attack_count,
        defenses=defense_count,
        defense_loss=observed_defense_loss,
        previous_defenses=0 if first else (
            previous.observed_defense_count + previous.zero_result_defense_slots
        ),
        previous_defense_loss=0 if first else previous.observed_defense_loss,
        first=first,
    ), "calculated"


def first_day(data: ReconciliationInput) -> bool:
    """A player's first Legend day of the Season: Day 1, a promoted
    player's first Legend I day, or a day starting at 5,000 under the
    Season rule, the first a player who sat there battles. From 6 to 13
    October 2026 the game charged such days for (attacks - defenses)
    missing defenses: 1,247 of 1,250 with equal counts below 8 lost
    nothing, and 131 of 140 with more attacks lost that."""
    return data.season_first_day or "promoted_into_legend_i" in data.start_baseline_evidence or (
        data.start_baseline_evidence.get("start_trophies_source") == "season_rule")


def possible_loss(
    data: ReconciliationInput, attack_count: int, defense_count: int, defense_loss: int,
) -> int:
    """The automatic loss the game may or may not charge, which only the
    next Reset reading, or a later one, shows: on a day with no defense slot
    used, for all 8 at the previous day's average, 0 without a previous day
    whose battles are known; on a first Legend day after Day 1 with fewer
    attacks than defenses, for (8 - defenses) at its own average. From 5 to
    13 October 2026, 22 of 28 such first days were charged nothing."""
    previous = data.previous_day
    attack_count += data.zero_result_attack_slots
    defense_count += data.zero_result_defense_slots
    if defense_count and attack_count < defense_count < MAX_DAILY_DEFENSES and not (
            data.season_first_day) and first_day(data):
        return automatic_defense_loss(
            attacks=attack_count, defenses=defense_count, defense_loss=defense_loss,
            previous_defenses=0, previous_defense_loss=0, first=False,
        )
    if (
        defense_count
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
        first=False,
    )


def loss_candidates(
    automatic_loss: int | None, automatic_state: str, possible: int,
) -> tuple[int, ...]:
    """The automatic losses a reading may show applied: the calculated loss,
    or the ``possible_loss`` only a reading can show."""
    if automatic_state == "calculated" and automatic_loss:
        return (automatic_loss,)
    return (possible,) if possible else ()


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
    first: bool,
) -> int:
    """The game's automatic defense loss for a day with 0 to 7 used defense
    slots. Attacks and defenses are used slots, "no opponent, no battle" rows
    included; losses are observed battle losses, without the previous day's
    automatic loss."""
    if first:
        # The game averages a first Legend day's own defenses only: 115 of
        # 119 such 12 October 2026 days lost exactly that.
        previous_defenses = previous_defense_loss = 0
    average_loss = (previous_defense_loss + defense_loss) // (
        previous_defenses + defenses
    )
    missing_defenses = MAX_DAILY_DEFENSES - defenses
    if first and attacks >= defenses:
        # On a first Legend day (``first_day``) the game charges a player
        # with at least as many attacks as defenses for (attacks - defenses)
        # missing defenses, so equal counts lose nothing. Profiles read on
        # 6 October 2026 after the loss and before any Day 2 battle: 66 of
        # 69 players with more attacks than defenses lost that, 3 lost
        # (8 - defenses); of 120 with equal counts, 59 kept the Reset
        # reading past 05:14 and 6 lost (8 - defenses). Fewer attacks than
        # defenses lost (8 - defenses) on Day 1 in 37 of 38. Recounted with
        # "no opponent, no battle" rows on 1,615 Day 1 results, this still
        # matched 1,597, against 1,442 for (8 - defenses) and 1,566 for
        # (attacks - defenses) alone.
        missing_defenses = attacks - defenses
    return average_loss * missing_defenses
