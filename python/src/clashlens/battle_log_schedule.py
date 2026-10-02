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
from dataclasses import dataclass
from datetime import datetime, timedelta

from .battle import LIVE_SOURCE_PARSER_VERSION, _parse_battle_timestamp, _parse_row

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
# Players whose tag's SHA-256 starts with a byte below this (13/256, about 5%)
# fetch both responses on every check. Comparing them with everyone else
# measures how much later battle details appear. The same group in SQL:
# get_byte(sha256(convert_to(normalized_tag, 'UTF8')), 0) < 13
CONTROL_BYTE_LIMIT = 13


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
    # Opponents of the battles near the newest, to recognise a battle that an
    # opponent's log reports under its own timestamp.
    recent: tuple[tuple[str, datetime], ...] = ()
    # Newest row time in any log, malformed rows included.
    latest_row_at: datetime | None = None


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
            battles, malformed_at = read
            player = self._players.setdefault(normalized_tag, _Player())
            # A malformed row holds the log owed once, for a corrected copy.
            # Live logs keep rows with no opponent for days.
            complete = all(
                at is not None
                and player.latest_row_at is not None
                and at <= player.latest_row_at
                for at in malformed_at
            )
            for at in [battle[0] for battle in battles] + malformed_at:
                if at is not None and (
                    player.latest_row_at is None or at > player.latest_row_at
                ):
                    player.latest_row_at = at
            if complete:
                _note_complete_log(player, started_at, completed_at)
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
        newest = player.latest_battle_at
        if newest is not None:
            player.recent = tuple(
                (tag, at)
                for tag, at in {
                    *player.recent,
                    *((tag, at) for at, tag, _attack in battles),
                }
                if at >= newest - 2 * SAME_BATTLE_GAP
            )
        for battle_at, opponent_tag, _attack in battles:
            opponent = self._players.get(opponent_tag)
            if opponent is not None and not _has_battle(
                opponent, normalized_tag, battle_at
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
            or now - player.battle_log_at >= SAFETY_INTERVAL
        )


def _has_battle(player: _Player, opponent_tag: str, battle_at: datetime) -> bool:
    """Whether the player's logs already reached a battle the opponent reported."""
    if player.latest_battle_at is None:
        return False
    if player.latest_battle_at > battle_at + SAME_BATTLE_GAP:
        return True
    return any(
        tag == opponent_tag and abs(at - battle_at) <= SAME_BATTLE_GAP
        for tag, at in player.recent
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
) -> tuple[list[tuple[datetime, str, bool]], list[datetime | None]] | None:
    """Each valid Legend battle's time, opponent and side, and malformed rows.

    A row is valid when it passes the worker's own row rules with an explicit
    battleTimestamp. Each malformed Legend row gives its readable
    battleTimestamp, or None. None means the body is not a battle log at all.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return None
    battles = []
    malformed_at: list[datetime | None] = []
    for index, item in enumerate(items):
        try:
            row = _parse_row(index, item, normalized_tag, LIVE_SOURCE_PARSER_VERSION)
        except Exception:  # noqa: BLE001 - one bad row never stops collection
            malformed_at.append(_row_time(item))
            continue
        if row.outcome == "ignored_non_legend":
            continue
        # Live battleTime is the battle's length, never a stand-in date.
        if row.battle is None or item.get("battleTimestamp") is None:
            malformed_at.append(_row_time(item))
        else:
            battles.append(
                (
                    row.battle.battle_timestamp,
                    row.battle.opponent_tag,
                    row.battle.perspective == "attacker",
                )
            )
    return battles, malformed_at


def _row_time(item: object) -> datetime | None:
    value = item.get("battleTimestamp") if isinstance(item, dict) else None
    if value is None:
        return None
    try:
        return _parse_battle_timestamp(value, LIVE_SOURCE_PARSER_VERSION)
    except Exception:  # noqa: BLE001 - an unreadable time never counts as seen
        return None
