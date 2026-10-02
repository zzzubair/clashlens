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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .response_fields import PROFILE_SIGNALS

# Battles that change nothing the other rules see are found by this fetch.
SAFETY_INTERVAL = timedelta(minutes=15)
# The official API caches each endpoint for up to 60 seconds, so a profile can
# show a battle before the battle log does. The follow-up fetch must start at
# least this long after the fetch that the profile change triggered.
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
    signals: tuple[Any, ...] | None = None
    profile_at: datetime | None = None
    # Battle-log fetches still owed: 2 after a profile change (that check and a
    # later one), 1 for the follow-up or after an opponent's log showed a battle.
    owed: int = 0
    owed_since: datetime | None = None
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
        fields: Any,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        """Record one saved successful response's used fields."""
        player = self._players.setdefault(normalized_tag, _Player())
        if endpoint == "profile":
            self._note_profile(player, fields, completed_at)
        elif endpoint == "battle_log" and isinstance(fields, list):
            self._note_battle_log(player, fields, started_at, completed_at)

    @staticmethod
    def _note_profile(player: _Player, fields: Any, completed_at: datetime) -> None:
        if not isinstance(fields, dict):
            return
        signals = tuple(fields.get(name) for name in PROFILE_SIGNALS)
        if (
            not isinstance(signals[0], int)
            or isinstance(signals[0], bool)
            or (player.profile_at is not None and completed_at < player.profile_at)
        ):
            return
        if signals != player.signals:
            player.owed = 2
            player.owed_since = completed_at
            player.follow_up_after = None
        player.signals = signals
        player.profile_at = completed_at

    def _note_battle_log(
        self,
        player: _Player,
        entries: list[Any],
        started_at: datetime,
        completed_at: datetime,
    ) -> None:
        first_log = player.battle_log_at is None
        if first_log or completed_at > player.battle_log_at:
            player.battle_log_at = completed_at
        # A log requested before the change was seen may not show it.
        if (
            player.owed == 2
            and player.owed_since is not None
            and started_at >= player.owed_since
        ):
            player.owed = 1
            player.follow_up_after = started_at + FOLLOW_UP_GAP
        elif (
            player.owed == 1
            and player.follow_up_after is not None
            and started_at >= player.follow_up_after
        ):
            player.owed = 0
            player.follow_up_after = None
        previous_latest = player.latest_battle_at
        for entry in entries:
            battle_at = _battle_at(entry)
            if battle_at is None:
                continue
            if player.latest_battle_at is None or battle_at > player.latest_battle_at:
                player.latest_battle_at = battle_at
            # The first log this collector sees has no known new battles.
            if first_log or (
                previous_latest is not None and battle_at <= previous_latest
            ):
                continue
            opponent = self._players.get(entry.get("opponentPlayerTag"))
            if opponent is not None and (
                opponent.latest_battle_at is None
                or opponent.latest_battle_at < battle_at
            ):
                _owe_one_fetch_after(opponent, completed_at)

    def due(
        self,
        normalized_tag: str,
        *,
        check_started_at: datetime,
        now: datetime,
    ) -> bool:
        """Whether this check needs the battle log after its profile."""
        player = self._players.get(normalized_tag)
        return (
            player is None
            or in_control_group(normalized_tag)
            # This check saw no valid trophies: a failed or unreadable profile.
            or player.profile_at is None
            or player.profile_at < check_started_at
            or player.owed > 0
            or player.battle_log_at is None
            or now - player.battle_log_at >= SAFETY_INTERVAL
        )


def _owe_one_fetch_after(player: _Player, at: datetime) -> None:
    """Owe a battle-log fetch that starts no earlier than `at`."""
    if player.owed == 2:
        return  # Its first owed fetch has not started yet, so it starts later.
    if player.owed == 1 and player.follow_up_after is not None:
        player.follow_up_after = max(player.follow_up_after, at)
    else:
        player.owed = 1
        player.follow_up_after = at


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
