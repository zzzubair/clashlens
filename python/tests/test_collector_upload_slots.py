from __future__ import annotations

import asyncio
import errno
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import psycopg
import pytest
from test_collector import _Client, _collector, _Spool, _Store

import clashlens.collector as collector_module
from clashlens.collector_db import CollectorWork
from clashlens.collector_uploads import UploadClaim


def test_uploads_use_at_most_four_database_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    now = datetime.now(UTC)
    claims = [
        UploadClaim(f"{index:064x}", str(index), 4, "uploader", str(index), now, 1)
        for index in range(48)
    ]
    lock = threading.Lock()
    active = {"database": 0, "archive": 0}
    peak = {"database": 0, "archive": 0}
    completed = 0
    stop = asyncio.Event()

    def busy(kind: str, seconds: float) -> None:
        with lock:
            active[kind] += 1
            peak[kind] = max(peak[kind], active[kind])
        time.sleep(seconds)
        with lock:
            active[kind] -= 1

    def claim_upload(_database: object, **_kwargs: object) -> UploadClaim | None:
        busy("database", 0.01)
        with lock:
            return claims.pop() if claims else None

    def complete_upload(*_args: object, **_kwargs: object) -> None:
        nonlocal completed
        busy("database", 0.01)
        with lock:
            completed += 1
            if completed == 48:
                stop.set()

    monkeypatch.setattr(
        collector_module.collector_uploads, "claim_upload", claim_upload
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "renew_upload",
        lambda *_args, **_kwargs: busy("database", 0.01),
    )
    monkeypatch.setattr(
        collector_module.collector_uploads, "complete_upload", complete_upload
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @staticmethod
        def write_immutable(
            _body: bytes, digest: str, *, generation: str | None = None
        ) -> str:
            busy("archive", 0.1)
            return f"archive/{digest}"

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=96)
        )
        await asyncio.wait_for(collector._upload_loop(stop, 0.01), timeout=10)

    asyncio.run(scenario())

    assert completed == 48
    assert peak["database"] <= 4
    # Archive writes still overlap beyond the database limit.
    assert peak["archive"] > 4


def test_spool_failure_keeps_database_slots_until_cancelled_renewals_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    lock = threading.Lock()
    renewals_started = threading.Event()
    release_renewals = threading.Event()
    fail_spool_read = threading.Event()
    queued_claim_started = threading.Event()
    active = 0
    peak = 0
    now = datetime.now(UTC)

    def claim_upload(
        _database: object, *, owner: str, **_kwargs: object
    ) -> UploadClaim | None:
        nonlocal peak
        with lock:
            peak = max(peak, active + 1)
        if owner == "queued":
            queued_claim_started.set()
            return None
        return UploadClaim("a" * 64, "one", 4, owner, owner, now, 1)

    def renew_upload(*_args: object, **_kwargs: object) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                renewals_started.set()
        try:
            assert release_renewals.wait(timeout=5)
        finally:
            with lock:
                active -= 1

    def verify(_digest: str, _size: int) -> bytes:
        assert fail_spool_read.wait(timeout=5)
        raise OSError(errno.EIO, "spool unavailable")

    spool.verify = verify  # type: ignore[method-assign]
    monkeypatch.setattr(collector_module, "_UPLOAD_RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        collector_module.collector_uploads, "claim_upload", claim_upload
    )
    monkeypatch.setattr(
        collector_module.collector_uploads, "renew_upload", renew_upload
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

    collector = _collector(spool, _Store(spool), _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=96)
        )
        uploads = [
            asyncio.create_task(collector.upload_once(owner=str(index)))
            for index in range(4)
        ]
        queued = None
        try:
            assert await asyncio.to_thread(renewals_started.wait, 2)
            fail_spool_read.set()
            queued = asyncio.create_task(collector.upload_once(owner="queued"))
            await asyncio.sleep(0.05)
            assert not queued_claim_started.is_set()
            assert all(not task.done() for task in uploads)
        finally:
            fail_spool_read.set()
            release_renewals.set()
            results = await asyncio.gather(*uploads, return_exceptions=True)
            if queued is not None:
                queued_result = await asyncio.gather(queued, return_exceptions=True)
        assert results == [True] * 4
        assert queued_result == [False]

    asyncio.run(scenario())

    assert collector.outcomes["spool_io_failure"] == 4
    assert queued_claim_started.is_set()
    assert peak == 4


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
    held = threading.Event()
    held.set()
    record = store.record_response

    def locked(handoff: object) -> object:
        if held.is_set():
            raise psycopg.errors.LockNotAvailable("the worker holds the player")
        return record(handoff)

    store.record_response = locked  # type: ignore[method-assign]
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
