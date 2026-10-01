from __future__ import annotations

import asyncio
import errno
import threading
import time
from datetime import UTC, datetime

import pytest
from test_collector import _Client, _collector, _Spool, _Store

import clashlens.collector as collector_module
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

    asyncio.run(asyncio.wait_for(collector._upload_loop(stop, 0.01), timeout=10))

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
