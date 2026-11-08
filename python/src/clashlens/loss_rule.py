"""The game's automatic defense loss: trophies it takes at the Reset for
the defense slots a Legend day left unused, and how it settles across the
Reset (``LossEvidence``, ``StartCharge``, ``Landing``).

The settlement, day by day: the Reset reading is taken as it is
(provisional). The day's own readings then show the loss landed before it
(the day ends after the loss), landed after it (an unsettled loss: the next
day starts from the reading less it), or cannot tell. When they cannot, the
next day is tried from the other start (``other_start``), and, once the next
day ends proven from it, says how the loss went (``next_day_landing``): the
day before is then calculated again. Each calculation starts from the saved
results, so a failed or retried one changes nothing, and a later reading
can move a day either way.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from .domain import MAX_DAILY_DEFENSES

if TYPE_CHECKING:
    from .reconciliation import PreviousRankedDay, ReconciliationInput


class LossEvidence(StrEnum):
    """What a day's automatic defense loss is known as, saved as each
    version's ``automatic_defense_evidence_state``.

    NOT_APPLICABLE: no loss to charge (no defense slot used, all 8 used, or
    a first Legend day with fewer attacks than defenses), unless a reading
    shows one. UNKNOWN: its basis is missing (a gap in the logs, or a day
    before whose battles are not all known). CALCULATED: calculated from the
    battles, or taken from a reading showing it. CONFIRMED: a calculated
    loss the readings isolate exactly, with no reset hiding the end and the
    Reset reading taken after it landed.

    ``adjustment`` gives the first three. ``_reconcile`` then moves
    NOT_APPLICABLE to CALCULATED when a reading shows a loss only a reading
    can (``possible_losses``), and CALCULATED to CONFIRMED; UNKNOWN stays.
    Nothing else sets it.
    """

    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"
    CALCULATED = "calculated"
    CONFIRMED = "confirmed"


class StartCharge(StrEnum):
    """Which start a day is calculated from, as to the day before's
    automatic loss. AS_RESULT_SAYS: the day before's saved result says
    (its unsettled loss comes off the Reset reading, if any). CHARGED: the
    whole loss comes off. NOT_CHARGED: none does. A day is tried as its
    result says first, then the other way (``other_start``)."""

    AS_RESULT_SAYS = "as_result_says"
    CHARGED = "charged"
    NOT_CHARGED = "not_charged"


class Landing(StrEnum):
    """How the next day's proven start says this day's automatic loss went,
    when its own readings could not (``next_day_landing``). NEVER: taken to
    land after the Reset reading, it never landed. LATE: taken as landed in
    the Reset reading, it landed after it. ``None`` stands for no
    correction: the next day started as this day's result says."""

    LATE = "late"
    NEVER = "never"


def adjustment(
    data: ReconciliationInput,
    *,
    coverage_complete: bool,
    attack_count: int,
    defense_count: int,
    observed_defense_loss: int,
    failures: list[str],
) -> tuple[int | None, LossEvidence]:
    """The day's automatic defense loss and its evidence state."""
    # The game fills missing defense slots, counting its "no opponent, no
    # battle" rows as used slots with no loss. Of 3,643 Day 1 results checked
    # against a profile read after the loss on 6 October 2026, 3,623 matched
    # counting them and 3,581 without.
    attack_count += data.zero_result_attack_slots
    defense_count += data.zero_result_defense_slots
    if defense_count == 0 and "dropped_from_legend_i" in data.end_baseline_evidence:
        failures.append("automatic_defense_basis_unavailable")
        return None, LossEvidence.UNKNOWN
    if defense_count == 0 or defense_count >= MAX_DAILY_DEFENSES:
        return None, LossEvidence.NOT_APPLICABLE
    previous = data.previous_day
    first = first_day(data)
    if not coverage_complete or not first and (
        previous is None
        or not battles_known(previous)
        or previous.observed_defense_count < 0
    ):
        failures.append("automatic_defense_basis_unavailable")
        return None, LossEvidence.UNKNOWN
    if first and attack_count < defense_count:
        # Only a reading shows it (``possible_losses``).
        return None, LossEvidence.NOT_APPLICABLE
    return automatic_defense_loss(
        attacks=attack_count,
        defenses=defense_count,
        defense_loss=observed_defense_loss,
        previous_defenses=0 if first else (
            previous.observed_defense_count + previous.zero_result_defense_slots
        ),
        previous_defense_loss=0 if first else previous.observed_defense_loss,
        first=first,
    ), LossEvidence.CALCULATED


def first_day(data: ReconciliationInput) -> bool:
    """A player's first Legend day of the Season: Day 1, a promoted
    player's first Legend I day, or a day starting at 5,000 under the
    Season rule, the first a player who sat there battles. From 6 to 13
    October 2026 the game charged such days for (attacks - defenses)
    missing defenses: 1,247 of 1,250 with equal counts below 8 lost
    nothing, and 131 of 140 with more attacks lost that."""
    return data.season_first_day or "promoted_into_legend_i" in data.start_baseline_evidence or (
        data.start_baseline_evidence.get("start_trophies_source") == "season_rule")


def possible_losses(
    data: ReconciliationInput, attack_count: int, defense_count: int, defense_loss: int,
) -> tuple[int, ...]:
    """The automatic losses the game may charge instead of nothing, which
    only the next Reset reading, or a later one, shows. A day with no defense
    slot used: all 8 at the previous day's average, none without a previous
    day whose battles are known. A first Legend day with fewer attacks than
    defenses, at its own average: on Day 1, (8 - defenses), as 24 of 34 such
    days from 5 to 13 October 2026 lost and 10 lost nothing; on any other,
    fewer missing defenses than that, as 22 of 28 lost nothing and 6, all
    with no attack, lost their average once or twice; none lost
    (8 - defenses)."""
    previous = data.previous_day
    attack_count += data.zero_result_attack_slots
    defense_count += data.zero_result_defense_slots
    if attack_count < defense_count < MAX_DAILY_DEFENSES and first_day(data):
        average = defense_loss // defense_count
        missing = MAX_DAILY_DEFENSES - defense_count
        return () if not average else (average * missing,) if data.season_first_day else tuple(
            average * count for count in range(1, missing))
    if (
        defense_count
        or data.season_first_day
        or previous is None
        or not battles_known(previous)
    ):
        return ()
    previous_defenses = (
        previous.observed_defense_count + previous.zero_result_defense_slots
    )
    if previous_defenses <= 0:
        return ()
    loss = automatic_defense_loss(
        attacks=0,
        defenses=0,
        defense_loss=0,
        previous_defenses=previous_defenses,
        previous_defense_loss=previous.observed_defense_loss,
        first=False,
    )
    return (loss,) if loss else ()


def loss_candidates(
    automatic_loss: int | None, automatic_state: LossEvidence, possible: tuple[int, ...],
) -> tuple[int, ...]:
    """The automatic losses a reading may show applied: the calculated loss,
    or those only a reading can show (``possible_losses``)."""
    if automatic_state == LossEvidence.CALCULATED and automatic_loss:
        return (automatic_loss,)
    return possible


def reading_start(data: ReconciliationInput) -> bool:
    """Whether the day starts from the Reset reading that ended the day
    before, a day whose battles are all known (``battles_known``)."""
    previous = data.previous_day
    return not (
        data.start_trophies is None or previous is None or not battles_known(previous)
        or data.start_baseline_evidence.get("start_trophies_source") == "previous_day_end"
        or previous.end_baseline_id is None or previous.end_baseline_id != data.start_baseline_id
    )


def other_start(data: ReconciliationInput) -> StartCharge | None:
    """The other way the day before's automatic loss can have gone at this
    day's start: not charged when its result took it to land after the
    Reset reading, charged when a result its readings did not settle took
    the reading as is. On a day that starts at 5,000, never: None."""
    previous = data.previous_day
    if previous is None or not previous.automatic_loss or not reading_start(data) or (
            data.season_first_day or data.start_baseline_evidence.get("start_trophies_source") == "season_rule"):
        return None
    if previous.unsettled_automatic_loss:
        return StartCharge.NOT_CHARGED
    return StartCharge.CHARGED if not (previous.complete or previous.reset_reading_correction) else None


def next_day_landing(
    data: ReconciliationInput, automatic_loss: int | None, unsettled_loss: int,
    next_start: int | None,
) -> Landing | None:
    """How the next day's start, proven from the other start
    (``other_start``), says this day's automatic loss went, when its own
    readings could not: NEVER landed, when they took it to land after the
    Reset reading; LATE, after a Reset reading they took as is. Only the
    next day's end can show a loss never landed: until then every reading
    fits it as still to land. None when the next day started as this day's
    result says."""
    if not automatic_loss or data.next_day_start is None or next_start is None:
        return None
    if unsettled_loss and data.next_day_start == next_start + automatic_loss:
        return Landing.NEVER
    if not unsettled_loss and data.next_day_start == next_start - automatic_loss:
        return Landing.LATE
    return None


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
    if first:
        # On a first Legend day (``first_day``) the game charges a player
        # with at least as many attacks as defenses for (attacks - defenses)
        # missing defenses, so equal counts lose nothing; one with fewer
        # attacks only as a reading shows (``possible_losses``). Profiles
        # read on 6 October 2026 after the loss and before any Day 2 battle:
        # 66 of 69 players with more attacks than defenses lost that, 3 lost
        # (8 - defenses); of 120 with equal counts, 59 kept the Reset
        # reading past 05:14 and 6 lost (8 - defenses). Recounted with "no
        # opponent, no battle" rows on 1,615 Day 1 results, this still
        # matched 1,597, against 1,442 for (8 - defenses) and 1,566 for
        # (attacks - defenses) alone.
        missing_defenses = attacks - defenses
    return average_loss * missing_defenses
