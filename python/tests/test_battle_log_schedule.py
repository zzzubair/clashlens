from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable
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
UNTRACKED = "#9PP"
CONTROL_TAG = "#28Q"
BOTH = ["profile", "battle_log"]
PROFILE = ["profile"]


def _battle(opponent: str, at: datetime, *, attack: bool = True) -> dict[str, Any]:
    return {
        "battleType": "legend",
        "attack": attack,
        "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"),
        "opponentPlayerTag": opponent,
        "stars": 0,
        "destructionPercentage": 49,
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
        self.during_profile: Callable[[], Awaitable[None]] | None = None
        self.fetched: list[str] = []

    def profile(self, tag: str = TAG) -> dict[str, Any]:
        return self.profiles.setdefault(
            tag, {"tag": tag, "trophies": 5_000, "attackWins": 10, "defenseWins": 7}
        )

    async def fetch_player(
        self, _pool: KeyPool, tag: str, endpoint: str
    ) -> FetchedResponse:
        self.fetched.append(endpoint)
        if endpoint == "profile" and self.during_profile is not None:
            during, self.during_profile = self.during_profile, None
            await during()
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

    return SimpleNamespace(
        check=check, settle=settle, client=client, clock=clock, collector=collector
    )


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


def test_defense_won_fetches_the_log_even_when_win_counts_stay_zero(
    game: SimpleNamespace,
) -> None:
    profile = game.client.profile()
    profile.update(attackWins=0, defenseWins=0)
    profile["achievements"] = [
        {"name": "Unbreakable", "village": "home", "value": 1767},
        {"name": "Conqueror", "village": "home", "value": 12216},
    ]
    game.settle()
    # A 0-star 30% defense: no trophies lost, one more defense won.
    profile["achievements"][0]["value"] += 1

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
    [
        "transport",
        "server_error",
        "missing_trophies",
        "malformed_trophies",
        "negative_trophies",
        "boolean_defense_wins",
    ],
)
def test_unreadable_profile_fetches_the_log(
    game: SimpleNamespace, failure: str
) -> None:
    game.settle()
    profile = game.client.profile()
    if failure == "transport":
        game.client.profile_fails = True
    elif failure == "server_error":
        game.client.profile_status = 503
    elif failure == "boolean_defense_wins":
        profile["defenseWins"] = True
    else:
        profile["trophies"] = {
            "missing_trophies": None,
            "malformed_trophies": "5000",
            "negative_trophies": -1,
        }[failure]

    assert game.check() == BOTH


def test_refresh_during_a_check_does_not_hide_its_failed_profile(
    game: SimpleNamespace,
) -> None:
    game.settle()

    async def refresh_saves_an_unchanged_profile() -> None:
        game.client.profile_fails = False
        await game.collector.collect_player(
            CollectorWork(1, TAG, game.clock[0]),
            lane="interactive",
            endpoints=("profile",),
        )
        game.client.profile_fails = True

    game.client.during_profile = refresh_saves_an_unchanged_profile
    game.client.profile_fails = True

    # The Refresh's profile, then this check's failed profile and its log.
    assert game.check() == ["profile", "profile", "battle_log"]


@pytest.mark.parametrize(
    "bad_row",
    [
        {"battleTimestamp": "yesterday"},
        # Live battleTime is the battle's length, not its date.
        {"battleTimestamp": None, "battleTime": 180},
    ],
)
def test_malformed_log_neither_clears_owed_fetches_nor_stops_collection(
    game: SimpleNamespace, bad_row: dict[str, Any]
) -> None:
    game.settle()
    game.client.logs[TAG] = [_battle(OPPONENT, game.clock[0], attack=False) | bad_row]
    game.client.profile()["trophies"] -= 40

    assert [game.check() for _ in range(3)] == [BOTH] * 3

    game.client.logs[TAG] = [_battle(OPPONENT, game.clock[0], attack=False)]
    assert [game.check() for _ in range(3)] == [BOTH, BOTH, PROFILE]


@pytest.mark.parametrize(
    "bad_row",
    [
        # Live logs keep rows like this for days: no opponent, no battle.
        {"opponentPlayerTag": None, "battleTime": 0, "destructionPercentage": 0},
        {"stars": None},
    ],
)
def test_malformed_row_is_retried_once_not_on_every_check(
    game: SimpleNamespace, bad_row: dict[str, Any]
) -> None:
    game.settle()
    game.client.logs[TAG] = [_battle(OPPONENT, game.clock[0], attack=False) | bad_row]
    game.client.profile()["trophies"] -= 40

    # The change's fetch, the retry, then the follow-up past the API cache.
    assert [game.check() for _ in range(4)] == [BOTH, BOTH, BOTH, PROFILE]


def test_failed_log_fetch_keeps_the_obligation(game: SimpleNamespace) -> None:
    game.settle()
    game.client.profile()["trophies"] += 40
    game.client.battle_log_status = 503
    assert game.check() == BOTH

    game.client.battle_log_status = 200
    assert [game.check() for _ in range(3)] == [BOTH, BOTH, PROFILE]


def _at(clock: str) -> datetime:
    """A time on 2026-10-02, the day of the production run, in UTC."""
    hour, minute, second = map(int, clock.split(":"))
    return datetime(2026, 10, 2, hour, minute, second, tzinfo=UTC)


def test_zero_trophy_defense_of_battle_6964051_arrives_within_a_check(
    game: SimpleNamespace,
) -> None:
    """Production times and profiles. Both players tracked.

    The attacker's profile showed none of these attacks until 07:14, and both
    players' win counts stayed 0. Only the defender's count of defenses won
    moved. The defense was saved at 07:13:46, by the safety fetch.
    """
    attacker, defender, earlier_defender = "#2990QRJY9", "#P9YJR8QY", "#92C9G8YJ9"
    logs = game.client.logs

    def check(tag: str, clock: str) -> list[str]:
        return game.check(tag, after=_at(clock) - game.clock[0])

    def battle(opponent: str, attacked_at: str, defended_at: str) -> None:
        # The attacker's log times a battle about 161 s after the defender's.
        logs.setdefault(attacker, []).append(_battle(opponent, _at(attacked_at)))
        logs.setdefault(opponent, []).append(
            _battle(attacker, _at(defended_at), attack=False)
        )

    game.clock[0] = _at("06:40:00")
    for tag in (attacker, defender, earlier_defender):
        game.client.profile(tag).update(attackWins=0, defenseWins=0)
    defenses_won = {"name": "Unbreakable", "village": "home", "value": 1767}
    game.client.profile(defender)["achievements"] = [defenses_won]
    game.settle(attacker)
    game.settle(earlier_defender)
    # The defender's last log before the battle came at 06:56:29.
    assert [check(defender, "06:53:00"), check(defender, "06:56:28")] == [BOTH] * 2

    # 3 stars on another tracked player, whose own profile shows the loss.
    battle(earlier_defender, "06:56:43", "06:53:38")
    game.client.profile(earlier_defender)["trophies"] -= 40
    assert check(earlier_defender, "06:57:05") == BOTH
    assert check(attacker, "06:57:27") == BOTH
    assert check(defender, "06:59:11") == PROFILE

    # 0 stars, 30%: the attacker gains 3 trophies, the defender loses none.
    cached = list(logs[attacker])
    battle(defender, "06:59:55", "06:57:34")
    defenses_won["value"] += 1
    fresh, logs[attacker] = logs[attacker], cached
    # 11 seconds after the battle the API still serves the attacker's old log.
    assert check(attacker, "07:00:05") == BOTH
    logs[attacker] = fresh

    # Production skipped both logs on these checks and the next five.
    assert check(defender, "07:01:54") == BOTH
    assert check(attacker, "07:03:00") == BOTH
    assert check(defender, "07:05:12") == BOTH
    assert check(defender, "07:08:15") == PROFILE


def test_attackers_log_is_watched_for_ten_minutes_after_an_attack(
    game: SimpleNamespace,
) -> None:
    game.settle()
    game.client.logs[TAG] = [_battle(UNTRACKED, game.clock[0])]
    game.client.profile()["trophies"] += 40

    # Checks start 94 seconds apart; the seventh is 10 minutes after the attack.
    assert [game.check() for _ in range(9)] == [BOTH] * 7 + [PROFILE] * 2


def test_defender_with_the_battle_is_not_refetched_for_each_attacker_log(
    game: SimpleNamespace,
) -> None:
    game.settle(TAG)
    game.settle(OPPONENT)
    defended_at = game.clock[0]
    game.client.logs[OPPONENT] = [_battle(TAG, defended_at, attack=False)]
    # The attacker's log times the same battle 161 seconds later.
    game.client.logs[TAG] = [_battle(OPPONENT, defended_at + timedelta(seconds=161))]
    game.client.profile(OPPONENT)["trophies"] -= 40
    game.client.profile(TAG)["trophies"] += 40
    assert [game.check(OPPONENT) for _ in range(3)] == [BOTH, BOTH, PROFILE]

    # Each of the attacker's later checks fetches its log with that battle again.
    assert [game.check(TAG) for _ in range(5)] == [BOTH] * 5
    assert game.check(OPPONENT) == PROFILE


def test_opponent_whose_newest_battle_is_another_one_is_refetched() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    defended_at = now - timedelta(minutes=3)
    # The opponent's log shows only a defense two minutes before this battle.
    state.log(
        OPPONENT,
        now,
        _battle(UNTRACKED, defended_at - timedelta(minutes=2), attack=False),
    )
    state.log(
        TAG,
        now + timedelta(seconds=30),
        _battle(OPPONENT, defended_at + timedelta(seconds=161)),
    )

    assert state.due(OPPONENT, now + timedelta(seconds=92))


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
        body = {"trophies": trophies, "attackWins": 10, "defenseWins": 7}
        assert self.schedule.note_response(
            tag, "profile", json.dumps(body).encode(), started_at=at, completed_at=at
        )

    def log(self, tag: str, started_at: datetime, *entries: dict[str, Any]) -> bool:
        return self.schedule.note_response(
            tag,
            "battle_log",
            json.dumps({"items": list(entries)}).encode(),
            started_at=started_at,
            completed_at=started_at + timedelta(seconds=1),
        )

    def due(self, tag: str, at: datetime) -> bool:
        return self.schedule.due(tag, profile_usable=True, now=at)

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


def test_first_log_seen_after_a_restart_still_marks_opponents() -> None:
    state = _Schedule()
    now = state.settle(OPPONENT)
    state.profile(TAG, now)
    state.log(TAG, now, _battle(OPPONENT, now - timedelta(seconds=30)))

    assert state.due(OPPONENT, now + timedelta(seconds=2))


def test_valid_battle_in_a_partly_malformed_log_still_marks_the_opponent() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    state.profile(TAG, now, trophies=5_040)
    valid = _battle(OPPONENT, now - timedelta(seconds=20))
    malformed = _battle(OPPONENT, now - timedelta(minutes=5)) | {"stars": None}

    assert not state.log(TAG, now, valid, malformed)
    later = now + timedelta(minutes=2)
    assert state.due(OPPONENT, later)
    # The malformed log does not count as this player's owed fetch.
    assert state.due(TAG, later)


def test_corrected_older_row_still_marks_the_opponent() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    older = _battle(OPPONENT, now - timedelta(seconds=40))
    newer = _battle(UNTRACKED, now - timedelta(seconds=20))
    state.log(TAG, now, newer, older | {"stars": None})
    assert not state.due(OPPONENT, now + timedelta(seconds=2))

    state.log(TAG, now + timedelta(seconds=90), newer, older)
    assert state.due(OPPONENT, now + timedelta(seconds=92))


def test_row_with_an_unusable_date_is_skipped_without_raising() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    ancient = _battle(OPPONENT, now) | {"battleTimestamp": "00010101T000000.000Z"}
    valid = _battle(OPPONENT, now - timedelta(seconds=20))

    assert not state.log(TAG, now, ancient, valid)
    assert state.due(OPPONENT, now + timedelta(seconds=2))


def test_timestamp_without_fractional_seconds_counts() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    battle_at = now - timedelta(seconds=20)
    whole_seconds = {"battleTimestamp": battle_at.strftime("%Y%m%dT%H%M%SZ")}

    assert state.log(TAG, now, _battle(OPPONENT, battle_at) | whole_seconds)
    assert state.due(OPPONENT, now + timedelta(seconds=2))


def test_opponent_log_from_inside_the_api_cache_does_not_count() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    battle_at = now - timedelta(seconds=20)

    state.log(TAG, now, _battle(OPPONENT, battle_at))
    # The opponent's cached log, saved just before the battle, comes back again.
    state.log(OPPONENT, now + timedelta(seconds=10))
    assert state.due(OPPONENT, now + timedelta(seconds=12))

    state.log(
        OPPONENT, now + timedelta(seconds=61), _battle(TAG, battle_at, attack=False)
    )
    assert not state.due(OPPONENT, now + timedelta(seconds=63))


def test_opponent_mark_during_a_profile_change_outlasts_its_follow_up() -> None:
    state = _Schedule()
    now = state.settle(TAG)
    state.settle(OPPONENT)
    state.profile(OPPONENT, now, trophies=5_040)
    # The opponent's owed log request is already running when the mark arrives.
    started = now + timedelta(seconds=1)
    state.log(TAG, now + timedelta(seconds=30), _battle(OPPONENT, now))
    state.log(OPPONENT, started)
    state.log(OPPONENT, started + timedelta(seconds=60))

    assert state.due(OPPONENT, started + timedelta(seconds=62))

    state.log(OPPONENT, now + timedelta(seconds=91), _battle(TAG, now, attack=False))
    assert not state.due(OPPONENT, now + timedelta(seconds=93))
