"""Archive uploads, run in their own process beside the collector."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _FixtureS3Handler
from test_collector import _Client, _collector, _Spool, _Store

import clashlens.uploader as uploader_module
from clashlens import collector_liveness
from clashlens.archive import ArchiveReadError
from clashlens.collector_uploads import UploadClaim
from clashlens.uploader import Uploader, UploaderProcess


def _uploader(spool: _Spool, store: _Store, archive: object) -> Uploader:
    return Uploader(
        database=store, spool=spool, archive=archive, archive_instance_id="fixture"
    )


def test_one_bad_archive_object_does_not_stop_other_uploads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    now = datetime.now(UTC)
    claims = [
        UploadClaim("a" * 64, "one", 4, "uploader", "one", now, 1),
        UploadClaim("b" * 64, "two", 4, "uploader", "two", now, 1),
    ]
    failures: list[tuple[str, str, bool]] = []
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claims.pop(0),
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "fail_upload",
        lambda _database, claim, *, category, retryable, **_kwargs: failures.append(
            (claim.response_hash, category, retryable)
        ),
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @staticmethod
        def write_immutable(
            _body: bytes, _digest: str, *, generation: str | None = None
        ) -> str:
            raise ArchiveReadError(
                "archive_checksum_mismatch",
                "stored bytes differ",
                retryable=False,
            )

    uploader = _uploader(spool, store, Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert failures == [
        ("a" * 64, "archive_checksum_mismatch", False),
        ("b" * 64, "archive_checksum_mismatch", False),
    ]
    assert uploader.archive_health == "degraded"


def test_background_uploader_drains_multiple_objects_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    now = datetime.now(UTC)
    claims = [
        UploadClaim(character * 64, str(index), 4, "uploader", str(index), now, 1)
        for index, character in enumerate("abc", start=1)
    ]
    claims_lock = threading.Lock()
    completed = 0
    stop = asyncio.Event()

    def claim_upload(**_kwargs: object) -> UploadClaim | None:
        with claims_lock:
            return claims.pop(0) if claims else None

    def complete_upload(*_args: object, **_kwargs: object) -> None:
        nonlocal completed
        with claims_lock:
            completed += 1
            if completed == 3:
                stop.set()

    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **kwargs: claim_upload(**kwargs),
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "complete_upload",
        lambda _database, *args, **kwargs: complete_upload(*args, **kwargs),
    )

    class Archive:
        instance_config = None
        writes = 0
        lock = threading.Lock()
        concurrent_writes = threading.Barrier(2)

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @classmethod
        def write_immutable(
            cls, _body: bytes, digest: str, *, generation: str | None = None
        ) -> str:
            with cls.lock:
                cls.writes += 1
                write_number = cls.writes
            if write_number > 1:
                cls.concurrent_writes.wait(timeout=1)
            return f"archive/{digest}"

    uploader = _uploader(spool, store, Archive())

    asyncio.run(asyncio.wait_for(uploader.run(stop, 0.01), timeout=2))

    assert completed == 3


def test_background_uploader_surfaces_unexpected_failure_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    digest = "a" * 64
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    store.referenced.add(digest)
    claim = UploadClaim(
        digest,
        "sha256/aa/" + digest,
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
    )
    claim_lock = threading.Lock()

    def claim_upload(**_kwargs: object) -> UploadClaim | None:
        nonlocal claim
        with claim_lock:
            current, claim = claim, None  # type: ignore[assignment]
            return current

    def lose_lease(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("upload lease lost")

    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **kwargs: claim_upload(**kwargs),
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "complete_upload",
        lambda _database, *args, **kwargs: lose_lease(*args, **kwargs),
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @staticmethod
        def write_immutable(
            _body: bytes, response_hash: str, *, generation: str | None = None
        ) -> str:
            return f"s3://evidence/sha256/{response_hash[:2]}/{response_hash}"

    uploader = _uploader(spool, store, Archive())

    async def run_uploader() -> None:
        with pytest.raises(RuntimeError, match="upload lease lost"):
            await asyncio.wait_for(
                uploader.run(asyncio.Event(), 0.01), timeout=2
            )

    asyncio.run(run_uploader())
    assert store.referenced == {digest}


def test_slow_upload_renews_its_lease_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    claim = UploadClaim(
        "a" * 64,
        "one",
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
    )
    renewals = 0
    renewed_during_write = threading.Event()
    completed: list[str] = []

    monkeypatch.setattr(uploader_module, "RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def renew(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            renewed_during_write.set()

    monkeypatch.setattr(uploader_module.collector_uploads, "renew_upload", renew)
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "complete_upload",
        lambda _database, seen, **_kwargs: completed.append(seen.response_hash),
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
            assert renewed_during_write.wait(timeout=1)
            return f"archive/{digest}"

    uploader = _uploader(spool, store, Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    # Renewed in the background while the write ran. No renewal is added
    # before or after the write while most of the lease is left.
    assert renewals >= 2
    assert completed == ["a" * 64]


def test_lost_renewal_finishes_immutable_write_without_committing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    claim = UploadClaim(
        "a" * 64,
        "one",
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
    )
    renewals = 0
    lease_lost = threading.Event()
    completed: list[str] = []

    monkeypatch.setattr(uploader_module, "RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def renew(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            lease_lost.set()
            raise uploader_module.collector_uploads.UploadLeaseLost(
                "upload lease lost"
            )

    monkeypatch.setattr(uploader_module.collector_uploads, "renew_upload", renew)
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "complete_upload",
        lambda _database, seen, **_kwargs: completed.append(seen.response_hash),
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
            assert lease_lost.wait(timeout=1)
            return f"archive/{digest}"

    uploader = _uploader(spool, store, Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert uploader.outcomes["upload_lease_lost"] == 1
    assert completed == []


def test_archive_failure_after_lost_renewal_does_not_fail_stale_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    claim = UploadClaim(
        "a" * 64,
        "one",
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
    )
    lease_lost = threading.Event()
    failed: list[str] = []

    monkeypatch.setattr(uploader_module, "RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def lose_renewal(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        lease_lost.set()
        raise uploader_module.collector_uploads.UploadLeaseLost("upload lease lost")

    monkeypatch.setattr(
        uploader_module.collector_uploads, "renew_upload", lose_renewal
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "fail_upload",
        lambda _database, seen, **_kwargs: failed.append(seen.response_hash),
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            assert lease_lost.wait(timeout=1)
            return "degraded"

    uploader = _uploader(spool, store, Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert uploader.outcomes["upload_lease_lost"] == 1
    assert failed == []


def test_upload_spool_io_failure_pauses_and_preserves_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    digest = "a" * 64
    store = _Store(spool)
    store.referenced.add(digest)
    claim = UploadClaim(
        digest,
        "sha256/aa/" + digest,
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def failed_verify(_digest: str, _size: int) -> bytes:
        raise OSError(errno.EIO, "spool unavailable")

    spool.verify = failed_verify  # type: ignore[attr-defined]

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

    uploader = _uploader(spool, store, Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert uploader.spool_io_failed is True
    assert store.referenced == {digest}


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
        uploader_module.collector_uploads, "claim_upload", claim_upload
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "renew_upload",
        lambda *_args, **_kwargs: busy("database", 0.01),
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads, "complete_upload", complete_upload
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

    uploader = _uploader(spool, store, Archive())

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=96)
        )
        await asyncio.wait_for(uploader.run(stop, 0.01), timeout=10)

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
    monkeypatch.setattr(uploader_module, "RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        uploader_module.collector_uploads, "claim_upload", claim_upload
    )
    monkeypatch.setattr(
        uploader_module.collector_uploads, "renew_upload", renew_upload
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

    uploader = _uploader(spool, _Store(spool), Archive())

    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=96)
        )
        uploads = [
            asyncio.create_task(uploader.upload_once(owner=str(index)))
            for index in range(4)
        ]
        queued = None
        try:
            assert await asyncio.to_thread(renewals_started.wait, 2)
            fail_spool_read.set()
            queued = asyncio.create_task(uploader.upload_once(owner="queued"))
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

    assert uploader.outcomes["spool_io_failure"] == 4
    assert queued_claim_started.is_set()
    assert peak == 4


class _ReadyArchive:
    instance_config = None
    bucket = "evidence"

    @staticmethod
    def check_marker_health() -> str:
        return "ready"

    @staticmethod
    def write_immutable(
        _body: bytes, digest: str, *, generation: str | None = None
    ) -> str:
        return f"s3://evidence/sha256/{digest[:2]}/{digest}"


def _one_claim(monkeypatch: pytest.MonkeyPatch, claim: UploadClaim) -> list[str]:
    calls: list[str] = []
    claims = [claim]
    patches = {
        "release_expired_uploads": lambda *_args, **_kwargs: calls.append("release"),
        "claim_upload": lambda *_args, **_kwargs: (
            calls.append("claim") or (claims.pop() if claims else None)
        ),
        "renew_upload": lambda *_args, **_kwargs: calls.append("renew"),
        "archived_copy": lambda *_args, **_kwargs: None,
        "complete_upload": lambda _database, _claim, *, archive_reference, **_kwargs: (
            calls.append(f"complete:{archive_reference}")
        ),
        "fail_upload": lambda _database, _claim, *, category, retryable, **_kwargs: (
            calls.append(f"fail:{category}:{retryable}")
        ),
    }
    for name, replacement in patches.items():
        monkeypatch.setattr(uploader_module.collector_uploads, name, replacement)
    return calls


def test_an_upload_makes_two_database_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each upload used to make four: claiming, renewing before and after the
    # archive write, and completing. After the 8 October 2026 Reset each call
    # was slow and uploads fell to 145-300 a minute.
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    digest = "a" * 64
    claim = UploadClaim(digest, "one", 4, "uploader", "token", datetime.now(UTC), 1)
    calls = _one_claim(monkeypatch, claim)
    uploader = _uploader(spool, _Store(spool), _ReadyArchive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert asyncio.run(uploader.upload_once(owner="uploader")) is False

    reference = f"s3://evidence/sha256/aa/{digest}"
    # Expired leases are returned once each half lease, not per upload.
    assert calls == ["release", "claim", f"complete:{reference}", "claim"]


@pytest.mark.parametrize("generation", ["", "b" * 32])
def test_a_lost_saved_copy_already_archived_completes_without_writing(
    monkeypatch: pytest.MonkeyPatch, generation: str
) -> None:
    # A database restored to before this upload finished still lists it as
    # pending, though the bytes reached the archive and spool cleanup then
    # removed the saved copy.
    spool = _Spool()
    spool.verify = lambda _digest, _size: None  # type: ignore[attr-defined]
    digest = "a" * 64
    claim = UploadClaim(
        digest, "one", 4, "uploader", "token", datetime.now(UTC), 1, generation
    )
    calls = _one_claim(monkeypatch, claim)
    location = f"s3://evidence/sha256/aa/{digest}" + (
        f"/generation/{generation}" if generation else ""
    )
    reads: list[str] = []

    class Archive(_ReadyArchive):
        @staticmethod
        def read_verified(reference: str, expected_hash: str) -> object:
            reads.append(reference)
            assert expected_hash == digest
            return SimpleNamespace(body=b"body", reference=reference)

        @staticmethod
        def write_immutable(*_args: object, **_kwargs: object) -> str:
            raise AssertionError("the archive already holds these bytes")

    uploader = _uploader(spool, _Store(spool), Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert reads == [location]
    assert calls[-1] == f"complete:{location}"
    assert uploader.outcomes == {"archived_copy_found": 1, "uploaded": 1}


@pytest.mark.parametrize(
    ("archive_error", "unresolved_write", "outcome"),
    [
        (
            ArchiveReadError("archive_missing", "no such object", retryable=True),
            None,
            "fail:spool_missing:False",
        ),
        # The write of attempt 31 timed out and may still land.
        (
            ArchiveReadError("archive_missing", "no such object", retryable=True),
            31,
            "fail:archive_missing:True",
        ),
        (
            ArchiveReadError("archive_unavailable", "provider error", retryable=True),
            None,
            "fail:archive_unavailable:True",
        ),
    ],
    ids=["never-archived", "write-may-yet-land", "archive-unavailable"],
)
def test_a_lost_saved_copy_fails_for_good_only_when_the_archive_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
    archive_error: ArchiveReadError,
    unresolved_write: int | None,
    outcome: str,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: None  # type: ignore[attr-defined]
    claim = UploadClaim(
        "a" * 64,
        "one",
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        1,
        unresolved_write=unresolved_write,
    )
    calls = _one_claim(monkeypatch, claim)

    class Archive(_ReadyArchive):
        @staticmethod
        def read_verified(_reference: str, _expected_hash: str) -> object:
            raise archive_error

    uploader = _uploader(spool, _Store(spool), Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert calls[-1] == outcome


@pytest.mark.parametrize("generation", ["", "b" * 32])
@pytest.mark.parametrize("saved", [False, True])
def test_an_attempt_that_writes_nothing_keeps_counting_from_the_last_write(
    monkeypatch: pytest.MonkeyPatch, generation: str, saved: bool
) -> None:
    # Attempt 31's write may still land. Attempt 40 finds no saved copy and no
    # archived one, and names attempt 31 so the wait still counts from it; an
    # attempt that writes again starts the count afresh.
    digest = "a" * 64
    spool = _Spool()
    spool.verify = lambda _digest, _size: (  # type: ignore[attr-defined]
        b"body" if saved else None
    )
    claim = UploadClaim(
        digest,
        "one",
        4,
        "uploader",
        "token",
        datetime.now(UTC),
        40,
        generation,
        unresolved_write=31,
    )
    _one_claim(monkeypatch, claim)
    failures: list[tuple[str, str]] = []
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "fail_upload",
        lambda _database, _claim, *, category, detail, **_kwargs: failures.append(
            (category, detail)
        ),
    )
    reads: list[str] = []

    class Archive(_ReadyArchive):
        @staticmethod
        def read_verified(reference: str, _expected_hash: str) -> object:
            reads.append(reference)
            raise ArchiveReadError("archive_missing", "no such object", retryable=True)

        @staticmethod
        def write_immutable(*_args: object, **_kwargs: object) -> str:
            raise ArchiveReadError(
                "archive_unavailable", "write timed out", retryable=True
            )

    uploader = _uploader(spool, _Store(spool), Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    if saved:
        assert failures == [("archive_unavailable", "archive_unavailable: write timed out")]
        return
    location = f"s3://evidence/sha256/aa/{digest}" + (
        f"/generation/{generation}" if generation else ""
    )
    assert reads == [location]
    assert failures == [
        (
            "archive_missing",
            "write attempt 31 may yet land: archive_missing: no such object",
        )
    ]


@pytest.mark.parametrize("generation", ["", "b" * 32])
@pytest.mark.parametrize("change", ["saved_again", "archived_elsewhere", "recorded"])
def test_a_copy_that_appears_during_the_archive_read_is_not_missing_proof(
    monkeypatch: pytest.MonkeyPatch, generation: str, change: str
) -> None:
    # While this upload reads the archive, the collector saves the same bytes
    # again, or the catalogue comes to record a copy of them.
    digest = "a" * 64
    location = f"s3://evidence/sha256/aa/{digest}" + (
        f"/generation/{generation}" if generation else ""
    )
    reads: list[str] = []
    spool = _Spool()
    spool.verify = lambda _digest, _size: (  # type: ignore[attr-defined]
        b"body" if change == "saved_again" and reads else None
    )
    claim = UploadClaim(
        digest, "one", 4, "uploader", "token", datetime.now(UTC), 1, generation
    )
    calls = _one_claim(monkeypatch, claim)
    copies = {
        "saved_again": None,
        "archived_elsewhere": SimpleNamespace(
            reference=f"s3://evidence/sha256/aa/{digest}/generation/{'c' * 32}",
            recorded=True,
        ),
        "recorded": SimpleNamespace(reference=location, recorded=True),
    }
    monkeypatch.setattr(
        uploader_module.collector_uploads,
        "archived_copy",
        lambda *_args, **_kwargs: copies[change],
    )

    class Archive(_ReadyArchive):
        @staticmethod
        def read_verified(reference: str, _expected_hash: str) -> object:
            reads.append(reference)
            raise ArchiveReadError("archive_missing", "no such object", retryable=True)

    uploader = _uploader(spool, _Store(spool), Archive())

    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    assert reads == [location]
    assert calls[-1] == "fail:archive_missing:True"


def test_upload_step_times_and_counts_show_on_the_collector_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    _one_claim(
        monkeypatch,
        UploadClaim("a" * 64, "one", 4, "uploader", "token", datetime.now(UTC), 1),
    )

    class SlowArchive(_ReadyArchive):
        @staticmethod
        def write_immutable(
            _body: bytes, digest: str, *, generation: str | None = None
        ) -> str:
            time.sleep(0.05)
            return f"s3://evidence/sha256/{digest[:2]}/{digest}"

    uploader = _uploader(spool, _Store(spool), SlowArchive())
    assert asyncio.run(uploader.upload_once(owner="uploader")) is True
    spool_store = _Spool()
    collector = _collector(spool_store, _Store(spool_store), _Client(spool_store))
    process = UploaderProcess(SimpleNamespace())

    process._apply(json.dumps(uploader.snapshot()).encode(), collector)
    lines = process.metric_lines()

    assert 'clashlens_uploader_uploads_total{outcome="uploaded"} 1' in lines
    for step in ("claim", "spool_read", "archive_write", "complete", "total"):
        assert f'clashlens_uploader_step_seconds_count{{step="{step}"}} 1' in lines
    write = next(
        line
        for line in lines
        if line.startswith('clashlens_uploader_step_seconds_sum{step="archive_write"}')
    )
    assert float(write.split()[-1]) >= 0.05
    assert collector.archive_health == "ready"


def test_a_spool_read_failure_in_the_uploads_process_pauses_collection() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    collector_liveness.mark(collector, "regular")
    collector_liveness.mark(collector, "intents")
    collector_liveness.mark(collector, "uploads")
    report = {"archive_health": "ready", "spool_io_failed": True, "outcomes": {}}

    UploaderProcess(SimpleNamespace())._apply(json.dumps(report).encode(), collector)

    assert collector_liveness.livez(collector) == (
        503,
        "text/plain",
        b"spool_io_failure\n",
    )


def _report_then_exit(_arguments: object, sender: object) -> None:
    report = {"archive_health": "ready", "outcomes": {"uploaded": 1}, "stages": {}}
    sender.send_bytes(json.dumps(report).encode())  # type: ignore[attr-defined]


def _report_then_wait(arguments: SimpleNamespace, sender: object) -> None:
    Path(arguments.pid_file).write_text(str(os.getpid()))
    sender.send_bytes(b"{}")  # type: ignore[attr-defined]
    time.sleep(60)


def _never_report(_arguments: object, _sender: object) -> None:
    time.sleep(60)


async def _run_until(
    process: UploaderProcess, collector: object, done: object, timeout: float = 60
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(process.run(collector, stop))
    try:
        async with asyncio.timeout(timeout):
            while not done():  # type: ignore[operator]
                await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=uploader_module.STOP_SECONDS + 10)


def test_an_uploads_process_that_exits_is_started_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uploader_module, "run_process", _report_then_exit)
    monkeypatch.setattr(uploader_module, "RESTART_SECONDS", 0.01)
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    process = UploaderProcess(SimpleNamespace())

    asyncio.run(_run_until(process, collector, lambda: process.restarts >= 2))

    assert 'clashlens_uploader_uploads_total{outcome="uploaded"} 1' in (
        process.metric_lines()
    )
    assert collector.archive_health == "ready"


def test_a_silent_uploads_process_is_stopped_and_started_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uploader_module, "run_process", _never_report)
    monkeypatch.setattr(uploader_module, "SILENT_SECONDS", 0.5)
    monkeypatch.setattr(uploader_module, "RESTART_SECONDS", 0.01)
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    process = UploaderProcess(SimpleNamespace())

    started = time.monotonic()
    asyncio.run(_run_until(process, collector, lambda: process.restarts >= 1))

    # Stopped at once, not after the 60 seconds the process would have slept.
    assert time.monotonic() - started < 30


def test_stopping_the_collector_stops_the_uploads_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(uploader_module, "run_process", _report_then_wait)
    pid_file = tmp_path / "uploader.pid"
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    process = UploaderProcess(SimpleNamespace(pid_file=str(pid_file)))

    asyncio.run(
        _run_until(
            process,
            collector,
            lambda: pid_file.exists() and process._reported_at is not None,
        )
    )

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert process.restarts == 0
    assert process.running is False


class _UploadArchive(_FixtureS3Handler):
    """The test archive, also taking immutable writes."""

    def do_PUT(self) -> None:
        key = self.path.split("?", 1)[0].removeprefix("/evidence/")
        body = self.rfile.read(int(self.headers["Content-Length"]))
        stored = type(self).objects.setdefault(key, body)
        self.send_response(200 if stored is body else 412)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_the_uploads_process_archives_saved_responses(
    database_url: str, tmp_path: Path
) -> None:
    import psycopg
    from domain_test_support import domain_database
    from test_spool_full_recovery_postgres import TAGS, _save

    from clashlens import cli
    from clashlens.collector_db import CollectorDatabase
    from clashlens.spool import Spool

    marker = b'{"instance":"fixture-instance"}'
    marker_hash = hashlib.sha256(marker).hexdigest()
    handler = type(
        "UploadArchive",
        (_UploadArchive,),
        {"objects": {"clashlens/archive-instance.json": marker}, "get_count": 0},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    endpoint = f"127.0.0.1:{server.server_port}"
    root = tmp_path / "spool"
    try:
        with domain_database(database_url, include_coordinator=True) as connection_info:
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "INSERT INTO archive_instances (instance_id, endpoint, region,"
                    " bucket, marker_key, marker_hash, marker_payload_version)"
                    " VALUES ('fixture-instance', %s, 'us-east-1', 'evidence',"
                    " 'clashlens/archive-instance.json', %s, 'v1')",
                    (endpoint, marker_hash),
                )
            database = CollectorDatabase(connection_info)
            spool = Spool(root, max_body_bytes=64 << 10)
            digests = [_save(connection_info, database, spool, tag) for tag in TAGS]
            spool.close()
            database.close()
            # The collector's own parsed settings, as cli passes them.
            arguments = cli.build_parser().parse_args(
                [
                    "collector",
                    f"--database-url={connection_info}",
                    f"--archive-endpoint={endpoint}",
                    "--archive-insecure-test-only",
                    "--archive-access-key=test",
                    "--archive-secret-key=test",
                    f"--spool-root={root}",
                    "--archive-instance-id=fixture-instance",
                    "--archive-marker-key=clashlens/archive-instance.json",
                    f"--archive-marker-hash={marker_hash}",
                    "--archive-marker-payload-version=v1",
                    f"--archive-max-body-bytes={64 << 10}",
                ]
            )
            process = UploaderProcess(arguments)
            spool_store = _Spool()
            collector = _collector(spool_store, _Store(spool_store), _Client(spool_store))

            def uploaded() -> bool:
                return process.report.get("outcomes", {}).get("uploaded") == len(TAGS)

            asyncio.run(_run_until(process, collector, uploaded))
            with psycopg.connect(connection_info) as connection:
                rows = connection.execute(
                    "SELECT response_hash, state, archive_reference"
                    " FROM collector_response_uploads ORDER BY response_hash"
                ).fetchall()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    locations = {digest: f"sha256/{digest[:2]}/{digest}" for digest in digests}
    assert rows == [
        (digest, "complete", f"s3://evidence/{locations[digest]}")
        for digest in sorted(digests)
    ]
    assert {key for key in handler.objects if key.startswith("sha256/")} == set(
        locations.values()
    )
    assert collector.archive_health == "ready"
    assert process.restarts == 0
    lines = process.metric_lines()
    assert f'clashlens_uploader_uploads_total{{outcome="uploaded"}} {len(TAGS)}' in lines
    assert f'clashlens_uploader_step_seconds_count{{step="archive_write"}} {len(TAGS)}' in lines
