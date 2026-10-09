"""One rule for every profile reading of a Legend day.

At the moment a profile was read, its trophy count must equal what the
ledger says the player had then. During the day that is the day's start
plus every battle the game had shown by then, plus the day before's
automatic defense loss while it may not have landed. From the day's end
Reset it is the day's end plus any battle of the new day the profile
already showed, less the automatic loss once the game applied it. A
reading taken while a battle may or may not have landed yet is read both
ways. One short of exactly the gains of the player's latest landed
attacks shows the game crediting the attacker's profile late, which it can
do until the player stops attacking: it neither confirms nor contradicts. A
trustworthy reading that fits no value makes the day Uncertain, and no
later match erases that; nor can a later reading take back a loss an
earlier one showed landed. Otherwise the last clean match decides. A
reading can only confirm when it is not trustworthy: a Legend I profile
naming Season 0, one read after the player's continuous battle logs, or
one read after a battle only the opponent has reported so far.

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
# A day's readings are checked from this long after its start Reset: the
# day before's last attack is reported by 05:05 and lands within
# BATTLE_LANDING_LAG.
DAY_READINGS_FROM = timedelta(minutes=15)


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
    # The reading proves the day's end before the automatic loss: it showed
    # every battle landed and none was in flight, whether or not the loss
    # had landed yet.
    clean: bool = False
    # Attacks some reading lacked the gains of, the game's attacker-profile lag.
    lagged: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Judged:
    reading: Reading
    matched: bool
    ambiguous: bool
    loss: int = 0
    missed: tuple[str, ...] = ()
    new_day_change: int = 0
    residual: int = 0
    # Landed attacks whose gains the reading lacks: the attacker's profile lag.
    lagged: tuple[str, ...] = ()
    # Every automatic loss some reading of it that fits shows applied.
    losses: frozenset[int] = frozenset()


def judge(
    reading: Reading,
    *,
    end_before_loss: int,
    loss_candidates: tuple[int, ...],
    loss_certain: bool,
    day_effects: Iterable[Effect],
    new_day_effects: Iterable[Effect],
    floor: int | None = None,
) -> _Judged | None:
    """Whether one reading equals a value the ledger allows at its time, or
    ``None`` when it cannot be judged: a disputed battle may be in it, or too
    many battles may be. ``floor`` is the weekly raise: an ended-day value
    below it may also be read raised to it, before any new-day battle."""
    day_effects = tuple(day_effects)
    at = reading.read_at
    base = end_before_loss
    missed: list[str] = []
    # A battle the reading may or may not show: the change if it does not
    # (ended day) or does (new day), its identity, and which day.
    maybes: list[tuple[int, str, bool]] = []
    new_day_change = 0
    # An undisputed battle worth no trophies shows the same either way.
    for effect in day_effects:
        if effect.lands_until <= at or not (effect.change or effect.disputed):
            continue
        if effect.disputed:
            return None
        if effect.lands_from > at:
            base -= effect.change
            missed.append(effect.identity)
        else:
            maybes.append((-effect.change, effect.identity, True))
    for effect in new_day_effects:
        if effect.lands_from > at or not (effect.change or effect.disputed):
            continue
        if effect.disputed:
            return None
        if effect.lands_until <= at:
            new_day_change += effect.change
        else:
            maybes.append((effect.change, effect.identity, False))
    if len(maybes) > MAX_AMBIGUOUS_BATTLES:
        return None
    losses = [0, *(loss for loss in loss_candidates if loss)]

    def fits(ended: int, new: int) -> bool:
        return reading.trophies == ended + new or (
            floor is not None and ended < floor and reading.trophies == floor + new)

    # The game can credit an attack to the attacker's own profile long after
    # it: on 6 October 2026 #P20G0CUJY read 4,766 at 05:02:41 without all 308
    # of the day's attack gains, and 5,074 at 05:09:56. The profile catches
    # up when the player stops attacking, so a reading short of exactly the
    # latest landed attacks' gains, of either day, shows that lag, tried only
    # when nothing else fits. A battle in flight is read both ways either way.
    attacks = sorted(
        [(effect, True) for effect in day_effects]
        + [(effect, False) for effect in new_day_effects if effect.lands_from <= at],
        key=lambda item: item[0].lands_from)
    attacks = [(effect, ended) for effect, ended in attacks
               if effect.change > 0 and effect.lands_until <= at]
    runs = [attacks[len(attacks) - count:] for count in range(len(attacks) + 1)]
    found = []
    for run in runs:
        short = sum(effect.change for effect, ended in run if ended)
        new_short = sum(effect.change for effect, ended in run if not ended)
        found += [
            (run, chosen, loss)
            for count in range(len(maybes) + 1)
            for chosen in combinations(maybes, count)
            for loss in losses
            if fits(base - short - loss + sum(delta for delta, _, ended in chosen if ended),
                    new_day_change - new_short
                    + sum(delta for delta, _, ended in chosen if not ended))
        ]
        if found and not run:
            break
    if found:
        run, chosen, loss = found[0]
        lagged = tuple(effect.identity for effect, _ in run)
        return _Judged(
            # Any battle in flight leaves the reading read one way or the
            # other, even when it fits as read.
            reading, True, bool(maybes or run), loss,
            tuple(missed) + tuple(identity for _, identity, ended in chosen if ended)
            + tuple(effect.identity for effect, ended in run if ended),
            new_day_change - sum(effect.change for effect, ended in run if not ended)
            + sum(delta for delta, _, ended in chosen if not ended),
            lagged=lagged, losses=frozenset(loss for _, _, loss in found),
        )
    # The residual is against the day's end after a certain loss, the value
    # every saved result records as the expected next start.
    expected = base + new_day_change - (
        max(loss_candidates) if loss_certain and loss_candidates else 0)
    return _Judged(reading, False, bool(maybes), residual=reading.trophies - expected)


def decide(
    readings: Iterable[Reading],
    *,
    reset_at: datetime,
    end_before_loss: int,
    loss_candidates: tuple[int, ...],
    loss_certain: bool,
    day_effects: tuple[Effect, ...],
    new_day_effects: tuple[Effect, ...],
    unknown_from: datetime | None = None,
    start_proven: bool,
    floor: int | None = None,
) -> Verdict:
    """The day's verdict from every reading taken from its end Reset on.

    A reading is trustworthy unless it is confirm-only or taken at or after
    ``unknown_from``, the earliest report of a new-day battle only the
    opponent has reported, which it may show. A trustworthy reading that
    fits no value, each battle in flight read both ways, contradicts the
    day, and no later match erases that. One short of exactly the gains of
    the player's latest landed attacks, the game's attacker-profile lag,
    never contradicts and confirms nothing. Once every way a trustworthy
    reading fits has the loss landed, a later one that fits only without it
    contradicts too: a loss does not un-land. Otherwise the last
    trustworthy clean match decides, a match proving the day with the loss
    landed when one is certain or a reading showed it, or, read before the
    loss, proving its end before the loss; with none, the last clean
    confirm-only match showing the most loss confirms. Neither it nor a
    reading taken from ``unknown_from`` shows a possible loss did not land,
    nor the latter that it did. A reading that fits
    only with a battle not yet shown, or read one way, is a guess: taken
    when the day's start is proven and no clean reading decided.
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
                floor=floor,
            )
            for reading in sorted(readings, key=lambda item: (item.read_at, item.trophies))
            if reading.read_at >= reset_at
        )
        if judged is not None
    ]

    def unknown(item: _Judged) -> bool:
        return unknown_from is not None and item.reading.read_at >= unknown_from

    def trusted(item: _Judged) -> bool:
        return not item.reading.confirm_only and not unknown(item)

    def clean(item: _Judged) -> bool:
        return item.matched and not item.ambiguous and not item.missed

    lagged = tuple(dict.fromkeys(name for item in judged for name in item.lagged))
    contradiction = next((
        Verdict("contradicted", item.reading, residual=item.residual, lagged=lagged)
        for item in judged
        if trusted(item) and not item.matched
    ), None)
    matches = [item for item in judged if trusted(item) and clean(item)]
    landed = next((item for item in judged
                   if trusted(item) and item.matched and 0 not in item.losses), None)
    if contradiction is None and landed is not None:
        contradiction = next((
            Verdict("contradicted", item.reading, residual=landed.loss - item.loss,
                    lagged=lagged)
            for item in judged
            if trusted(item) and item.matched and landed.loss not in item.losses
            and item.reading.read_at > landed.reading.read_at
        ), None)
    if contradiction is not None:
        return contradiction
    # A confirm-only reading may show a possible loss landed, but showing none
    # proves nothing: it may be read before the loss lands. One that may show
    # a battle only the opponent has reported proves neither.
    possible = bool(loss_candidates) and not loss_certain
    judged = [item for item in judged
              if not possible or not unknown(item) and (item.loss or trusted(item))]
    matches = matches or [item for item in judged if clean(item)]
    shown = landed.loss if landed is not None else 0
    if matches:
        loss = max(shown, *(item.loss for item in matches))
        decider = ([item for item in matches if item.loss == loss] or matches)[-1]
        return Verdict(
            # A certain loss must have landed for the whole day; a possible
            # one (a day with no defense slot used) is uncharged until a
            # reading shows otherwise.
            "verified", decider.reading, loss, bool(loss or not loss_certain),
            (), decider.new_day_change, clean=True, lagged=lagged,
        )
    # A reading showing the lag confirms nothing, and no guess stands beside it.
    guessed = next((item for item in judged if item.matched), None)
    if guessed is not None and start_proven and not lagged:
        return Verdict(
            "verified", guessed.reading, max(shown, guessed.loss), False,
            guessed.missed, guessed.new_day_change, lagged=lagged,
        )
    # Its evidence keeps the last reading that showed the lag, when one did.
    lagging = [item.reading for item in judged if item.lagged]
    return Verdict("unverified", lagging[-1] if lagging else None, lagged=lagged)


def contradiction_during_day(
    readings: Iterable[Reading],
    *,
    day_start: datetime,
    reset_at: datetime,
    start: int,
    pending_loss: int,
    day_effects: tuple[Effect, ...],
) -> Verdict | None:
    """The first trustworthy reading taken during the day, from
    ``DAY_READINGS_FROM`` after its start, that fits no value the ledger
    allows at its time: the start plus every battle landed by then, plus
    the day before's automatic loss, ``pending_loss``, while it may not have
    landed, which has no fixed time; once every way a reading fits has it
    landed, every later one must too. A battle in flight is read both ways."""
    total = start + pending_loss + sum(effect.change for effect in day_effects)
    for reading in sorted(readings, key=lambda item: (item.read_at, item.trophies)):
        if reading.confirm_only or not (
                day_start + DAY_READINGS_FROM <= reading.read_at < reset_at):
            continue
        judged = judge(
            reading, end_before_loss=total,
            loss_candidates=(pending_loss,) if pending_loss else (),
            loss_certain=bool(pending_loss), day_effects=day_effects, new_day_effects=(),
        )
        if judged is None:
            continue
        if not judged.matched:
            return Verdict("contradicted", reading, residual=judged.residual)
        if judged.losses and 0 not in judged.losses:
            total, pending_loss = total - judged.loss, 0
    return None
