"""When an ordinary check also fetches the battle log.

A battle moves the Clasher's trophies or win counts, or shows up in a tracked
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
from datetime import UTC, datetime, timedelta
from typing import Any

from .battle import parse_battle_log

# Read from each valid profile: a battle changes at least one of them unless it
# is a 0-star attack under 10% destruction.
PROFILE_SIGNALS = ("trophies", "attackWins", "defenseWins")
# Battles that change nothing the other rules see are found by this fetch.
SAFETY_INTERVAL = timedelta(minutes=15)
# The official API caches each endpoint for up to 60 seconds, so a profile or
# another player's log can show a battle before this player's log does. An owed
# fetch only counts when it starts at least this long after that evidence.
FOLLOW_UP_GAP = timedelta(seconds=60)
# Players whose tag's SHA-256 starts with a byte below this (13/256, about 5%)
# fetch both responses on every check. Comparing them with everyone else
# measures how much later battle details appear. The same group in SQL:
# get_byte(sha256(convert_to(normalized_tag, 'UTF8')), 0) < 13
CONTROL_BYTE_LIMIT = 13


def in_control_group(normalized_tag: str) -> bool:
    return hashlib.sha256(normalized_tag.encode()).digest()[0] < CONTROL_BYTE_LIMIT


@dataclass(slots=True)
class _Player:
    signals: tuple[int, ...] | None = None
    profile_at: datetime | None = None
    # Battle-log fetches still owed: 2 after a profile change (that check and a
    # later one), 1 for the follow-up or after an opponent's log showed a battle.
    owed: int = 0
    owed_since: datetime | None = None
    # The last owed fetch counts only when it starts at or after this time.
    follow_up_after: datetime | None = None
    battle_log_at: datetime | None = None
    latest_battle_at: datetime | None = None


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
            read = _battles(body, normalized_tag, completed_at)
            if read is None:
                return False
            battles, complete = read
            player = self._players.setdefault(normalized_tag, _Player())
            if complete:
                _note_complete_log(player, started_at, completed_at)
            self._note_battles(player, battles, completed_at)
            return complete
        return False

    def _note_battles(
        self,
        player: _Player,
        battles: list[tuple[datetime, str]],
        completed_at: datetime,
    ) -> None:
        previous_latest = player.latest_battle_at
        for battle_at, opponent_tag in battles:
            if player.latest_battle_at is None or battle_at > player.latest_battle_at:
                player.latest_battle_at = battle_at
            if previous_latest is not None and battle_at <= previous_latest:
                continue
            opponent = self._players.get(opponent_tag)
            if opponent is not None and (
                opponent.latest_battle_at is None
                or opponent.latest_battle_at < battle_at
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
    player: _Player, signals: tuple[int, ...], completed_at: datetime
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


def _profile_signals(body: bytes) -> tuple[int, ...] | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    signals = tuple(payload.get(name) for name in PROFILE_SIGNALS)
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in signals
    ):
        return None
    return signals


def _battles(
    body: bytes, normalized_tag: str, completed_at: datetime
) -> tuple[list[tuple[datetime, str]], bool] | None:
    """Each valid Legend battle's time and opponent, and whether all were valid.

    A row is valid when it passes the worker's own row rules and has a live
    battleTimestamp. None means the body is not a battle log at all.
    """
    try:
        log = parse_battle_log(
            body, expected_tag=normalized_tag, observed_at=completed_at
        )
    except ValueError:
        return None
    battles = []
    complete = True
    for row in log.rows:
        if row.outcome == "ignored_non_legend":
            continue
        battle_at = _battle_at(row.source_json)
        if row.battle is None or battle_at is None:
            complete = False
        else:
            battles.append((battle_at, row.battle.opponent_tag))
    return battles, complete


def _battle_at(entry: Any) -> datetime | None:
    # Live entries carry the date as compact text in battleTimestamp;
    # battleTime is the battle's length in seconds.
    value = entry.get("battleTimestamp") if isinstance(entry, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None
