from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from test_collector import _Client, _collector, _Spool, _Store

import clashlens.collector as collector_module
from clashlens import collector_commits
from clashlens.collector_db import CollectorIntent, CollectorWork, ResponseHandoff
from clashlens.collector_http import FetchedResponse


@pytest.mark.parametrize(
    ("parallelism", "first_claims"),
    [(12, [(3, False), (9, None)]), (2, [(1, False), (1, None)]), (1, [(1, False)])],
)
def test_regular_checks_in_flight_follow_the_configured_parallelism(
    parallelism: int, first_claims: list[tuple[int, bool | None]]
) -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    collector.regular_parallelism = parallelism
    claims: list[tuple[int, bool | None]] = []
    in_flight = 0
    peak = 0
    stop = asyncio.Event()

    def claim_due_players(
        limit: int, now: datetime, first_battle_pending: bool | None = None
    ) -> list[CollectorWork]:
        claims.append((limit, first_battle_pending))
        return [CollectorWork(index, f"#P{index}", now) for index in range(limit)]

    async def collect_player(_work: CollectorWork, *, lane: str) -> list[str]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if peak == parallelism:
            stop.set()
        await stop.wait()
        in_flight -= 1
        return ["recorded"]

    collector.database.regular_admission_open = lambda _now: True  # type: ignore[attr-defined]
    collector.database.claim_due_players = claim_due_players  # type: ignore[attr-defined]
    collector.collect_player = collect_player  # type: ignore[method-assign]

    asyncio.run(collector._regular_loop(stop, 0.01))

    assert peak == parallelism
    # A quarter of the slots, and always at least one, go to overdue revisits first.
    assert claims[: len(first_claims)] == first_claims


def test_saves_a_held_lock_blocks_land_later_in_order_without_holding_checks() -> None:
    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, _Client(spool))
    held = _held_saves(store)
    work = CollectorWork(1, "#2PP", datetime.now(UTC))

    async def scenario() -> None:
        for _ in range(2):
            checked = collector.collect_player(work, lane="ordinary", endpoints=("profile",))
            assert await asyncio.wait_for(checked, 1) == ["recorded"]
        saved = list(spool.handoffs)
        assert len(saved) == 2
        assert store.handoffs == []
        held.clear()
        await asyncio.wait_for(asyncio.gather(*collector._later_commits), 5)
        assert [handoff.occurrence_key for handoff in store.handoffs] == saved

    asyncio.run(scenario())

    assert spool.handoffs == {}
    assert collector.outcomes["commit_deferred"] == 2
    assert not collector._handoff_recovery_required


def _held_saves(store: _Store) -> threading.Event:
    held = threading.Event()
    held.set()
    record = store.record_response

    def locked(handoff: object) -> object:
        if held.is_set():
            raise psycopg.errors.LockNotAvailable("the worker holds the player")
        return record(handoff)

    store.record_response = locked  # type: ignore[method-assign]
    return held


def test_work_waits_for_its_held_save_instead_of_fetching_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector_commits, "_RETRY_SECONDS", 0.01)
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    held = _held_saves(store)
    completions: list[int] = []

    def complete_intent(work_id: int) -> bool:
        completions.append(work_id)
        if len(completions) == 1:
            raise psycopg.errors.LockNotAvailable("the worker holds the work")
        return bool(store.handoffs)

    store.complete_intent = complete_intent  # type: ignore[method-assign]
    intent = CollectorIntent(
        "live_refresh", datetime.now(UTC), 1, "#2PP", work_id=7, battle_log_required=False
    )

    async def scenario() -> None:
        refresh = asyncio.create_task(collector.collect_intent(intent))
        await asyncio.sleep(0.2)
        assert not refresh.done()
        assert completions == []
        held.clear()
        assert await asyncio.wait_for(refresh, 5) == "complete"

    asyncio.run(scenario())

    assert client.fetch_count == 1
    assert completions == [7, 7]
    assert not collector._handoff_recovery_required


def test_restart_recovery_leaves_held_saves_to_commit_later_and_holds_their_work() -> None:
    spool = _Spool()
    now = datetime.now(UTC)
    refresh = CollectorIntent(
        "live_refresh", now, 1, "#2PP", work_id=7, battle_log_required=False
    )

    class WorkStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.completed: list[int] = []

        def pending_intents(
            self, limit: int, now: datetime, *, interactive: bool, held: list[int]
        ) -> list[CollectorIntent]:
            # As the database query does, held work is never picked.
            due = interactive and 7 not in self.completed and 7 not in held
            return [refresh] if due else []

        def complete_intent(self, work_id: int) -> bool:
            self.completed.append(work_id)
            return True

        @staticmethod
        def begin_reset(_boundary: datetime, *, local_regular_inflight: int) -> None:
            return None

        @staticmethod
        def expire_settlement_checks(_now: datetime) -> int:
            return 0

    store = WorkStore(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    held = _held_saves(store)
    body = b"profile"
    first = collector._make_handoff(
        CollectorWork(1, "#2PP", now, collector_work_id=7),
        FetchedResponse("profile", body, 200, now, now, "regular-1", {}),
        hashlib.sha256(body).hexdigest(),
    )
    second = replace(
        first, occurrence_key="second", response_completed_at=now + timedelta(seconds=1)
    )
    for handoff in (second, first):
        name, payload = collector.serialize_handoff(handoff)
        spool.handoffs[name] = payload

    assert collector.recover_handoffs() == 0

    async def scenario() -> None:
        collector_commits.commit_unrecovered(collector, collector._unrecovered)
        stop = asyncio.Event()
        intents = asyncio.create_task(collector._intent_loop(stop, False, 0.01))
        try:
            await asyncio.sleep(0.2)
            # The Refresh's saved responses are pending, so it is not fetched again.
            assert collector.held_work() == [7, 7]
            assert store.handoffs == []
            assert client.fetch_count == 0
            held.clear()
            await asyncio.wait_for(asyncio.gather(*collector._later_commits), 5)
            while not store.completed:
                await asyncio.sleep(0.01)
        finally:
            stop.set()
            await intents

    asyncio.run(scenario())

    assert [handoff.occurrence_key for handoff in store.handoffs[:2]] == [
        first.occurrence_key,
        "second",
    ]
    # Once they landed, the Refresh was picked again and ran as usual.
    assert client.fetch_count == 1
    assert store.completed == [7]
    assert spool.handoffs == {}
    assert not collector._handoff_recovery_required


def test_work_behind_a_save_that_failed_is_not_reported_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector_commits, "_RETRY_SECONDS", 0.01)
    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, _Client(spool))
    broken = threading.Event()

    def record(_handoff: object) -> object:
        if broken.is_set():
            raise psycopg.OperationalError("the database went away")
        raise psycopg.errors.LockNotAvailable("the worker holds the player")

    store.record_response = record  # type: ignore[method-assign]
    now = datetime.now(UTC)

    async def scenario() -> None:
        regular = CollectorWork(1, "#2PP", now)
        check = collector.collect_player(regular, lane="ordinary", endpoints=("profile",))
        assert await check == ["recorded"]
        refresh = asyncio.create_task(
            collector.collect_player(
                CollectorWork(1, "#2PP", now, collector_work_id=7),
                lane="interactive",
                endpoints=("profile",),
            )
        )
        await asyncio.sleep(0.1)
        assert not refresh.done()
        broken.set()
        # The Refresh's save waited behind the regular one, which failed.
        assert await asyncio.wait_for(refresh, 5) == ["capacity_paused"]

    asyncio.run(scenario())

    assert collector._handoff_recovery_required
    assert store.handoffs == []
    assert len(spool.handoffs) == 2


@pytest.mark.parametrize("held", ["save", "completion"])
def test_stopping_ends_lock_retries_and_keeps_the_saved_response(held: str) -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    if held == "save":
        _held_saves(store)
    else:

        def complete_intent(_work_id: int) -> bool:
            raise psycopg.errors.LockNotAvailable("the worker holds the work")

        store.complete_intent = complete_intent  # type: ignore[method-assign]
    intent = CollectorIntent(
        "live_refresh", datetime.now(UTC), 1, "#2PP", work_id=7, battle_log_required=False
    )

    async def scenario() -> str:
        refresh = asyncio.create_task(collector.collect_intent(intent))
        await asyncio.sleep(0.3)
        assert not refresh.done()
        collector._stopping.set()
        return await asyncio.wait_for(refresh, 5)

    outcome = asyncio.run(scenario())

    if held == "save":
        assert outcome == "capacity_paused"
        assert len(spool.handoffs) == 1
        assert store.handoffs == []
    else:
        assert outcome == "incomplete"
    assert client.fetch_count == 1


def test_a_held_player_never_delays_another_players_reset_or_regular_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #2P0P and #2P2G share one disk-write lock. A worker holds #2P0P from
    # before 05:00 until after the Reset pair for #2P2G must finish.
    reset_at = datetime(2026, 10, 5, 5, tzinfo=UTC)

    class Clock(datetime):
        current = reset_at - timedelta(seconds=2)

        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return cls.current

    monkeypatch.setattr(collector_module, "datetime", Clock)
    reset_pair = CollectorIntent(
        "reset_baseline", reset_at, 2, "#2P2G", work_id=7, sweep_id=1
    )

    class ResetStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.sweeps: list[datetime] = []
            self.completed: list[int] = []
            self.regular = ["#2P0P", "#2Q2G", "#2Q8G"]

        def record_response(self, handoff: ResponseHandoff) -> object:
            if handoff.identity_key == "#2P0P":
                raise psycopg.errors.LockNotAvailable("the worker holds #2P0P")
            return super().record_response(handoff)

        def claim_due_players(
            self, limit: int, now: datetime, first_battle_pending: bool | None = None
        ) -> list[CollectorWork]:
            # As in the database, an unfinished Reset pair holds regular checks.
            if (now >= reset_at and 7 not in self.completed) or not self.regular:
                return []
            if now < reset_at and self.regular[0] != "#2P0P":
                return []
            tag = self.regular.pop(0)
            return [CollectorWork(len(self.regular) + 10, tag, now)]

        def begin_reset(self, boundary: datetime, *, local_regular_inflight: int) -> int:
            self.sweeps.append(boundary)
            return 1

        @staticmethod
        def expire_settlement_checks(_now: datetime) -> int:
            return 0

        def pending_intents(
            self, limit: int, now: datetime, *, interactive: bool, held: list[int]
        ) -> list[CollectorIntent]:
            due = not interactive and self.sweeps and 7 not in self.completed
            return [reset_pair] if due else []

        def complete_intent(self, work_id: int) -> bool:
            self.completed.append(work_id)
            return True

    class Client(_Client):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.tags: list[str] = []

        async def fetch_player(self, pool: object, tag: str, endpoint: str) -> FetchedResponse:
            self.tags.append(tag)
            return await super().fetch_player(pool, tag, endpoint)  # type: ignore[arg-type]

    spool = _Spool()
    store = ResetStore(spool)
    client = Client(spool)
    collector = _collector(spool, store, client)
    now = datetime.now(UTC)
    assert collector._handoff_lock(
        _handoff_for(collector, "#2P0P", now)
    ) is collector._handoff_lock(_handoff_for(collector, "#2P2G", now))

    async def scenario() -> None:
        stop = asyncio.Event()
        loops = [
            asyncio.create_task(collector._regular_loop(stop, 0.01)),
            asyncio.create_task(collector._intent_loop(stop, False, 0.01)),
        ]
        try:
            while not collector._later_commits or collector.regular_inflight:
                await asyncio.sleep(0.01)
            assert store.sweeps == []
            Clock.current = reset_at + timedelta(seconds=1)
            while "#2Q8G" not in client.tags:
                await asyncio.sleep(0.01)
                assert time.monotonic() - started < 5
            # #2P0P's answer is still waiting for the worker.
            assert collector._later_commits
            assert "#2P0P" not in {h.identity_key for h in store.handoffs}
        finally:
            waiting = list(collector._later_commits)
            stop.set()
            collector._stopping.set()
            await asyncio.gather(*loops)
            await asyncio.gather(*waiting, return_exceptions=True)

    started = time.monotonic()
    asyncio.run(scenario())

    assert store.sweeps == [reset_at]
    assert store.completed == [7]
    assert client.tags[0] == "#2P0P"
    assert client.tags.index("#2P2G") < client.tags.index("#2Q2G")


def _handoff_for(collector: collector_module.Collector, tag: str, now: datetime) -> ResponseHandoff:
    body = tag.encode()
    return collector._make_handoff(
        CollectorWork(1, tag, now),
        FetchedResponse("profile", body, 200, now, now, "regular-1", {}),
        hashlib.sha256(body).hexdigest(),
    )


def test_a_save_left_at_stop_keeps_later_saves_from_committing_first() -> None:
    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, _Client(spool))
    held = _held_saves(store)
    work = CollectorWork(1, "#2PP", datetime.now(UTC))

    async def scenario() -> None:
        first = collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        assert await first == ["recorded"]
        collector._stopping.set()
        await asyncio.wait(set(collector._later_commits))
        # The worker lets go during shutdown, and another answer arrives.
        held.clear()
        second = collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        assert await second == ["capacity_paused"]

    asyncio.run(scenario())

    assert collector._handoff_recovery_required
    assert store.handoffs == []
    assert len(spool.handoffs) == 1
