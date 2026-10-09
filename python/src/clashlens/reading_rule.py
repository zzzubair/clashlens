"""One rule for every profile reading after a Legend day ends.

At the moment a profile was read, its trophy count must equal what the
ledger says the player had then: the day's start plus every battle the game
had shown by then, less the automatic defense loss once the game applied it,
plus any battle of the new day the profile already showed. A reading that
equals it confirms the day; a reading that cannot contradicts it. A reading
taken while a battle may or may not have landed yet is read both ways and
can only confirm. The last trustworthy reading before the player's first
new-day battle that is not read both ways decides, even against an earlier
one that showed a loss; a later reading can only confirm, and only when none
of those decided, never undoing a loss another later one showed.

This one rule replaces four: a Reset reading taken before the automatic
loss landed, a later reading settling a Reset reading taken too early, a
Reset reading that missed the day's last battles, and a Reset reading taken
after the player's first battle of the new day.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import combinations

# The game shows a battle in the profile after its report: an attacker's own
# attack about 4 minutes after the report, a defense about 2 minutes after it
# starts (measured 7 October 2026), and either can take longer. A reading in
# this span after a battle's earliest landing may or may not show it.
BATTLE_LANDING_LAG = timedelta(minutes=10)
# The automatic defense loss landed no earlier than 7 minutes 38 seconds
# after the Reset in 733 sampled days (6 and 8 October 2026) and as late as
# 05:29 in lab data. Neither bound is official, so a reading may show the
# loss at any time after the Reset: its value says whether it did.
# A reading with more battles than this possibly in flight cannot be judged.
MAX_AMBIGUOUS_BATTLES = 6


@dataclass(frozen=True, slots=True)
class Effect:
    """One battle's trophy change for the player, and the earliest moment
    the profile can show it."""

    identity: str
    change: int
    lands_from: datetime
    disputed: bool = False

    @property
    def lands_until(self) -> datetime:
        return self.lands_from + BATTLE_LANDING_LAG


@dataclass(frozen=True, slots=True)
class Reading:
    read_at: datetime
    trophies: int
    # A Legend I profile naming Season 0, which the game also sends to
    # signed-up players: its trophies can confirm the ledger, never
    # contradict it.
    confirm_only: bool = False
    # The Reset pair's own profile.
    reset_reading: bool = False


@dataclass(frozen=True, slots=True)
class Verdict:
    # "verified", "contradicted" or "unverified".
    outcome: str
    reading: Reading | None = None
    # The automatic loss the reading showed already applied: 0 when it had
    # not landed yet, or none applies.
    loss: int = 0
    # The reading proves the whole day: no battle in flight had to be read
    # either way, and any certain loss had landed.
    exact: bool = False
    # Ended-day battles the reading had not shown yet.
    missed: tuple[str, ...] = ()
    # New-day trophy change the reading already showed.
    new_day_change: int = 0
    # A contradiction: the reading less the nearest value it could have had.
    residual: int | None = None
    earlier_contradictions: int = 0


@dataclass(frozen=True, slots=True)
class _Judged:
    reading: Reading
    matched: bool
    ambiguous: bool
    loss: int = 0
    missed: tuple[str, ...] = ()
    new_day_change: int = 0
    residual: int = 0


def judge(
    reading: Reading,
    *,
    end_before_loss: int,
    loss_candidates: tuple[int, ...],
    loss_certain: bool,
    day_effects: Iterable[Effect],
    new_day_effects: Iterable[Effect],
) -> _Judged | None:
    """Whether one reading equals a value the ledger allows at its time, or
    ``None`` when it cannot be judged: a disputed battle may be in it, or too
    many battles may be."""
    at = reading.read_at
    base = end_before_loss
    missed: list[str] = []
    # A battle the reading may or may not show: the change if it does not
    # (ended day) or does (new day), its identity, and which day.
    maybes: list[tuple[int, str, bool]] = []
    new_day_change = 0
    for effect in day_effects:
        if effect.lands_until <= at:
            continue
        if effect.disputed:
            return None
        if effect.lands_from > at:
            base -= effect.change
            missed.append(effect.identity)
        else:
            maybes.append((-effect.change, effect.identity, True))
    for effect in new_day_effects:
        if effect.lands_from > at:
            continue
        if effect.disputed:
            return None
        if effect.lands_until <= at:
            base += effect.change
            new_day_change += effect.change
        else:
            maybes.append((effect.change, effect.identity, False))
    if len(maybes) > MAX_AMBIGUOUS_BATTLES:
        return None
    losses = [0, *(loss for loss in loss_candidates if loss)]
    # Any battle in flight leaves the reading read one way or the other, even
    # when it fits as read: the battle's credit may have been visible or not.
    ambiguous = bool(maybes)
    for loss in losses:
        if reading.trophies == base - loss:
            return _Judged(reading, True, ambiguous, loss, tuple(missed), new_day_change)
    for count in range(1, len(maybes) + 1):
        for chosen in combinations(maybes, count):
            value = base + sum(delta for delta, _, _ in chosen)
            for loss in losses:
                if reading.trophies == value - loss:
                    return _Judged(
                        reading, True, True, loss,
                        tuple(missed) + tuple(
                            identity for _, identity, ended in chosen if ended
                        ),
                        new_day_change + sum(
                            delta for delta, _, ended in chosen if not ended
                        ),
                    )
    # The residual is against the day's end after a certain loss, the value
    # every saved result records as the expected next start.
    expected = base - (max(loss_candidates) if loss_certain and loss_candidates else 0)
    return _Judged(reading, False, ambiguous, residual=reading.trophies - expected)


def decide(
    readings: Iterable[Reading],
    *,
    reset_at: datetime,
    end_before_loss: int,
    loss_candidates: tuple[int, ...],
    loss_certain: bool,
    day_effects: tuple[Effect, ...],
    new_day_effects: tuple[Effect, ...],
    new_day_from: datetime | None = None,
    start_proven: bool,
) -> Verdict:
    """The day's verdict from every reading taken from its end Reset on.

    A clean reading, one with every battle landed and none in flight,
    either equals the ledger or contradicts it. Trustworthy readings taken
    before the player's first new-day battle, the earliest report of one by
    either player (``new_day_from``) or of any in ``new_day_effects``,
    decide, and the last clean one among them decides: a match proves the
    day, with the loss landed when one is certain, or, read before the loss,
    confirms the battles and leaves the loss unsettled; it outranks every
    contradiction or earlier loss match before it: an earlier reading can
    match a loss by missing credits that had not landed. Only when none of
    them decided, a later or confirm-only reading can confirm the day, never
    contradict it, a new-day battle it shows may not be known yet, nor undo a
    loss an earlier such reading showed. A reading
    the ledger reads both ways settles nothing on its own, except the Reset
    reading, which contradicts when no way fits. A reading that fits only
    with a battle not yet shown, or read one way, is a guess: taken when the
    day's start is proven and no clean reading decided.
    """
    judged = [
        judged for judged in (
            judge(
                reading,
                end_before_loss=end_before_loss,
                loss_candidates=loss_candidates,
                loss_certain=loss_certain,
                day_effects=day_effects,
                new_day_effects=new_day_effects,
            )
            for reading in sorted(readings, key=lambda item: (item.read_at, item.trophies))
        )
        if judged is not None
    ]
    def exactness(item: _Judged) -> bool:
        # A certain loss must have landed; a possible one (a day with no
        # defense slot used) is uncharged until a reading shows otherwise.
        return not (item.missed or item.ambiguous) and bool(item.loss or not loss_certain)

    cutoff = min(
        (at for at in (new_day_from, *(effect.lands_from for effect in new_day_effects))
         if at is not None),
        default=None,
    )

    def deciding(item: _Judged) -> bool:
        return not item.reading.confirm_only and (cutoff is None or item.reading.read_at < cutoff)

    def contradicts(item: _Judged) -> bool:
        return (
            deciding(item)
            and not item.matched
            and (not item.ambiguous or item.reading.reset_reading)
        )

    decider: _Judged | None = None
    loss_landed = False
    for item in judged:
        clean = item.matched and not item.ambiguous and not item.missed
        if deciding(item) and (clean or contradicts(item)):
            decider = item
        elif clean and not (loss_landed and not item.loss) and not (
            decider is not None and deciding(decider)
        ):
            # A later or confirm-only match confirms only when none decided,
            # and never undoes a loss shown landed.
            decider = item
        if decider is item and item.matched:
            loss_landed = bool(item.loss)
    if decider is not None and decider.matched:
        return Verdict(
            "verified", decider.reading, decider.loss, exactness(decider),
            decider.missed, decider.new_day_change, None,
            sum(
                1 for item in judged
                if contradicts(item) and item.reading.read_at < decider.reading.read_at
            ),
        )
    if decider is not None:
        return Verdict("contradicted", decider.reading, residual=decider.residual)
    guessed = next((item for item in judged if item.matched), None)
    if guessed is not None and start_proven:
        return Verdict(
            "verified", guessed.reading, guessed.loss, False,
            guessed.missed, guessed.new_day_change,
        )
    return Verdict("unverified")
