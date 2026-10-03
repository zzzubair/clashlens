"""When an ordinary check also fetches the battle log.

A battle moves the defender's trophies or count of defenses won, often comes
minutes after the attacker's previous attack, and shows up in a tracked
opponent's battle log, so ordinary checks fetch the profile first and the
battle log only when it can have changed. See
docs/collector-polling.md#battle-log-only-when-it-can-have-changed.

This state lives only in collector memory, one small entry per player checked
since the collector started. A restart forgets it, which makes every player's
next checks fetch both responses again, so forgetting costs requests, never
battles.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta

from .battle import LIVE_SOURCE_PARSER_VERSION, _parse_row
from .domain import RANKED_DAY_DURATION, battle_day_for, ranked_day_for
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


@dataclass(slots=True)
class _Player:
    signals: tuple[int | None, ...] | None = None
    profile_at: datetime | None = None
    # Battle-log fetches still owed: 2 after a profile change (that check and a
    # later one), 1 for the follow-up, after an opponent's log showed a battle
    # or while watching for another attack.
    owed: int = 0
    owed_since: datetime | None = None
    # The last owed fetch counts only when it starts at or after this time.
    follow_up_after: datetime | None = None
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
                _note_complete_log(player, started_at, completed_at)
            else:
                player.owed = max(player.owed, 1)
                _start_no_earlier_than(player, started_at + FOLLOW_UP_GAP)
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
                player.owed = max(player.owed, 1)
                _start_no_earlier_than(player, battle_at + ATTACK_WATCH)
        for (battle_at, opponent_tag, attack), record in zip(battles, records):
            # Only a battle missing from this player's last log tells its
            # opponent, so an unchanged log never does.
            if record in known:
                continue
            opponent = self._players.get(opponent_tag)
            if opponent is not None and not _has_battle(
                opponent, normalized_tag, battle_at, not attack
            ):
                opponent.owed = max(opponent.owed, 1)
                _start_no_earlier_than(opponent, completed_at + FOLLOW_UP_GAP)

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
            or player.owed > 0
            or player.battle_log_at is None
            or (
                now - player.battle_log_at >= SAFETY_INTERVAL
                and _next_reset(player, now) is None
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
        if not profile_usable or player is None or player.owed > 0:
            return None
        reset = _next_reset(player, now)
        until = now + FINISHED_RECHECK
        if reset is None or until >= reset - ADMISSION_CLOSE - ADMISSION_DELAY:
            return None
        return until


def _next_reset(player: _Player, now: datetime) -> datetime | None:
    """The next Reset, when the player finished the Legend day of `now`."""
    if (
        player.finished_day is None
        or player.latest_battle_at is None
        or now - player.latest_battle_at < FINISHED_SETTLE
        or ranked_day_for(now).start != player.finished_day
    ):
        return None
    return player.finished_day + RANKED_DAY_DURATION


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


def _note_complete_log(
    player: _Player, started_at: datetime, completed_at: datetime
) -> None:
    if player.battle_log_at is None or completed_at > player.battle_log_at:
        player.battle_log_at = completed_at
    # A log requested before the change was seen may not show it.
    if (
        player.owed == 2
        and player.owed_since is not None
        and started_at >= player.owed_since
    ):
        player.owed = 1
        _start_no_earlier_than(player, started_at + FOLLOW_UP_GAP)
    elif (
        player.owed == 1
        and player.follow_up_after is not None
        and started_at >= player.follow_up_after
    ):
        player.owed = 0
        player.follow_up_after = None


def _note_profile(
    player: _Player, signals: tuple[int | None, ...], completed_at: datetime
) -> None:
    if player.profile_at is not None and completed_at < player.profile_at:
        return
    if signals != player.signals:
        player.owed = 2
        player.owed_since = completed_at
    player.signals = signals
    player.profile_at = completed_at


def _start_no_earlier_than(player: _Player, at: datetime) -> None:
    if player.follow_up_after is None or player.follow_up_after < at:
        player.follow_up_after = at


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


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _battles(
    body: bytes, normalized_tag: str
) -> tuple[list[tuple[datetime, str, bool]], bytes] | None:
    """Each valid Legend battle's time, opponent and side, and malformed rows.

    A row is valid when it passes the worker's own row rules with an explicit
    battleTimestamp. Malformed Legend rows come as joined digests of their
    content. None means the body is not a battle log at all.
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
