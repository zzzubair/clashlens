from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from test_collector import _Client, _collector, _Spool, _Store

from clashlens.battle_log_schedule import BattleLogSchedule, in_control_group
from clashlens.collector_db import CollectorWork
from clashlens.collector_http import FetchedResponse, KeyPool, ProviderFailure

collector_module = importlib.import_module("clashlens.collector")
START = datetime(2026, 10, 2, 12, tzinfo=UTC)
CHECK_INTERVAL = timedelta(seconds=90)
TAG = "#2PP"
OPPONENT = "#8PP"
CONTROL_TAG = "#28Q"
BOTH = ["profile", "battle_log"]
PROFILE = ["profile"]


def _battle(opponent: str, at: datetime, *, attack: bool = True) -> dict[str, Any]:
    return {
        "battleType": "legend",
        "attack": attack,
        "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
        "opponentPlayerTag": opponent,
    }


class _GameClient(_Client):
    """Fake Clash API whose per-player profiles and battle logs the test sets."""

    def __init__(self, spool: _Spool, clock: list[datetime]) -> None:
        super().__init__(spool)
        self.clock = clock
        self.profiles: dict[str, dict[str, Any]] = {}
        self.logs: dict[str, list[dict[str, Any]]] = {}
        self.profile_status = 200
        self.battle_log_status = 200
        self.profile_fails = False
        self.fetched: list[str] = []

    def profile(self, tag: str = TAG) -> dict[str, Any]:
        return self.profiles.setdefault(
            tag, {"tag": tag, "trophies": 5_000, "attackWins": 10, "defenseWins": 7}
        )

    async def fetch_player(
        self, _pool: KeyPool, tag: str, endpoint: str
    ) -> FetchedResponse:
        self.fetched.append(endpoint)
        if endpoint == "profile" and self.profile_fails:
            raise ProviderFailure("timeout", retryable=True)
        if endpoint == "profile":
            body, status = self.profile(tag), self.profile_status
        else:
            body = {"items": self.logs.get(tag, [])}
            status = self.battle_log_status
        # Each request takes a second of game time.
        started_at = self.clock[0]
        self.clock[0] += timedelta(seconds=1)
        return FetchedResponse(
            endpoint=endpoint,
            body=json.dumps(body).encode(),
            http_status=status,
            request_started_at=started_at,
            response_completed_at=self.clock[0],
            key_label="regular-1",
            headers={},
        )


@pytest.fixture
def game(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    clock = [START]
    monkeypatch.setattr(
        collector_module, "datetime", SimpleNamespace(now=lambda _zone: clock[0])
    )
    spool = _Spool()
    client = _GameClient(spool, clock)
    collector = _collector(spool, _Store(spool), client)

    def check(
        tag: str = TAG, *, after: timedelta = CHECK_INTERVAL, lane: str = "ordinary"
    ) -> list[str]:
        """Run one check `after` the previous one; return its requests in order."""
        clock[0] += after
        client.fetched.clear()
        asyncio.run(
            collector.collect_player(CollectorWork(1, tag, clock[0]), lane=lane)
        )
        return sorted(client.fetched, reverse=True)

    def settle(tag: str = TAG) -> None:
        """A restarted collector knows nothing yet, so it checks twice in full."""
        assert [check(tag) for _ in range(3)] == [BOTH, BOTH, PROFILE]

    return SimpleNamespace(check=check, settle=settle, client=client, clock=clock)


def test_quiet_player_needs_only_the_profile_once_settled(
    game: SimpleNamespace,
) -> None:
    game.settle()

    assert game.check() == PROFILE


@pytest.mark.parametrize("lane", ["reset", "interactive"])
def test_reset_refresh_and_first_lookup_always_fetch_both(
    game: SimpleNamespace, lane: str
) -> None:
    # Refresh and first-time lookups run in the interactive lane.
    game.settle()

    assert [game.check(lane=lane) for _ in range(3)] == [BOTH] * 3


@pytest.mark.parametrize(
    ("signal", "change"),
    [
        ("trophies", 40),
        # 49%, 0 stars: the attacker gains trophies, the defender loses none
        # but wins a defense.
        ("defenseWins", 1),
        ("attackWins", 1),
    ],
)
def test_profile_change_fetches_the_log_on_that_check_and_the_next(
    game: SimpleNamespace, signal: str, change: int
) -> None:
    game.settle()
    game.client.profile()[signal] += change

    assert [game.check() for _ in range(3)] == [BOTH, BOTH, PROFILE]


def test_new_change_during_the_follow_up_owes_two_more_fetches(
    game: SimpleNamespace,
) -> None:
    game.settle()
    game.client.profile()["trophies"] += 40
    game.check()
    game.client.profile()["trophies"] -= 32

    assert [game.check() for _ in range(3)] == [BOTH, BOTH, PROFILE]


def test_follow_up_waits_for_a_check_past_the_api_cache(
    game: SimpleNamespace,
) -> None:
    game.settle()
    game.client.profile()["trophies"] += 40
    game.check()

    # A quick re-check inside the API's 60-second cache cannot be the follow-up.
    assert game.check(after=timedelta(seconds=20)) == BOTH
    assert game.check() == BOTH
    assert game.check() == PROFILE


def test_new_battle_in_a_log_fetches_the_tracked_opponents_log(
    game: SimpleNamespace,
) -> None:
    game.settle(TAG)
    game.settle(OPPONENT)
    battle_at = game.clock[0]
    game.client.logs[TAG] = [_battle(OPPONENT, battle_at)]
    game.client.logs[OPPONENT] = [_battle(TAG, battle_at, attack=False)]
    game.client.profile(TAG)["trophies"] += 30
    # The opponent's profile shows no change yet, for example from the API cache.

    assert game.check(TAG) == BOTH
    assert game.check(OPPONENT) == BOTH
    assert game.check(OPPONENT) == PROFILE


def test_battle_moving_nothing_is_found_by_the_safety_fetch(
    game: SimpleNamespace,
) -> None:
    game.settle()
    requests = [game.check() for _ in range(10)]

    # Quiet checks run 91 seconds apart, so the tenth after the last log fetch
    # is the first at least 15 minutes later.
    assert requests == [PROFILE] * 8 + [BOTH] + [PROFILE]


@pytest.mark.parametrize(
    "failure",
    ["transport", "server_error", "missing_trophies", "malformed_trophies"],
)
def test_unreadable_profile_fetches_the_log(
    game: SimpleNamespace, failure: str
) -> None:
    game.settle()
    if failure == "transport":
        game.client.profile_fails = True
    elif failure == "server_error":
        game.client.profile_status = 503
    else:
        game.client.profile()["trophies"] = (
            None if failure == "missing_trophies" else "5000"
        )

    assert game.check() == BOTH


def test_failed_log_fetch_keeps_the_obligation(game: SimpleNamespace) -> None:
    game.settle()
    game.client.profile()["trophies"] += 40
    game.client.battle_log_status = 503
    assert game.check() == BOTH

    game.client.battle_log_status = 200
    assert [game.check() for _ in range(3)] == [BOTH, BOTH, PROFILE]


def test_control_group_fetches_both_on_every_check(game: SimpleNamespace) -> None:
    assert in_control_group(CONTROL_TAG)
    assert not in_control_group(TAG)
    assert not in_control_group(OPPONENT)

    assert [game.check(CONTROL_TAG) for _ in range(4)] == [BOTH] * 4


class _Schedule:
    """Drives BattleLogSchedule directly with exact response times."""

    def __init__(self) -> None:
        self.schedule = BattleLogSchedule()

    def profile(self, tag: str, at: datetime, trophies: int = 5_000) -> None:
        self.schedule.note_response(
            tag, "profile", {"trophies": trophies}, started_at=at, completed_at=at
        )

    def log(self, tag: str, started_at: datetime, *entries: dict[str, Any]) -> None:
        self.schedule.note_response(
            tag,
            "battle_log",
            list(entries),
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
        )

    def due(self, tag: str, at: datetime) -> bool:
        return self.schedule.due(tag, check_started_at=START, now=at)

    def settle(self, tag: str) -> datetime:
        """Two full checks a minute apart, as after a restart; returns the time."""
        for at in (START, START + timedelta(minutes=1)):
            self.profile(tag, at)
            self.log(tag, at)
        return START + timedelta(minutes=2)


def test_log_requested_before_the_change_was_seen_does_not_count() -> None:
    state = _Schedule()
    profile_at = START + timedelta(seconds=1)
    state.profile(TAG, profile_at)
    # A Reset or overlapping check asked for this log while the profile was open.
    state.log(TAG, START)
    assert state.due(TAG, profile_at)

    state.log(TAG, profile_at)
    state.log(TAG, profile_at + timedelta(seconds=1))
    assert state.due(TAG, profile_at + timedelta(seconds=2))

    state.log(TAG, profile_at + timedelta(seconds=61))
    assert not state.due(TAG, profile_at + timedelta(seconds=63))


def test_older_profile_cannot_replace_newer_trophies() -> None:
    state = _Schedule()
    now = state.settle(TAG)

    state.profile(TAG, START, trophies=4_900)

    assert not state.due(TAG, now)


def test_opponent_that_already_has_the_battle_is_not_refetched() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    battle_at = now - timedelta(seconds=30)

    state.log(OPPONENT, now, _battle(TAG, battle_at, attack=False))
    state.log(TAG, now, _battle(OPPONENT, battle_at))

    assert not state.due(OPPONENT, now + timedelta(seconds=2))


def test_first_log_seen_after_a_restart_owes_opponents_nothing() -> None:
    state = _Schedule()
    now = state.settle(OPPONENT)
    # The first log this collector sees may hold battles it never missed.
    state.profile(TAG, now)
    state.log(TAG, now, _battle(OPPONENT, now - timedelta(seconds=30)))

    assert not state.due(OPPONENT, now + timedelta(seconds=2))
