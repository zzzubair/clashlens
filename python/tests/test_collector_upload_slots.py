from __future__ import annotations

import asyncio
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
