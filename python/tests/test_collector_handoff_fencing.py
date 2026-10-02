from __future__ import annotations

import asyncio
import errno
import hashlib
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from test_collector import _Client, _collector, _Reservation, _Spool, _Store

from clashlens.collector_db import CollectorDatabase, CollectorWork
from clashlens.collector_http import FetchedResponse, KeyPool
from clashlens.spool import Spool


@pytest.mark.parametrize(
    ("applied", "replayed"),
    [(set(), ["z-earlier", "a-later"]), ({"a-later"}, ["a-later", "z-earlier"])],
)
def test_recovery_replays_applied_then_received_order(
    tmp_path: Path, applied: set[str], replayed: list[str]
) -> None:
    class RecoveryStore:
        def __init__(self) -> None:
            self.replayed: list[str] = []

        def applied_occurrence_keys(self, keys: list[str]) -> set[str]:
            return applied & set(keys)

        def record_recovered_response(self, handoff: Any, *, serialized: bool) -> None:
            assert serialized
            self.replayed.append(handoff.occurrence_key)

        def referenced_spool_hashes(self) -> set[str]:
            return set()

    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    store = RecoveryStore()
    collector = _collector(spool, store, _Client(_Spool()))  # type: ignore[arg-type]
    work = CollectorWork(1, "#2PP", datetime.now(UTC))
    received = datetime.now(UTC)
    # File names sort opposite to the order the responses were received.
    for key, body, offset in (("z-earlier", b"5032", 0), ("a-later", b"5033", 1)):
        digest = hashlib.sha256(body).hexdigest()
        at = received + timedelta(seconds=offset)
        response = FetchedResponse("profile", body, 200, at, at, "regular-1", {})
        handoff = replace(
            collector._make_handoff(work, response, digest), occurrence_key=key
        )
        name, payload = collector.serialize_handoff(handoff)
        with spool.reserve(1024) as reservation:
            spool.publish_handoff(body, digest, name, payload, reservation)

    try:
        assert collector.recover_handoffs() == 2
        assert store.replayed == replayed
        assert spool.iter_handoffs() == []
    finally:
        spool.close()


def test_cleanup_batch_acknowledges_a_file_already_removed_by_a_crash(
    tmp_path: Path,
) -> None:
    digest = "a" * 64

    class CleanupStore:
        def __init__(self) -> None:
            self.marked: list[str] = []

        def deletable_hashes(self, **_kwargs: object) -> list[str]:
            return [digest]

        def delete_spool_if_deletable(self, candidates: list[str], delete: Any) -> int:
            deleted = [candidate for candidate in candidates if delete(candidate)]
            self.marked.extend(deleted)
            return len(deleted)

    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    store = CleanupStore()
    collector = _collector(spool, store, _Client(_Spool()))

    try:
        assert collector.cleanup_uploaded() == 1
        assert store.marked == [digest]
    finally:
        spool.close()


def test_normal_upload_shutdown_finishes_owned_upload_and_removes_its_raw_body(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    bodies = {
        hashlib.sha256(body).hexdigest(): body
        for body in (f"uploaded response {index}".encode() for index in range(17))
    }
    digests = set(bodies)
    for digest, body in bodies.items():
        with spool.reserve(1024) as reservation:
            reservation.publish(body, digest)

    class UploadStore:
        def __init__(self) -> None:
            self.uploaded: set[str] = set()
            self.local_deleted: set[str] = set()

        def deletable_hashes(self, *, limit: int) -> list[str]:
            return sorted(self.uploaded - self.local_deleted)[:limit]

        def delete_spool_if_deletable(self, candidates: list[str], delete: Any) -> int:
            deleted = {
                candidate
                for candidate in candidates
                if candidate in self.uploaded and delete(candidate)
            }
            self.local_deleted |= deleted
            return len(deleted)

        def referenced_spool_hashes(self) -> set[str]:
            return digests - self.local_deleted

    store = UploadStore()
    collector = _collector(spool, store, _Client(_Spool()))  # type: ignore[arg-type]
    remaining = list(digests)
    all_started = asyncio.Event()
    release = asyncio.Event()
    stop = asyncio.Event()

    async def upload_once(*, owner: str) -> bool:
        del owner
        if not remaining:
            await stop.wait()
            return False
        digest = remaining.pop()
        if not remaining:
            all_started.set()
        await release.wait()
        store.uploaded.add(digest)
        return True

    collector.upload_once = upload_once  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(collector._upload_loop(stop, 0.001))
        await asyncio.wait_for(all_started.wait(), 1)
        stop.set()
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.wait_for(task, 1)

    try:
        asyncio.run(scenario())
        assert store.uploaded == digests
        assert store.local_deleted == digests
        assert spool.final_hashes().isdisjoint(digests)
    finally:
        spool.close()


def test_upload_loop_cancellation_still_cancels_its_owner() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def upload_once(*, owner: str) -> bool:
        del owner
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    collector.upload_once = upload_once  # type: ignore[method-assign]

    async def scenario() -> None:
        task = asyncio.create_task(collector._upload_loop(asyncio.Event(), 0.001))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert cancelled.is_set()

    asyncio.run(scenario())


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
        # The successor is saved at once but commits only after its predecessor.
        assert len(spool.handoffs) == 2
        assert store.handoffs == []
        store.release.set()
        assert await first == ["recorded"]
        assert await second == ["recorded"]
        assert spool.handoffs == {}

    asyncio.run(scenario())


class _Profiles(_Client):
    def __init__(self, spool: _Spool, bodies: list[bytes]) -> None:
        super().__init__(spool)
        self.bodies = bodies

    async def fetch_player(
        self, pool: KeyPool, tag: str, endpoint: str
    ) -> FetchedResponse:
        response = await super().fetch_player(pool, tag, endpoint)
        if endpoint != "profile":
            return response
        return replace(response, body=self.bodies.pop(0))


def test_only_known_unchanged_responses_skip_the_spool() -> None:
    class CompactingStore(_Store):
        checks = 0

        def record_unchanged_response(self, _handoff: Any) -> bool:
            self.checks += 1
            return True

    spool = _Spool()
    store = CompactingStore(spool)
    collector = _collector(
        spool, store, _Profiles(spool, [b"a", b"b", b"b", b"b", b"b", b"b"])
    )
    work = CollectorWork(1, "#2PP", datetime.now(UTC))

    async def scenario() -> list[int]:
        checks = []
        for poll_work, lane, endpoints in (
            (work, "ordinary", ("profile",)),
            (work, "ordinary", ("profile",)),
            (work, "ordinary", ("profile",)),
            (replace(work, collector_work_id=7), "ordinary", ("profile",)),
            (work, "reset", ("profile", "battle_log")),
            (work, "ordinary", ("profile",)),
        ):
            await collector.collect_player(poll_work, lane=lane, endpoints=endpoints)
            checks.append(store.checks)
        return checks

    # First sight, changed, work-bound and reset responses never wait on the
    # database before reaching the spool; only repeats of committed fields do.
    assert asyncio.run(scenario()) == [0, 0, 1, 1, 1, 2]
    assert [handoff.endpoint for handoff in store.handoffs] == [
        "profile",
        "profile",
        "profile",
        "profile",
        "battle_log",
    ]
    assert spool.handoffs == {}


def test_refresh_is_saved_while_an_unchanged_check_waits() -> None:
    class SlowStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def record_unchanged_response(self, _handoff: Any) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=5)
            return True

    async def scenario() -> None:
        spool = _Spool()
        store = SlowStore(spool)
        collector = _collector(spool, store, _Profiles(spool, [b"a", b"a", b"b"]))
        work = CollectorWork(1, "#2PP", datetime.now(UTC))
        assert await collector.collect_player(
            work, lane="ordinary", endpoints=("profile",)
        ) == ["recorded"]
        unchanged = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        assert await asyncio.to_thread(store.entered.wait, 1)
        try:
            # The changed refresh must not queue behind the slow database check.
            assert await asyncio.wait_for(
                collector.collect_player(
                    work, lane="interactive", endpoints=("profile",)
                ),
                1,
            ) == ["recorded"]
        finally:
            store.release.set()
        assert await unchanged == ["recorded"]
        assert [handoff.response_hash for handoff in store.handoffs] == [
            hashlib.sha256(body).hexdigest() for body in (b"a", b"b")
        ]

    asyncio.run(scenario())


def test_refresh_survives_cancellation_while_a_fallback_waits_on_the_database() -> (
    None
):
    first, changed = b"a", b"b"

    class SlowFallbackStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def record_unchanged_response(self, _handoff: Any) -> bool:
            return False

        def record_response(self, handoff: Any) -> object:
            if self.handoffs and handoff.response_hash == self.handoffs[0].response_hash:
                self.entered.set()
                assert self.release.wait(timeout=5)
            return super().record_response(handoff)

    async def scenario() -> None:
        spool = _Spool()
        store = SlowFallbackStore(spool)
        collector = _collector(
            spool, store, _Profiles(spool, [first, first, changed])
        )
        work = CollectorWork(1, "#2PP", datetime.now(UTC))
        assert await collector.collect_player(
            work, lane="ordinary", endpoints=("profile",)
        ) == ["recorded"]
        fallback = asyncio.create_task(
            collector.collect_player(work, lane="ordinary", endpoints=("profile",))
        )
        assert await asyncio.to_thread(store.entered.wait, 1)
        refresh = asyncio.create_task(
            collector.collect_player(work, lane="interactive", endpoints=("profile",))
        )
        saved = f"publish:{hashlib.sha256(changed).hexdigest()}:b"
        try:
            async with asyncio.timeout(1):
                while saved not in spool.events:
                    await asyncio.sleep(0.001)
        finally:
            refresh.cancel()
            store.release.set()
        results = await asyncio.gather(fallback, refresh, return_exceptions=True)
        assert results[0] == ["recorded"]
        assert isinstance(results[1], asyncio.CancelledError)
        # The cancelled refresh stays saved for restart recovery.
        assert len(spool.handoffs) == 1
        assert await collector.health_response("/readyz") == (
            503,
            "text/plain",
            b"handoff_recovery_required\n",
        )

    asyncio.run(scenario())


def _poll_profile(collector: Any) -> Any:
    return collector.collect_player(
        CollectorWork(1, "#2PP", datetime.now(UTC)),
        lane="ordinary",
        endpoints=("profile",),
    )


def test_failed_unchanged_check_still_saves_the_response() -> None:
    class DownStore(_Store):
        checks = 0

        def record_unchanged_response(self, _handoff: Any) -> bool:
            self.checks += 1
            raise psycopg.OperationalError("database unavailable")

    spool = _Spool()
    store = DownStore(spool)
    collector = _collector(spool, store, _Client(spool))

    async def scenario() -> list[list[str]]:
        return [await _poll_profile(collector), await _poll_profile(collector)]

    assert asyncio.run(scenario()) == [["recorded"], ["recorded"]]
    assert store.checks == 1
    assert [handoff.endpoint for handoff in store.handoffs] == ["profile"] * 2
    assert spool.events[-3:] == ["handoff", "database", "ack"]


def test_cancelled_unchanged_check_still_saves_the_response() -> None:
    class SlowStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()
            self.release = threading.Event()

        def record_unchanged_response(self, _handoff: Any) -> bool:
            self.entered.set()
            assert self.release.wait(timeout=2)
            return False

    async def scenario() -> None:
        spool = _Spool()
        store = SlowStore(spool)
        collector = _collector(spool, store, _Client(spool))
        assert await _poll_profile(collector) == ["recorded"]
        task = asyncio.create_task(_poll_profile(collector))
        assert await asyncio.to_thread(store.entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0.01)
        store.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert [handoff.endpoint for handoff in store.handoffs] == ["profile"] * 2
        assert spool.events[-3:] == ["handoff", "database", "ack"]
        assert await collector.health_response("/readyz") != (
            503,
            "text/plain",
            b"handoff_recovery_required\n",
        )

    asyncio.run(scenario())


def test_unreachable_database_at_shutdown_still_saves_the_response() -> None:
    database = CollectorDatabase(
        "postgresql://clashlens@127.0.0.1:1/clashlens?connect_timeout=1"
    )

    class UnreachableStore(_Store):
        def __init__(self, spool: _Spool) -> None:
            super().__init__(spool)
            self.entered = threading.Event()

        def record_unchanged_response(self, handoff: Any) -> bool:
            self.entered.set()
            return database.record_unchanged_response(handoff)

    async def scenario() -> float:
        spool = _Spool()
        store = UnreachableStore(spool)
        collector = _collector(spool, store, _Client(spool))
        assert await _poll_profile(collector) == ["recorded"]
        task = asyncio.create_task(_poll_profile(collector))
        assert await asyncio.to_thread(store.entered.wait, 1)
        stopping = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
        assert [handoff.endpoint for handoff in store.handoffs] == ["profile"] * 2
        assert spool.events[-3:] == ["handoff", "database", "ack"]
        return time.monotonic() - stopping

    try:
        assert asyncio.run(scenario()) < 10
    finally:
        database.close()


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
        while len(spool.handoffs) < 2:
            await asyncio.sleep(0.001)
        store.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert isinstance(results[0], RuntimeError)
        assert results[1] == ["capacity_paused"]
        # Both saved responses are left for restart recovery.
        assert len(spool.handoffs) == 2
        assert await collector.health_response("/readyz") == (
            503,
            "text/plain",
            b"handoff_recovery_required\n",
        )

    asyncio.run(scenario())


def test_reset_failure_drains_sibling_publication_before_reservation_close() -> None:
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
                CollectorWork(1, "#2PP", datetime.now(UTC)), lane="reset"
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


def test_regular_admission_drains_work_if_the_next_tier_claim_fails() -> None:
    async def scenario() -> None:
        spool = _Spool()
        store = _Store(spool)
        collector = _collector(spool, store, _Client(spool))
        work = CollectorWork(1, "#2PP", datetime.now(UTC))
        started = threading.Event()
        drained = asyncio.Event()
        block = asyncio.Event()

        def claim_due_players(
            *, first_battle_pending: bool | None = None, **_kwargs: Any
        ) -> list[CollectorWork]:
            if first_battle_pending is False:
                return [work]
            if not started.wait(1):
                raise AssertionError("claimed work was not admitted")
            raise RuntimeError("later tier claim failed")

        async def collect_player(*_args: Any, **_kwargs: Any) -> list[str]:
            started.set()
            try:
                await block.wait()
            finally:
                drained.set()

        store.claim_due_players = claim_due_players  # type: ignore[attr-defined]
        collector.collect_player = collect_player  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="later tier claim failed"):
            await asyncio.wait_for(
                collector._regular_loop(asyncio.Event(), 0.001), 1
            )
        assert drained.is_set()

    asyncio.run(scenario())


def test_regular_admission_serves_repeats_and_borrows_an_empty_first_battle_tier(
) -> None:
    async def run_case(
        first_battles: list[CollectorWork],
        repeats: list[CollectorWork],
        expected_starts: int,
    ) -> list[CollectorWork]:
        spool = _Spool()
        store = _Store(spool)
        collector = _collector(spool, store, _Client(spool))
        queues = {True: first_battles, False: repeats}
        started: list[CollectorWork] = []
        enough_started = asyncio.Event()
        release = asyncio.Event()

        def claim_due_players(
            *, limit: int, first_battle_pending: bool | None = None, **_kwargs: Any
        ) -> list[CollectorWork]:
            if first_battle_pending is None:
                first_count = min(limit, len(queues[True]))
                repeat_count = min(limit - first_count, len(queues[False]))
                claimed = (
                    queues[True][:first_count] + queues[False][:repeat_count]
                )
                queues[True] = queues[True][first_count:]
                queues[False] = queues[False][repeat_count:]
                return claimed
            queue = queues[first_battle_pending]
            claimed, queues[first_battle_pending] = queue[:limit], queue[limit:]
            return claimed

        async def collect_player(
            work: CollectorWork, **_kwargs: Any
        ) -> list[str]:
            started.append(work)
            if len(started) >= expected_starts:
                enough_started.set()
            await release.wait()
            return ["recorded"]

        store.claim_due_players = claim_due_players  # type: ignore[attr-defined]
        collector.collect_player = collect_player  # type: ignore[method-assign]
        stop = asyncio.Event()
        loop = asyncio.create_task(collector._regular_loop(stop, 0.001))
        await asyncio.wait_for(enough_started.wait(), 1)
        stop.set()
        release.set()
        await loop
        return started

    async def scenario() -> None:
        now = datetime.now(UTC)
        first_battles = [
            CollectorWork(index, f"#F{index}", now, first_battle_pending=True)
            for index in range(1, 301)
        ]
        repeats = [
            CollectorWork(1000 + index, f"#R{index}", now)
            for index in range(1, 81)
        ]
        mixed = await run_case(first_battles, repeats, 256)
        assert sum(not work.first_battle_pending for work in mixed) == 64
        assert sum(work.first_battle_pending for work in mixed) == 192
        assert len({work.player_id for work in mixed}) == len(mixed)

        only_first_battles = first_battles[:270]
        first_borrowed = await run_case(
            only_first_battles, [], 256
        )
        assert first_borrowed == only_first_battles[:256]

        only_repeats = [
            CollectorWork(index, f"#R{index}", now) for index in range(1, 271)
        ]
        borrowed = await run_case([], only_repeats, 256)
        assert borrowed == only_repeats[:256]

    asyncio.run(scenario())
