"""The Reset total: a player's trophies at exactly 05:00:00 UTC at a Reset,
before the automatic defense loss, the number the Daily board ranks by.

The day calculation works it out when it saves an ended day's result and
stores it there (``formula_components["reset_total"]``): its value, whether
it is proven, what it rests on, and the reading that proved it.

| The day is | Reset total | Proven |
| --- | --- | --- |
| The Season's last day, with the game's official final saved | the official final | yes |
| Complete | its calculated end, unless the reading side was proven first | when a reading proves it |
| Unproven (Partial, only ``end_reading_unverified``) | its calculated end, or the reading side once a reading proves that | when a reading proves it |
| Contradicted, or missing its start or end | the reading side | when a reading proves it |
| Without usable battles | the old reading | no |

The calculated end is the day's end before its automatic loss, less the
battles whose attack ended at or after the Reset. The old reading is the
newest profile reading taken at or before the Reset; the reading side is
that reading plus every battle stamped after it, less those whose attack
ended at or after the Reset. Usable battles are the day's continuous battle
logs, an old reading taken at least ``reading_rule.DAY_READINGS_FROM`` into
the day, a time on every battle, and no battle the two players' reports
disagree on.

Proven once: the readings from the Reset on are taken in time order, and
the total is proven by the first run of them that proves it, at the
standard a confirmed board entry needs (``reading_rule.first_proof``).
Readings after that run cannot take the proof back, nor prove the other
answer instead: when both are proven, the one proven first stands, the
calculated end on a tie, even once the day is Complete. A defense reported in
the last ``ATTACK_ENDS_WITHIN`` before the Reset whose attacker's report is
not saved may have ended after it, so a total counting it is not proven.
The old reading may or may not show a battle reported between its request
and its response, or a defense reported up to ``ATTACK_ENDS_WITHIN`` before
that request: the reading side is then proven only when the same run also
contradicts every other way of reading it.

A Season's last Reset keeps the board's older rule: without the official
final, a Complete day gives its EOD and any other its reading side, neither
proven.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import combinations
from typing import Any

from . import reading_rule
from .reading_rule import Reading

# A defense is reported near the attack's start; the attack can end up to
# this long after, when the attacker's own report is stamped.
ATTACK_ENDS_WITHIN = timedelta(minutes=4)

OFFICIAL_FINAL = "official_final"
CALCULATED_END = "calculated_end"
# The old reading plus the battles stamped after it.
READING_SIDE = "reading_side"
OLD_READING = "old_reading"
NO_ANSWER = "none"


@dataclass(frozen=True, slots=True)
class Battle:
    """One battle the day counts: its trophy change for the player, its own
    report time, whether it was a defense, whether the two players' reports
    disagree on it, and, for a defense, the attacker's report time, when
    the attack ended, if saved."""

    change: int
    stamped_at: datetime | None
    defense: bool
    disputed: bool = False
    attack_ended_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResetTotal:
    value: int | None
    proven: bool
    basis: str
    # The reading that proved it, and the old reading it rests on.
    proven_at: datetime | None = None
    reading_at: datetime | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "value": self.value, "proven": self.proven, "basis": self.basis,
            "proven_at": None if self.proven_at is None else self.proven_at.astimezone(UTC).isoformat(),
            "reading_at": None if self.reading_at is None else self.reading_at.astimezone(UTC).isoformat(),
        }


def reset_total(
    *,
    reset_at: datetime,
    day_start: datetime,
    state: str,
    failure_reasons: Iterable[str],
    end_before_loss: int | None,
    eod: int | None,
    season_end: bool,
    official_final: int | None,
    battles: Iterable[Battle],
    battles_usable: bool,
    old_reading: Reading | None,
    end_proof: Mapping[str, Any] | None,
    reading_proof: Mapping[str, Any] | None,
) -> ResetTotal:
    """The day's Reset total (module docstring). ``end_proof`` is
    ``reading_rule.first_proof``'s rule for the calculated end, ``None``
    when the day's own battles, start and readings during it leave no end a
    reading can prove; ``reading_proof`` is its rule for the reading side,
    without the end, ``None`` when the automatic loss is not known."""
    if season_end and official_final is not None:
        return ResetTotal(official_final, True, OFFICIAL_FINAL)
    battles = tuple(battles)
    after_reset = sum(battle.change for battle in battles if _ended_after(battle, reset_at))
    end_unknown = any(_may_end_after(battle, reset_at) for battle in battles)
    complete = state == "Complete" and end_before_loss is not None
    side = _reading_side(old_reading, battles, day_start, reset_at, battles_usable)
    reading_at = None if old_reading is None else old_reading.read_at
    if season_end:
        if complete:
            return ResetTotal(eod, False, CALCULATED_END)
        if side is None or side[2]:
            return _old_reading(old_reading)
        return ResetTotal(side[1], False, READING_SIDE, reading_at=reading_at)
    calculated = None if end_before_loss is None else end_before_loss - after_reset
    proven = None
    if calculated is not None and end_proof is not None and not end_unknown:
        proving = reading_rule.first_proof(end_before_loss=end_before_loss, **end_proof)
        if proving is not None:
            proven = ResetTotal(calculated, True, CALCULATED_END, proving.read_at)
    # The reading side stands only when proven first: by readings before the
    # one that proved the day's own end, whatever the day's state now.
    if side is not None and reading_proof is not None and not end_unknown and (
            len(side[2]) <= reading_rule.MAX_AMBIGUOUS_BATTLES):
        end, value, unshown = side
        proving = reading_rule.first_proof(
            end_before_loss=end, **{**reading_proof, "readings": [
                reading for reading in reading_proof["readings"]
                if proven is None or proven.proven_at is None or reading.read_at < proven.proven_at]},
            others=[end + sum(changes) for count in range(1, len(unshown) + 1)
                    for changes in combinations(unshown, count)])
        if proving is not None:
            return ResetTotal(value, True, READING_SIDE, proving.read_at, reading_at)
    if proven is not None:
        return proven
    if complete:
        return ResetTotal(calculated, False, CALCULATED_END)
    if calculated is not None and tuple(failure_reasons) == ("end_reading_unverified",):
        return ResetTotal(calculated, False, CALCULATED_END)
    if side is None:
        return _old_reading(old_reading)
    return ResetTotal(side[1], False, READING_SIDE, reading_at=reading_at)


def _old_reading(old_reading: Reading | None) -> ResetTotal:
    if old_reading is None:
        return ResetTotal(None, False, NO_ANSWER)
    return ResetTotal(old_reading.trophies, False, OLD_READING, reading_at=old_reading.read_at)


def _reading_side(
    old_reading: Reading | None, battles: tuple[Battle, ...], day_start: datetime,
    reset_at: datetime, battles_usable: bool,
) -> tuple[int, int, list[int]] | None:
    """The old reading plus the battles stamped after it, at the day's end
    and at the Reset, and the changes of the battles it may or may not
    show; ``None`` without usable battles."""
    if old_reading is None or not battles_usable or (
            old_reading.read_at < day_start + reading_rule.DAY_READINGS_FROM):
        return None
    read_at = old_reading.read_at
    requested_at = old_reading.requested_at or read_at
    after = [battle for battle in battles
             if battle.stamped_at is not None and battle.stamped_at > read_at]
    unshown = [
        battle.change for battle in battles
        if battle.stamped_at is not None
        and requested_at - (ATTACK_ENDS_WITHIN if battle.defense else timedelta(0))
        <= battle.stamped_at <= read_at
    ]
    end = old_reading.trophies + sum(battle.change for battle in after)
    return end, end - sum(battle.change for battle in after if _ended_after(battle, reset_at)), unshown


def _ended_after(battle: Battle, reset_at: datetime) -> bool:
    """Whether the battle's attack ended at or after the Reset: an attack's
    own report is its end, and a defense's is its attacker's."""
    ended = battle.attack_ended_at if battle.defense else battle.stamped_at
    return ended is not None and ended >= reset_at


def _may_end_after(battle: Battle, reset_at: datetime) -> bool:
    """A defense reported in the last ``ATTACK_ENDS_WITHIN`` before the
    Reset without its attacker's report: its attack may have ended after."""
    return (battle.defense and battle.attack_ended_at is None
            and battle.stamped_at is not None
            and reset_at - ATTACK_ENDS_WITHIN <= battle.stamped_at < reset_at)
