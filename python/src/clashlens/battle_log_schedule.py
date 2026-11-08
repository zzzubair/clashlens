"""When an ordinary check also fetches the battle log.

A battle moves the defender's trophies or count of defenses won, often comes
minutes after the attacker's previous attack, and shows up in a tracked
opponent's battle log, so ordinary checks fetch the profile first and the
battle log only when it can have changed. See
docs/collector-polling.md#battle-log-only-when-it-can-have-changed.

A player whose last profile reported Legend I with Season ID 0 and unchanged
counts, seen by this collector before the last Reset and with no valid Season
or changed counts since, needs the profile only every 15 minutes; its battle log keeps the rules above, seeing
profile changes only then. See
docs/collector-polling.md#season-0-profiles.

Each player's entry holds three explicit states: the battle-log fetches it
owes (``LogDebt``), whether its Legend day is finished (``DayProgress``) and
how often its profile is checked (``SeasonChecks``). Each moves only in its
own function below.

This state lives only in collector memory, one small entry per player checked
since the collector started. A restart forgets it, which makes every player's
next checks fetch both responses again, so forgetting costs requests, never
battles. A failed or unusable response moves nothing.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Literal

from .battle import LIVE_SOURCE_PARSER_VERSION, _parse_row, is_no_opponent_row
from .domain import RANKED_DAY_DURATION, battle_day_for, ranked_day_for
from .profile import PROFILE_PARSER_VERSION, ProfileParseError, parse_profile
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

# Read from each valid profile. A defender's profile shows a battle at once;
# an attacker's often shows an attack many minutes late.
PROFILE_SIGNALS = ("trophies", "attackWins", "defenseWins")
# This achievement counts every defense won, including a 0-trophy one such as
# a 0-star 30% attack. On 2026-10-02 it did so even for the two in three
# profiles whose attackWins and defenseWins stayed 0.
DEFENSES_WON = "Unbreakable"
# Battles that change nothing the other rules see are found by this fetch.
SAFETY_INTERVAL = timedelta(minutes=15)
# On 2026-10-03, 1,357 players' profiles had said Season 0 for 63-65 hours,
# none with a Legend battle this Season. Each profile cost 561 requests a day.
SEASON_ZERO_RECHECK = timedelta(minutes=15)
# The official API caches each endpoint for up to 60 seconds, so a profile or
# another player's log can show a battle before this player's log does. An owed
# fetch only counts when it starts at least this long after that evidence.
FOLLOW_UP_GAP = timedelta(seconds=60)
# A Clasher who has just attacked often attacks again within minutes, and their
# own profile may not show it. After a new attack the log stays owed until a
# fetch starts this long after it, so the next attack, even a 0-star one that
# moves no defender trophies, shows up within about one check.
ATTACK_WATCH = timedelta(minutes=10)
# The two players' logs time the same battle differently: the attacker's
# battleTimestamp was 108-211 s after the defender's on 2026-10-02.
SAME_BATTLE_GAP = timedelta(minutes=5)
# A Clasher with 8 attacks and 8 defenses on the current Legend day can battle
# no more until the next Reset. The attacker's profile can still show the last
# attack late: from September 29 to October 3, 2026, trophies changed more than
# 15 minutes after the 16th battle on 177 of 40,743 such days (0.43%).
FINISHED_SETTLE = timedelta(minutes=15)
# Until that Reset a finished Clasher's profile is still checked this often,
# without the battle log, so their page and Live Leaderboard entry, stale after
# 10 minutes, stay fresh even when a check starts a minute or two late.
FINISHED_RECHECK = timedelta(minutes=8)
# Regular checks stop this long before each Reset, at 04:55 UTC.
ADMISSION_CLOSE = timedelta(minutes=5)
# A due check can start a minute or two late when the keys set the pace, so a
# finished Clasher's last 8-minute wait ends this long before 04:55.
ADMISSION_DELAY = timedelta(minutes=2)
# Players whose tag's SHA-256 starts with a byte below this (13/256, about 5%)
# fetch both responses on every check. Comparing them with everyone else
# measures how much later battle details appear. The same group in SQL:
# get_byte(sha256(convert_to(normalized_tag, 'UTF8')), 0) < 13
CONTROL_BYTE_LIMIT = 13
# One remembered battle: its time in whole seconds, wrapping in 2106, and a
# 4-byte digest of its opponent and side.
_BATTLE = struct.Struct(">I4s")
# Bytes of each remembered malformed row's content digest.
_ROW_DIGEST_BYTES = 8


def in_control_group(normalized_tag: str) -> bool:
    return hashlib.sha256(normalized_tag.encode()).digest()[0] < CONTROL_BYTE_LIMIT


@dataclass(frozen=True, slots=True)
class CaughtUp:
    """No battle-log fetch is owed."""


@dataclass(frozen=True, slots=True)
class FetchOwed:
    """The profile changed at `since`: a fetch started at or after it is owed,
    then a follow-up. `follow_up_after`, if set, is the earliest start an
    earlier follow-up was waiting for, kept for the follow-up owed next."""

    since: datetime
    follow_up_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class FollowUpOwed:
    """One fetch owed, counting only when it starts at or after `after`."""

    after: datetime


# Battle-log fetching debt: the battle-log fetches a player still owes.
#
# CaughtUp -profile_changed-> FetchOwed -log_saved (requested at or after its
# `since`)-> FollowUpOwed -log_saved (requested at or after its `after`)->
# CaughtUp. `follow_up` (an opponent's log showed a battle this player's log
# lacks, a new attack is being watched, or a log had a new malformed row)
# moves CaughtUp to FollowUpOwed, or pushes a waiting start later. A player
# never checked, or with no saved log since the collector started, is due a
# log whatever its debt. Only `log_debt_after` moves it. A failed fetch or a
# log with a new malformed row never pays it off, so the next check retries;
# a restart forgets it, so every player starts unknown and fetches both.
LogDebt = CaughtUp | FetchOwed | FollowUpOwed
DebtEvent = Literal["profile_changed", "follow_up", "log_saved"]
CAUGHT_UP = CaughtUp()


def log_debt_after(debt: LogDebt, event: DebtEvent, at: datetime) -> LogDebt:
    """The debt after `event`: a profile changed, read at `at`; a follow-up
    must start no earlier than `at`; or a complete log was requested at `at`."""
    if event == "profile_changed":
        return FetchOwed(at, _earliest_start(debt))
    if event == "follow_up":
        if isinstance(debt, FetchOwed):
            return FetchOwed(debt.since, _later(debt.follow_up_after, at))
        return FollowUpOwed(_later(_earliest_start(debt), at))
    # A log requested before the change was seen may not show it.
    if isinstance(debt, FetchOwed) and at >= debt.since:
        return FollowUpOwed(_later(debt.follow_up_after, at + FOLLOW_UP_GAP))
    if isinstance(debt, FollowUpOwed) and at >= debt.after:
        return CAUGHT_UP
    return debt


def _earliest_start(debt: LogDebt) -> datetime | None:
    if isinstance(debt, FetchOwed):
        return debt.follow_up_after
    return debt.after if isinstance(debt, FollowUpOwed) else None


def _later(start: datetime | None, at: datetime) -> datetime:
    return at if start is None or start < at else start


class DayProgress(Enum):
    """Finished-for-the-day polling, derived by `_day_progress` from the last
    saved log and the clock, never stored.

    PLAYING: the last log does not show exactly 8 valid attacks and 8 valid
    defenses on the Legend day of now, or it has a malformed row; ordinary
    checks. SETTLING: it does, but its last battle is under FINISHED_SETTLE
    old, so the profile can still change. FINISHED: settled; the 15-minute
    safety fetch stops and, while no fetch is owed and the profile is usable,
    the player is checked with the profile only every FINISHED_RECHECK, the
    last such wait ending before 04:53. The next Reset, a log that is not
    finished or a malformed row moves it back to PLAYING; a profile change
    owes fetches, which resume full checks without changing it.
    """

    PLAYING = "playing"
    SETTLING = "settling"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class OrdinaryChecks:
    """The profile is fetched on every check. Until the Reset `until`, set by
    a valid Season or changed counts, an unchanged Season 0 profile keeps it so."""

    until: datetime | None = None


@dataclass(frozen=True, slots=True)
class SeasonZeroChecks:
    """The newest profile, read at `since`, reported Legend I with Season ID 0
    and unchanged counts: the profile is due again SEASON_ZERO_RECHECK later."""

    since: datetime


# Season 0 polling: how often a player's ordinary checks fetch the profile.
#
# OrdinaryChecks -unchanged Season 0 profile at or after its `until`->
# SeasonZeroChecks -another one-> SeasonZeroChecks (from that one). A valid
# Season or changed counts, read in any lane, move either to OrdinaryChecks
# until the next Reset; leaving SeasonZeroChecks for a valid Season also owes
# the battle log. Only `season_checks_after` moves it, on a usable profile no
# older than the newest. A failed profile moves nothing, so the next check
# retries it. A restart forgets it: ordinary checks, and the first profile
# counts as changed, so a wait resumes only from the next Reset.
SeasonChecks = OrdinaryChecks | SeasonZeroChecks


def season_checks_after(
    checks: SeasonChecks, at: datetime, *, season_zero: bool, counts_changed: bool
) -> SeasonChecks:
    """The checks after a usable profile read at `at`."""
    if not season_zero or counts_changed:
        until = checks.until if isinstance(checks, OrdinaryChecks) else None
        if until is None or until <= at:
            until = ranked_day_for(at).end
        return OrdinaryChecks(until)
    if isinstance(checks, OrdinaryChecks) and checks.until is not None and at < checks.until:
        return checks
    return SeasonZeroChecks(at)


@dataclass(slots=True)
class _Player:
    signals: tuple[int | None, ...] | None = None
    profile_at: datetime | None = None
    debt: LogDebt = CAUGHT_UP
    battle_log_at: datetime | None = None
    latest_battle_at: datetime | None = None
    # The valid battles of the last log, packed as _BATTLE records, to
    # recognise a battle that an opponent's log reports under its own
    # timestamp, and to tell an opponent about each battle only once.
    battles: bytes = b""
    # Digests of the malformed rows of the last log, each of its own content.
    malformed: bytes = b""
    # The Legend day on which the last log showed exactly 8 valid attacks and
    # 8 valid defenses, with no malformed rows.
    finished_day: datetime | None = None
    checks: SeasonChecks = OrdinaryChecks()


class BattleLogSchedule:
    def __init__(self) -> None:
        self._players: dict[str, _Player] = {}

    def note_response(
        self,
        normalized_tag: str,
        endpoint: str,
        body: bytes,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> bool:
        """Record one saved successful response; False when it is unusable."""
        if endpoint == "profile":
            signals = _profile_signals(body)
            if signals is None:
                return False
            player = self._players.setdefault(normalized_tag, _Player())
            if player.profile_at is None or completed_at >= player.profile_at:
                season_zero = _season_zero(body, normalized_tag, completed_at)
                if not season_zero and isinstance(player.checks, SeasonZeroChecks):
                    # A valid Season counts as a change, so the log is fetched.
                    player.signals = None
                # Changed counts mean battles; a restarted collector cannot
                # tell, so it assumes they did.
                player.checks = season_checks_after(
                    player.checks,
                    completed_at,
                    season_zero=season_zero,
                    counts_changed=player.signals != signals,
                )
            _note_profile(player, signals, completed_at)
            return True
        if endpoint == "battle_log":
            read = _battles(body, normalized_tag)
            if read is None:
                return False
            battles, malformed = read
            player = self._players.setdefault(normalized_tag, _Player())
            # A newly seen malformed row owes one fetch past the API cache, for
            # a corrected copy. Live logs keep rows with no opponent for days.
            complete = _row_digests(malformed) <= _row_digests(player.malformed)
            player.malformed = malformed
            player.finished_day = None if malformed else _finished_day(battles)
            if complete:
                if player.battle_log_at is None or completed_at > player.battle_log_at:
                    player.battle_log_at = completed_at
                player.debt = log_debt_after(player.debt, "log_saved", started_at)
            else:
                player.debt = log_debt_after(
                    player.debt, "follow_up", started_at + FOLLOW_UP_GAP
                )
            self._note_battles(normalized_tag, player, battles, completed_at)
            return complete
        return False

    def _note_battles(
        self,
        normalized_tag: str,
        player: _Player,
        battles: list[tuple[datetime, str, bool]],
        completed_at: datetime,
    ) -> None:
        previous = player.latest_battle_at
        known = set(_BATTLE.iter_unpack(player.battles))
        records = [_record(*battle) for battle in battles]
        player.battles = b"".join(_BATTLE.pack(*record) for record in records)
        for battle_at, _opponent_tag, attack in battles:
            if player.latest_battle_at is None or battle_at > player.latest_battle_at:
                player.latest_battle_at = battle_at
            if (
                attack
                and (previous is None or battle_at > previous)
                and battle_at + ATTACK_WATCH > completed_at
            ):
                player.debt = log_debt_after(
                    player.debt, "follow_up", battle_at + ATTACK_WATCH
                )
        for (battle_at, opponent_tag, attack), record in zip(battles, records):
            # Only a battle missing from this player's last log tells its
            # opponent, so an unchanged log never does.
            if record in known:
                continue
            opponent = self._players.get(opponent_tag)
            if opponent is not None and not _has_battle(
                opponent, normalized_tag, battle_at, not attack
            ):
                opponent.debt = log_debt_after(
                    opponent.debt, "follow_up", completed_at + FOLLOW_UP_GAP
                )

    def profile_due(self, normalized_tag: str, *, now: datetime) -> bool:
        """Whether an ordinary check fetches the profile. An unchanged Season 0
        one waits 15 minutes; any valid or changed profile, Refresh or Reset
        included, ends the wait until the next Reset."""
        player = self._players.get(normalized_tag)
        return (
            player is None
            or not isinstance(player.checks, SeasonZeroChecks)
            or now - player.checks.since >= SEASON_ZERO_RECHECK
        )

    def due(
        self,
        normalized_tag: str,
        *,
        profile_usable: bool,
        now: datetime,
    ) -> bool:
        """Whether this check needs the battle log after its own profile."""
        player = self._players.get(normalized_tag)
        return (
            # This check's profile failed, was an error or had unusable counts.
            not profile_usable
            or player is None
            or in_control_group(normalized_tag)
            or player.debt != CAUGHT_UP
            or player.battle_log_at is None
            or (
                now - player.battle_log_at >= SAFETY_INTERVAL
                and _day_progress(player, now) is not DayProgress.FINISHED
            )
        )

    def finished_recheck_at(
        self,
        normalized_tag: str,
        *,
        profile_usable: bool,
        now: datetime,
    ) -> datetime | None:
        """When a Clasher who finished the Legend day needs the next check.

        That is FINISHED_RECHECK later, unless that is within ADMISSION_DELAY
        of the 04:55 close of regular checks, when the last saved battle log
        shows exactly 8 valid attacks and 8 valid defenses on the Legend day
        of `now`, with no malformed rows; this
        check's profile was usable; no battle-log fetch is owed, so the profile
        did not change since the previous check; and the last battle is at
        least FINISHED_SETTLE old.
        """
        player = self._players.get(normalized_tag)
        if (
            not profile_usable
            or player is None
            or player.debt != CAUGHT_UP
            or player.finished_day is None
            or _day_progress(player, now) is not DayProgress.FINISHED
        ):
            return None
        reset = player.finished_day + RANKED_DAY_DURATION
        until = now + FINISHED_RECHECK
        if until >= reset - ADMISSION_CLOSE - ADMISSION_DELAY:
            return None
        return until


def _day_progress(player: _Player, now: datetime) -> DayProgress:
    if (
        player.finished_day is None
        or player.latest_battle_at is None
        or ranked_day_for(now).start != player.finished_day
    ):
        return DayProgress.PLAYING
    if now - player.latest_battle_at < FINISHED_SETTLE:
        return DayProgress.SETTLING
    return DayProgress.FINISHED


def _finished_day(battles: list[tuple[datetime, str, bool]]) -> datetime | None:
    """The log's newest Legend day, when it has every attack and defense."""
    days = [battle_day_for(battle_at).start for battle_at, _tag, _attack in battles]
    if not days:
        return None
    day = max(days)
    attacks = sum(
        1 for (_at, _tag, attack), on in zip(battles, days) if attack and on == day
    )
    defenses = days.count(day) - attacks
    if attacks == MAX_DAILY_ATTACKS and defenses == MAX_DAILY_DEFENSES:
        return day
    return None


def _has_battle(
    player: _Player, opponent_tag: str, battle_at: datetime, attack: bool
) -> bool:
    """Whether the player's logs already reached a battle the opponent reported.

    `attack` is this player's side of it, the opposite of the opponent's.
    """
    if player.latest_battle_at is None:
        return False
    if player.latest_battle_at > battle_at + SAME_BATTLE_GAP:
        return True
    at, side = _record(battle_at, opponent_tag, attack)
    return any(
        key == side and abs(seconds - at) <= SAME_BATTLE_GAP.total_seconds()
        for seconds, key in _BATTLE.iter_unpack(player.battles)
    )


def _record(battle_at: datetime, opponent_tag: str, attack: bool) -> tuple[int, bytes]:
    side = f"{opponent_tag} {attack}".encode()
    return (
        int(battle_at.timestamp()) & 0xFFFFFFFF,
        hashlib.blake2b(side, digest_size=4).digest(),
    )


def _note_profile(
    player: _Player, signals: tuple[int | None, ...], completed_at: datetime
) -> None:
    if player.profile_at is not None and completed_at < player.profile_at:
        return
    if signals != player.signals:
        player.debt = log_debt_after(player.debt, "profile_changed", completed_at)
    player.signals = signals
    player.profile_at = completed_at


def _profile_signals(body: bytes) -> tuple[int | None, ...] | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    signals = tuple(payload.get(name) for name in PROFILE_SIGNALS)
    if not all(_count(value) for value in signals):
        return None
    achievements = payload.get("achievements")
    defenses_won = next(
        (
            item.get("value")
            for item in (achievements if isinstance(achievements, list) else [])
            if isinstance(item, dict)
            and item.get("name") == DEFENSES_WON
            and item.get("village", "home") == "home"
            and _count(item.get("value"))
        ),
        # Without it the other signals still decide.
        None,
    )
    return (*signals, defenses_won)


def _season_zero(body: bytes, normalized_tag: str, observed_at: datetime) -> bool:
    """Whether the worker rejects this profile as Legend I with Season ID 0."""
    try:
        season = json.loads(body).get("currentLeagueSeasonId")
    except (ValueError, AttributeError):
        return False
    if season not in (0, "0") or isinstance(season, bool):
        return False
    try:
        profile = parse_profile(
            body,
            expected_tag=normalized_tag,
            observed_at=observed_at,
            endpoint_version="collector-schedule",
            parser_version=PROFILE_PARSER_VERSION,
        )
    except ProfileParseError:
        return False
    return (
        profile.eligibility_reason == "confirmed_legend_i"
        and profile.source_contract_state == "conflict"
    )


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _battles(
    body: bytes, normalized_tag: str
) -> tuple[list[tuple[datetime, str, bool]], bytes] | None:
    """Each valid Legend battle's time, opponent and side, and malformed rows.

    A row is valid when it passes the worker's own row rules with an explicit
    battleTimestamp. A "no opponent, no battle" row is skipped. Malformed
    Legend rows come as joined digests of their content. None means the body
    is not a battle log at all.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    battles = []
    malformed: set[bytes] = set()
    for index, item in enumerate(items):
        if is_no_opponent_row(item, LIVE_SOURCE_PARSER_VERSION):
            continue
        try:
            row = _parse_row(index, item, normalized_tag, LIVE_SOURCE_PARSER_VERSION)
        except Exception:  # noqa: BLE001 - one bad row never stops collection
            malformed.add(_row_identity(item))
            continue
        if row.outcome == "ignored_non_legend":
            continue
        # Live battleTime is the battle's length, never a stand-in date.
        if row.battle is None or item.get("battleTimestamp") is None:
            malformed.add(_row_identity(item))
        else:
            battles.append(
                (
                    row.battle.battle_timestamp,
                    row.battle.opponent_tag,
                    row.battle.perspective == "attacker",
                )
            )
    return battles, b"".join(sorted(malformed))


def _row_identity(item: object) -> bytes:
    content = json.dumps(item, sort_keys=True).encode()
    return hashlib.blake2b(content, digest_size=_ROW_DIGEST_BYTES).digest()


def _row_digests(packed: bytes) -> set[bytes]:
    return {
        packed[start : start + _ROW_DIGEST_BYTES]
        for start in range(0, len(packed), _ROW_DIGEST_BYTES)
    }
