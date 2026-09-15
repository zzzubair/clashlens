from __future__ import annotations

import asyncio
import errno
import threading
from datetime import UTC, datetime
from typing import Any

import pytest
from test_collector import _Client, _collector, _Reservation, _Spool, _Store

from clashlens.collector_db import CollectorWork


def test_same_endpoint_waits_for_predecessor_handoff_ack() -> None:
    class BlockingStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def record_response(self, handoff: Any) -> object:
            if not self.handoffs:
                self.entered.set()
                assert self.release.wait(timeout=2)
            return super().record_response(handoff)

    async def scenario() -> None:
        spool = _Spool()
        store = BlockingStore(spool)
        client = _Client(spool)
        collector = _collector(spool, store, client)
        work = CollectorWork(1, "#2PP", datetime.now(UTC))
        first = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        assert await asyncio.to_thread(store.entered.wait, 1)
        second = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        while client.fetch_count < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0.1)
        assert len(spool.handoffs) == 1
        store.release.set()
        assert await first == ["recorded"]
        assert await second == ["recorded"]
        assert spool.handoffs == {}

    asyncio.run(scenario())


def test_post_publish_failure_fences_a_waiting_successor() -> None:
    class FailedStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def record_response(self, handoff: Any) -> object:
            self.entered.set()
            assert self.release.wait(timeout=2)
            raise RuntimeError("database outcome unknown")

    async def scenario() -> None:
        spool = _Spool()
        store = FailedStore(spool)
        client = _Client(spool)
        collector = _collector(spool, store, client)
        work = CollectorWork(1, "#2PP", datetime.now(UTC))
        first = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        assert await asyncio.to_thread(store.entered.wait, 1)
        second = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        while client.fetch_count < 2:
            await asyncio.sleep(0)
        store.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert results[1] == ["capacity_paused"]
        assert len(spool.handoffs) == 1
        assert await collector.health_response("/readyz") == (
            503,
            "text/plain",
            b"handoff_recovery_required\n",
        )

    asyncio.run(scenario())


def test_player_failure_drains_sibling_publication_before_reservation_close() -> None:
    class TrackedReservation(_Reservation):
        closed = False

        def __exit__(self, *_args: object) -> None:
            self.closed = True

    class BlockingSpool(_Spool):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.reservations: list[TrackedReservation] = []

        def reserve(self, _limit: int) -> TrackedReservation:
            reservation = TrackedReservation(self)
            self.reservations.append(reservation)
            return reservation

        def publish_handoff(self, *args: Any, **kwargs: Any) -> None:
            if args[0] == b"battle_log":
                self.entered.set()
                assert self.release.wait(timeout=2)
                assert not any(item.closed for item in self.reservations)
            super().publish_handoff(*args, **kwargs)

    class FailedStore(_Store):
        def record_response(self, handoff: Any) -> object:
            if handoff.endpoint == "profile":
                assert spool.entered.wait(timeout=2)
                raise RuntimeError("database unavailable")
            return super().record_response(handoff)

    async def scenario() -> None:
        collector = _collector(spool, FailedStore(spool), _Client(spool))
        task = asyncio.create_task(
            collector.collect_player(
                CollectorWork(1, "#2PP", datetime.now(UTC)), lane="ordinary"
            )
        )
        assert await asyncio.to_thread(spool.entered.wait, 1)
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not any(item.closed for item in spool.reservations)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert not any(item.closed for item in spool.reservations)
        spool.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(item.closed for item in spool.reservations)

    spool = BlockingSpool()
    asyncio.run(scenario())


def test_capacity_failure_before_sidecar_keeps_automatic_recovery() -> None:
    class FullAtPublishSpool(_Spool):
        failed = False

        def publish_handoff(self, *args: Any, **kwargs: Any) -> None:
            if not self.failed:
                self.failed = True
                raise OSError(errno.ENOSPC, "spool full")
            super().publish_handoff(*args, **kwargs)

    spool = FullAtPublishSpool()
    client = _Client(spool)
    collector = _collector(spool, _Store(spool), client)
    work = CollectorWork(1, "#2PP", datetime.now(UTC))

    assert asyncio.run(
        collector.collect_player(work, lane="ordinary", endpoints=("profile",))
    ) == ["capacity_paused"]
    assert collector._handoff_recovery_required is False
    assert collector._spool_capacity_failed is True
    collector._spool_probe_after = 0.0
    assert asyncio.run(
        collector.collect_player(work, lane="ordinary", endpoints=("profile",))
    ) == ["recorded"]
    assert client.fetch_count == 2


def test_cancelled_reservation_acquire_closes_late_result() -> None:
    class TrackedReservation(_Reservation):
        closed = False

        def __exit__(self, *_args: object) -> None:
            self.closed = True

    class BlockingReserveSpool(_Spool):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.reservation = TrackedReservation(self)

        def reserve(self, _limit: int) -> TrackedReservation:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return self.reservation

    async def scenario() -> None:
        spool = BlockingReserveSpool()
        collector = _collector(spool, _Store(spool), _Client(spool))
        task = asyncio.create_task(
            collector.collect_player(
                CollectorWork(1, "#2PP", datetime.now(UTC)),
                lane="ordinary",
                endpoints=("profile",),
            )
        )
        assert await asyncio.to_thread(spool.entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        assert spool.reservation.closed is False
        spool.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spool.reservation.closed is True

    asyncio.run(scenario())


def test_regular_stop_drains_sibling_then_reports_child_failure() -> None:
    async def scenario() -> None:
        spool = _Spool()
        store = _Store(spool)
        collector = _collector(spool, store, _Client(spool))
        works = [
            CollectorWork(1, "#2PP", datetime.now(UTC)),
            CollectorWork(2, "#8VV", datetime.now(UTC)),
        ]
        claimed = False
        first_started = asyncio.Event()
        sibling_started = asyncio.Event()
        fail = asyncio.Event()
        finish_sibling = asyncio.Event()

        def claim_due_players(**_kwargs: Any) -> list[CollectorWork]:
            nonlocal claimed
            if claimed:
                return []
            claimed = True
            return works

        async def collect_player(
            work: CollectorWork, **_kwargs: Any
        ) -> list[str]:
            if work.player_id == 1:
                first_started.set()
                await fail.wait()
                raise RuntimeError("handoff failed during shutdown")
            sibling_started.set()
            await finish_sibling.wait()
            return ["recorded"]

        store.claim_due_players = claim_due_players  # type: ignore[attr-defined]
        collector.collect_player = collect_player  # type: ignore[method-assign]
        stop = asyncio.Event()
        loop = asyncio.create_task(collector._regular_loop(stop, 0.001))
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(sibling_started.wait(), 1)
        stop.set()
        fail.set()
        await asyncio.sleep(0.02)
        assert not loop.done()
        finish_sibling.set()
        with pytest.raises(RuntimeError, match="handoff failed during shutdown"):
            await loop

    asyncio.run(scenario())
