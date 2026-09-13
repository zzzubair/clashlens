from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from clashlens.archive import ArchiveReadError
from clashlens.collector import Collector
from clashlens.collector_db import (
    CollectorIntent,
    CollectorWork,
    ResponseHandoff,
    UploadClaim,
)
from clashlens.collector_http import ApiKey, FetchedResponse, KeyPool, ProviderFailure
from clashlens.spool import Spool


class _Reservation(AbstractContextManager["_Reservation"]):
    def __init__(self, spool: _Spool) -> None:
        self.spool = spool

    def __enter__(self) -> Self:
        self.spool.events.append("reserve")
        return self

    def publish(self, body: bytes, digest: str) -> Path:
        self.spool.events.append(f"publish:{digest}:{body.decode()}")
        return Path("/spool") / digest

    def __exit__(self, *_args: object) -> None:
        return None


class _Spool:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.handoffs: dict[str, bytes] = {}
        self.delete_result = False

    def reserve(self, _limit: int) -> _Reservation:
        return _Reservation(self)

    def write_handoff(self, name: str, body: bytes) -> None:
        self.events.append("handoff")
        self.handoffs[name] = body

    def publish_handoff(
        self,
        body: bytes,
        digest: str,
        name: str,
        payload: bytes,
        reservation: _Reservation,
    ) -> None:
        reservation.publish(body, digest)
        self.write_handoff(name, payload)

    def iter_handoffs(self) -> list[tuple[str, bytes]]:
        return list(self.handoffs.items())

    def remove_handoff(self, name: str) -> None:
        self.events.append("ack")
        del self.handoffs[name]

    def verify(self, _digest: str, expected_size: int | None = None) -> bytes:
        return b"x" * (expected_size or 0)

    def final_hashes(self) -> set[str]:
        return set()

    def remove_unreferenced(self, _referenced: set[str]) -> int:
        return 0

    def readiness(self) -> tuple[bool, str]:
        return True, "ready"

    def stats(self) -> dict[str, int]:
        return {
            "final_bytes": 12,
            "final_objects": 2,
            "reserved_bytes": 4,
            "free_bytes": 100,
            "free_inodes": 20,
        }

    def delete(self, _digest: str) -> bool:
        return self.delete_result

    def delete_if_unreferenced(self, digest: str) -> bool:
        self.delete(digest)
        return True


class _Client:
    def __init__(self, spool: _Spool, *, http_status: int = 200) -> None:
        self.spool = spool
        self.http_status = http_status
        self.active = 0
        self.maximum_active = 0
        self.fetch_count = 0
        self.seen_pools: list[KeyPool] = []

    async def fetch_player(
        self, _pool: KeyPool, _tag: str, endpoint: str
    ) -> FetchedResponse:
        self.seen_pools.append(_pool)
        self.spool.events.append(f"fetch:{endpoint}")
        self.fetch_count += 1
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        now = datetime.now(UTC)
        return FetchedResponse(
            endpoint=endpoint,
            body=endpoint.encode(),
            http_status=self.http_status,
            request_started_at=now,
            response_completed_at=now,
            key_label="regular-1",
            headers={
                "content-type": "application/json",
                "set-cookie": "must-not-be-stored",
            },
        )


class _Store:
    def __init__(self, spool: _Spool) -> None:
        self.spool = spool
        self.handoffs: list[Any] = []
        self.deletable: list[str] = []
        self.marked: list[str] = []
        self.referenced: set[str] = set()
        self.failures: list[Any] = []

    def record_response(self, handoff: Any) -> object:
        assert handoff.occurrence_key in self.spool.handoffs
        self.spool.events.append("database")
        self.handoffs.append(handoff)
        self.referenced.add(handoff.response_hash)
        return object()

    def complete_intent(self, work_id: int) -> bool:
        self.spool.events.append(f"complete:{work_id}")
        return True

    def fail_intent(self, work_id: int, **_kwargs: object) -> bool:
        self.spool.events.append(f"fail:{work_id}")
        return True

    def record_transport_failure(self, failure: Any) -> int:
        self.failures.append(failure)
        return len(self.failures)

    def health_metrics(self) -> dict[str, int]:
        return {"pending_uploads": 3}

    def deletable_hashes(self, **_kwargs: object) -> list[str]:
        return self.deletable

    def delete_spool_if_deletable(self, digest: str, delete: Any) -> bool:
        self.spool.events.append("locked")
        if not delete(digest):
            return False
        self.marked.append(digest)
        return True

    def referenced_spool_hashes(self) -> set[str]:
        return self.referenced


def _collector(spool: _Spool, store: _Store, client: _Client) -> Collector:
    return Collector(
        database=store,
        spool=spool,
        archive=None,
        client=client,
        regular_keys=KeyPool(
            [ApiKey("regular-1", "secret")],
            starts_per_second=30,
            concurrency_per_key=6,
        ),
        interactive_keys=KeyPool(
            [ApiKey("interactive-1", "secret")],
            starts_per_second=5,
            concurrency_per_key=2,
        ),
        archive_instance_id="fixture",
        collector_version="test",
        max_body_bytes=4 << 20,
    )


def test_player_pair_is_reserved_then_fetched_concurrently_then_handed_off() -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)

    outcomes = asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)), lane="ordinary"
        )
    )

    assert outcomes == ["recorded", "recorded"]
    assert client.maximum_active == 2
    first_fetch = min(
        index for index, event in enumerate(spool.events) if event.startswith("fetch:")
    )
    assert spool.events[:first_fetch] == ["reserve", "reserve"]
    assert len(store.handoffs) == 2
    assert all(
        handoff.evidence_headers == {"content-type": "application/json"}
        for handoff in store.handoffs
    )
    assert spool.handoffs == {}


def test_player_pair_publishes_spool_handoffs_concurrently() -> None:
    class ConcurrentSpool(_Spool):
        def __init__(self) -> None:
            super().__init__()
            self.publications = threading.Barrier(2)

        def publish_handoff(
            self,
            body: bytes,
            digest: str,
            name: str,
            payload: bytes,
            reservation: _Reservation,
        ) -> None:
            self.publications.wait(timeout=1)
            super().publish_handoff(body, digest, name, payload, reservation)

    spool = ConcurrentSpool()
    collector = _collector(spool, _Store(spool), _Client(spool))

    assert asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)), lane="ordinary"
        )
    ) == ["recorded", "recorded"]


def test_failure_without_a_healthy_key_records_a_non_null_label() -> None:
    class FailedClient(_Client):
        async def fetch_player(
            self, _pool: KeyPool, _tag: str, _endpoint: str
        ) -> FetchedResponse:
            raise ProviderFailure("no_healthy_api_key", retryable=False)

    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, FailedClient(spool))

    outcome = asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)),
            lane="ordinary",
            endpoints=("profile",),
        )
    )

    assert outcome == ["failed"]
    assert [failure.key_label for failure in store.failures] == ["unassigned"]


@pytest.mark.parametrize("failure_point", ["reserve", "publish"])
def test_raw_spool_os_error_pauses_collection(failure_point: str) -> None:
    class FailedSpool(_Spool):
        def reserve(self, limit: int) -> _Reservation:
            if failure_point == "reserve":
                raise OSError("spool unavailable")
            return super().reserve(limit)

        def publish_handoff(
            self,
            body: bytes,
            digest: str,
            name: str,
            payload: bytes,
            reservation: _Reservation,
        ) -> None:
            if failure_point == "publish":
                raise OSError("spool unavailable")
            super().publish_handoff(body, digest, name, payload, reservation)

    spool = FailedSpool()
    collector = _collector(spool, _Store(spool), _Client(spool))

    outcome = asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)), lane="ordinary"
        )
    )

    assert outcome == ["capacity_paused", "capacity_paused"]
    assert collector.outcomes["degraded_capacity"] >= 1


def test_run_starts_one_background_uploader() -> None:
    spool = _Spool()
    spool.cleanup_stale = lambda _age: None  # type: ignore[attr-defined]
    collector = _collector(spool, _Store(spool), _Client(spool))
    uploads_started = 0

    async def idle_loop(stop: asyncio.Event, _idle_seconds: float) -> None:
        await stop.wait()

    async def upload_loop(stop: asyncio.Event, _idle_seconds: float) -> None:
        nonlocal uploads_started
        uploads_started += 1
        stop.set()

    collector._regular_loop = idle_loop  # type: ignore[method-assign]
    collector._intent_loop = (  # type: ignore[method-assign]
        lambda stop, _rankings, idle: idle_loop(stop, idle)
    )
    collector._upload_loop = upload_loop  # type: ignore[method-assign]

    asyncio.run(
        collector.run(
            asyncio.Event(),
            health_host="127.0.0.1",
            health_port=0,
        )
    )

    assert uploads_started == 1


def test_one_bad_archive_object_does_not_stop_other_uploads() -> None:
    spool = _Spool()
    spool.verify = lambda _digest, _size: b"body"  # type: ignore[attr-defined]
    store = _Store(spool)
    now = datetime.now(UTC)
    claims = [
        UploadClaim("a" * 64, "one", 4, "uploader", "one", now, 1),
        UploadClaim("b" * 64, "two", 4, "uploader", "two", now, 1),
    ]
    failures: list[tuple[str, str, bool]] = []
    store.claim_upload = lambda **_kwargs: claims.pop(0)  # type: ignore[attr-defined]
    store.fail_upload = (  # type: ignore[attr-defined]
        lambda claim, *, category, retryable, **_kwargs: failures.append(
            (claim.response_hash, category, retryable)
        )
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @staticmethod
        def write_immutable(_body: bytes, _digest: str) -> str:
            raise ArchiveReadError(
                "archive_checksum_mismatch",
                "stored bytes differ",
                retryable=False,
            )

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert failures == [
        ("a" * 64, "archive_checksum_mismatch", False),
        ("b" * 64, "archive_checksum_mismatch", False),
    ]
    assert collector.archive_health == "degraded"


def test_background_uploader_drains_multiple_objects_concurrently() -> None:
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

    store.claim_upload = claim_upload  # type: ignore[attr-defined]
    store.complete_upload = complete_upload  # type: ignore[attr-defined]

    class Archive:
        instance_config = None
        writes = 0
        lock = threading.Lock()
        concurrent_writes = threading.Barrier(2)

        @staticmethod
        def check_marker_health() -> str:
            return "ready"

        @classmethod
        def write_immutable(cls, _body: bytes, digest: str) -> str:
            with cls.lock:
                cls.writes += 1
                write_number = cls.writes
            if write_number > 1:
                cls.concurrent_writes.wait(timeout=1)
            return f"archive/{digest}"

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    asyncio.run(asyncio.wait_for(collector._upload_loop(stop, 0.01), timeout=2))

    assert completed == 3


def test_startup_finishes_a_spool_handoff_before_acknowledging_it() -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    now = datetime.now(UTC)
    handoff = ResponseHandoff(
        occurrence_key="recovery-response",
        scope="player",
        identity_key="#2PP",
        endpoint="profile",
        player_id=1,
        normalized_tag="#2PP",
        request_started_at=now,
        response_completed_at=now,
        http_status=200,
        response_hash="1900eab6c028483d7126599ee6f50de0d27907b5c65fa90524580b4b0f9852b0",
        byte_size=7,
        spool_key="sha256/19/1900eab6c028483d7126599ee6f50de0d27907b5c65fa90524580b4b0f9852b0",
        collector_version="test",
        key_label="regular-1",
        evidence_headers={},
    )
    name, body = collector.serialize_handoff(handoff)
    spool.handoffs[name] = body

    recovered = collector.recover_handoffs()

    assert recovered == 1
    assert [item.occurrence_key for item in store.handoffs] == [handoff.occurrence_key]
    assert spool.events == ["database", "ack"]


def test_startup_removes_final_file_with_no_sidecar_or_database_reference(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    orphan = b"published before the crash"
    retained = b"already recorded"
    orphan_hash = hashlib.sha256(orphan).hexdigest()
    retained_hash = hashlib.sha256(retained).hexdigest()
    spool.publish(orphan, orphan_hash)
    spool.publish(retained, retained_hash)
    store = _Store(spool)  # type: ignore[arg-type]
    store.referenced = {retained_hash}
    collector = _collector(spool, store, _Client(spool))  # type: ignore[arg-type]

    assert collector.recover_handoffs() == 0

    assert spool.verify(orphan_hash) is None
    assert spool.verify(retained_hash) == retained


def test_interactive_intent_keeps_one_compact_work_identity() -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    intent = CollectorIntent(
        "live_refresh",
        datetime.now(UTC),
        1,
        "#2PP",
        work_id=9,
    )

    outcome = asyncio.run(collector.collect_intent(intent))

    assert outcome == "complete"
    assert [item.collector_work_id for item in store.handoffs] == [9, 9]
    assert client.seen_pools == [collector.interactive_keys] * 2
    assert spool.events[-1] == "complete:9"


@pytest.mark.parametrize("http_status", [401, 403, 429, 500])
def test_important_intent_terminal_status_fails_after_bounded_retries(
    http_status: int,
) -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool, http_status=http_status)
    collector = _collector(spool, store, client)
    intent = CollectorIntent(
        "live_refresh",
        datetime.now(UTC),
        1,
        "#2PP",
        work_id=9,
    )

    outcome = asyncio.run(collector.collect_intent(intent))

    assert outcome == "failed"
    assert client.fetch_count == 6
    assert len(store.handoffs) == 6
    assert "fail:9" in spool.events
    assert "complete:9" not in spool.events


def test_health_reports_key_spool_and_database_state() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))

    ready = asyncio.run(collector.health_response("/readyz"))
    metrics = asyncio.run(collector.health_response("/metrics"))

    assert ready == (200, "text/plain", b"ready\n")
    assert b"clashlens_spool_bytes 12" in metrics[2]
    assert b"clashlens_collector_pending_uploads 3" in metrics[2]
    assert b"clashlens_spool_free_bytes 100" in metrics[2]


def test_cleanup_finishes_database_ack_after_a_crash_already_removed_file() -> None:
    spool = _Spool()
    store = _Store(spool)
    store.deletable = ["a" * 64]
    collector = _collector(spool, store, _Client(spool))

    assert collector.cleanup_uploaded() == 1
    assert store.marked == ["a" * 64]
    assert spool.events == ["locked"]
