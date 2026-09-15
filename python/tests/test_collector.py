from __future__ import annotations

import asyncio
import errno
import hashlib
import importlib
import os
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
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
from clashlens.spool import Spool, SpoolError

collector_module = importlib.import_module("clashlens.collector")
spool_module = importlib.import_module("clashlens.spool")


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

    def remove_unreferenced(self, referenced: Callable[[], set[str]]) -> int:
        referenced()
        return 0

    def cleanup_stale(self, _age_seconds: float) -> int:
        return 0

    def probe_writable(self, _limit: int) -> None:
        return None

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
        self.cooldowns: list[tuple[str, int]] = []

    def record_response(self, handoff: Any) -> object:
        assert handoff.occurrence_key in self.spool.handoffs
        self.spool.events.append("database")
        self.handoffs.append(handoff)
        self.referenced.add(handoff.response_hash)
        return object()

    def record_recovered_response(
        self, handoff: Any, *, serialized: bool = False
    ) -> object:
        assert serialized is True
        return self.record_response(handoff)

    def complete_intent(self, work_id: int) -> bool:
        self.spool.events.append(f"complete:{work_id}")
        return True

    def fail_intent(self, work_id: int, **_kwargs: object) -> bool:
        self.spool.events.append(f"fail:{work_id}")
        return True

    def record_transport_failure(self, failure: Any) -> int:
        self.failures.append(failure)
        return len(self.failures)

    def cooldown_interactive_key(self, fingerprint: str, seconds: int) -> bool:
        self.cooldowns.append((fingerprint, seconds))
        self.spool.events.append(f"cooldown:{seconds}")
        return True

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


@pytest.mark.parametrize(
    ("lane", "expected_endpoints"),
    [
        ("ordinary", ["battle_log"]),
        ("reset", ["profile", "battle_log"]),
        ("interactive", ["profile", "battle_log"]),
    ],
)
def test_fresh_discovery_profile_is_reused_only_for_first_regular_collection(
    lane: str, expected_endpoints: list[str]
) -> None:
    spool = _Spool()
    store = _Store(spool)
    client = _Client(spool)
    collector = _collector(spool, store, client)
    now = datetime.now(UTC)
    work = CollectorWork(
        1,
        "#2PP",
        now,
        profile_fresh_until=now + timedelta(seconds=1),
    )

    outcomes = asyncio.run(collector.collect_player(work, lane=lane))

    assert outcomes == ["recorded"] * len(expected_endpoints)
    assert sorted(handoff.endpoint for handoff in store.handoffs) == sorted(
        expected_endpoints
    )


def test_regular_collection_fetches_profile_if_freshness_expires_while_reserving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    clock = [now]
    monkeypatch.setattr(
        collector_module, "datetime", SimpleNamespace(now=lambda _zone: clock[0])
    )

    class DelayedReservationSpool(_Spool):
        delayed = False

        def reserve(self, limit: int) -> _Reservation:
            if not self.delayed:
                self.delayed = True
                clock[0] = now + timedelta(seconds=2)
            return super().reserve(limit)

    spool = DelayedReservationSpool()
    store = _Store(spool)
    collector = _collector(spool, store, _Client(spool))
    work = CollectorWork(
        1,
        "#2PP",
        now,
        profile_fresh_until=now + timedelta(seconds=1),
    )

    outcomes = asyncio.run(collector.collect_player(work, lane="ordinary"))

    assert outcomes == ["recorded", "recorded"]
    assert sorted(handoff.endpoint for handoff in store.handoffs) == [
        "battle_log",
        "profile",
    ]


def test_regular_collection_fetches_profile_if_battle_finishes_after_freshness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    clock = [now]
    monkeypatch.setattr(
        collector_module, "datetime", SimpleNamespace(now=lambda _zone: clock[0])
    )

    class DelayedBattleClient(_Client):
        async def fetch_player(
            self, pool: KeyPool, tag: str, endpoint: str
        ) -> FetchedResponse:
            if endpoint == "battle_log":
                clock[0] = now + timedelta(seconds=2)
            return await super().fetch_player(pool, tag, endpoint)

    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, DelayedBattleClient(spool))
    work = CollectorWork(
        1,
        "#2PP",
        now,
        profile_fresh_until=now + timedelta(seconds=1),
    )

    outcomes = asyncio.run(collector.collect_player(work, lane="ordinary"))

    assert outcomes == ["recorded", "recorded"]
    assert sorted(handoff.endpoint for handoff in store.handoffs) == [
        "battle_log",
        "profile",
    ]


def test_regular_collection_fetches_profile_when_first_battle_fails() -> None:
    class FailedBattleClient(_Client):
        async def fetch_player(
            self, pool: KeyPool, tag: str, endpoint: str
        ) -> FetchedResponse:
            if endpoint == "battle_log":
                raise ProviderFailure("network_failure", retryable=True)
            return await super().fetch_player(pool, tag, endpoint)

    spool = _Spool()
    store = _Store(spool)
    collector = _collector(spool, store, FailedBattleClient(spool))
    now = datetime.now(UTC)
    work = CollectorWork(
        1,
        "#2PP",
        now,
        profile_fresh_until=now + timedelta(seconds=1),
    )

    outcomes = asyncio.run(collector.collect_player(work, lane="ordinary"))

    assert outcomes == ["recorded", "failed"]
    assert [handoff.endpoint for handoff in store.handoffs] == ["profile"]


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


def test_spool_capacity_pause_can_recover_without_restart() -> None:
    class FullSpool(_Spool):
        full = True
        probes = 0

        def reserve(self, limit: int) -> _Reservation:
            if self.full:
                self.full = False
                raise OSError(errno.ENOSPC, "spool full")
            return super().reserve(limit)

        def probe_writable(self, _limit: int) -> None:
            self.probes += 1
            if self.probes == 1:
                raise OSError(errno.ENOSPC, "quota still full")

    spool = FullSpool()
    client = _Client(spool)
    collector = _collector(spool, _Store(spool), client)
    work = CollectorWork(1, "#2PP", datetime.now(UTC))

    assert asyncio.run(collector.collect_player(work, lane="ordinary")) == [
        "capacity_paused",
        "capacity_paused",
    ]
    assert asyncio.run(collector.collect_player(work, lane="ordinary")) == [
        "capacity_paused",
        "capacity_paused",
    ]
    assert client.fetch_count == 0
    collector._spool_probe_after = 0.0
    assert asyncio.run(collector.collect_player(work, lane="ordinary")) == [
        "recorded",
        "recorded",
    ]
    assert client.fetch_count == 2
    assert collector.outcomes["degraded_capacity"] == 2
    assert collector.outcomes["capacity_recovered"] == 1
    assert asyncio.run(collector.health_response("/readyz"))[0] == 200


@pytest.mark.parametrize("readiness_result", [(False, "storage_error:OSError"), None])
def test_readiness_spool_io_failure_stays_failed_until_restart(
    readiness_result: tuple[bool, str] | None,
) -> None:
    class FailedSpool(_Spool):
        def readiness(self) -> tuple[bool, str]:
            if readiness_result is None:
                raise OSError(errno.EIO, "spool unavailable")
            return readiness_result

    spool = FailedSpool()
    client = _Client(spool)
    collector = _collector(spool, _Store(spool), client)

    assert asyncio.run(collector.health_response("/readyz")) == (
        503,
        "text/plain",
        b"spool_io_failure\n",
    )
    metrics = asyncio.run(collector.health_response("/metrics"))
    assert metrics[0] == 200
    assert b"clashlens_collector_spool_io_failed 1" in metrics[2]
    assert asyncio.run(
        collector.collect_player(
            CollectorWork(1, "#2PP", datetime.now(UTC)), lane="ordinary"
        )
    ) == ["capacity_paused", "capacity_paused"]
    assert client.fetch_count == 0


def test_rankings_first_spool_io_failure_latches_before_fetch() -> None:
    class FailedSpool(_Spool):
        def reserve(self, _limit: int) -> _Reservation:
            raise OSError(errno.EIO, "spool unavailable")

    spool = FailedSpool()
    client = _Client(spool)
    collector = _collector(spool, _Store(spool), client)

    assert asyncio.run(collector.collect_rankings()) == "capacity_paused"
    assert collector._spool_io_failed is True
    assert client.fetch_count == 0


def test_rankings_release_io_failure_latches_after_recording() -> None:
    class FailedReservation(_Reservation):
        def __exit__(self, *args: object) -> None:
            super().__exit__(*args)
            raise OSError(errno.EIO, "capacity lock unavailable")

    class FailedSpool(_Spool):
        def reserve(self, _limit: int) -> _Reservation:
            return FailedReservation(self)

    class RankingClient(_Client):
        async def fetch_rankings(self, pool: KeyPool) -> FetchedResponse:
            return await self.fetch_player(pool, "global", "global_player_rankings")

    spool = FailedSpool()
    client = RankingClient(spool)
    collector = _collector(spool, _Store(spool), client)

    assert asyncio.run(collector.collect_rankings()) == "recorded"
    assert collector._spool_io_failed is True
    assert client.fetch_count == 1


@pytest.mark.parametrize("failure_point", ["reserve", "publish"])
def test_spool_io_failure_stays_paused_and_fails_readiness(
    failure_point: str,
) -> None:
    class FailedSpool(_Spool):
        failed = False

        def reserve(self, limit: int) -> _Reservation:
            if failure_point == "reserve" and not self.failed:
                self.failed = True
                raise OSError(errno.EIO, "spool unavailable")
            return super().reserve(limit)

        def publish_handoff(
            self,
            body: bytes,
            digest: str,
            name: str,
            payload: bytes,
            reservation: _Reservation,
        ) -> None:
            if failure_point == "publish" and not self.failed:
                self.failed = True
                raise SpoolError("short spool write")
            super().publish_handoff(body, digest, name, payload, reservation)

    spool = FailedSpool()
    collector = _collector(spool, _Store(spool), _Client(spool))

    work = CollectorWork(1, "#2PP", datetime.now(UTC))
    outcome = asyncio.run(collector.collect_player(work, lane="ordinary"))

    assert "capacity_paused" in outcome
    assert set(outcome) <= {"capacity_paused", "recorded"}
    fetches_after_failure = collector.client.fetch_count
    assert asyncio.run(collector.collect_player(work, lane="ordinary")) == [
        "capacity_paused",
        "capacity_paused",
    ]
    assert collector.client.fetch_count == fetches_after_failure
    assert collector.outcomes["spool_io_failure"] >= 1
    assert asyncio.run(collector.health_response("/readyz")) == (
        503,
        "text/plain",
        b"spool_io_failure\n",
    )
    assert asyncio.run(collector.collect_rankings()) == "capacity_paused"
    assert collector.client.fetch_count == fetches_after_failure


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


def test_normal_shutdown_removes_only_unreferenced_spool_bodies(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    store = _Store(spool)  # type: ignore[arg-type]
    collector = _collector(  # type: ignore[arg-type]
        spool,
        store,
        _Client(spool),
    )
    bodies = {
        "unreferenced": b"duplicate response",
        "referenced": b"pending upload response",
        "handoff": b"in-flight handoff response",
    }
    hashes = {
        name: hashlib.sha256(body).hexdigest() for name, body in bodies.items()
    }

    async def idle_loop(stop: asyncio.Event, _idle_seconds: float) -> None:
        await stop.wait()

    async def drain_after_stop(stop: asyncio.Event, _idle_seconds: float) -> None:
        await stop.wait()
        for name, body in bodies.items():
            spool.publish(body, hashes[name])
        store.referenced.add(hashes["referenced"])
        spool.write_handoff(
            "pending-handoff",
            f'{{"response_hash":"{hashes["handoff"]}"}}'.encode(),
        )

    async def request_stop(stop: asyncio.Event, _idle_seconds: float) -> None:
        stop.set()

    collector._regular_loop = drain_after_stop  # type: ignore[method-assign]
    collector._intent_loop = (  # type: ignore[method-assign]
        lambda stop, _rankings, idle: idle_loop(stop, idle)
    )
    collector._upload_loop = request_stop  # type: ignore[method-assign]

    try:
        asyncio.run(
            collector.run(
                asyncio.Event(),
                health_host="127.0.0.1",
                health_port=0,
            )
        )

        assert spool.final_hashes() == {hashes["referenced"], hashes["handoff"]}
        assert [name for name, _payload in spool.iter_handoffs()] == [
            "pending-handoff"
        ]
    finally:
        spool.close()


@pytest.mark.parametrize(
    "pending_after_second",
    [(1, 3, 4, 2), (3, 4, 1, 2)],
    ids=["active-oldest", "new-work-before-active"],
)
def test_intent_lane_refills_around_active_rows_without_overadmitting(
    monkeypatch: pytest.MonkeyPatch, pending_after_second: tuple[int, ...]
) -> None:
    spool = _Spool()

    class IntentStore(_Store):
        def __init__(self) -> None:
            super().__init__(spool)
            now = datetime.now(UTC)
            self.intents = [
                CollectorIntent(
                    "discovery_profile", now, index, f"#{index}", work_id=index
                )
                for index in range(1, 5)
            ]
            self.completed: set[int] = set()

        def pending_intents(
            self,
            limit: int,
            _now: datetime | None = None,
            *,
            interactive: bool | None = None,
            **_kwargs: object,
        ) -> list[CollectorIntent]:
            if interactive:
                return []
            order = (
                pending_after_second
                if 2 in self.completed
                else tuple(range(1, 5))
            )
            by_id = {intent.work_id: intent for intent in self.intents}
            return [
                by_id[work_id]
                for work_id in order
                if work_id not in self.completed
            ][:limit]

        @staticmethod
        def begin_reset(
            _boundary: datetime, *, local_regular_inflight: int
        ) -> None:
            return None

    store = IntentStore()
    collector = _collector(spool, store, _Client(spool))
    oldest_release = asyncio.Event()
    newcomer_release = asyncio.Event()
    third_started = asyncio.Event()
    fourth_started = asyncio.Event()
    active_count = 0
    maximum_active = 0

    async def collect_intent(intent: CollectorIntent) -> str:
        nonlocal active_count, maximum_active
        assert intent.work_id is not None
        active_count += 1
        maximum_active = max(maximum_active, active_count)
        try:
            if intent.work_id == 1:
                await oldest_release.wait()
            elif intent.work_id in {3, 4}:
                (third_started if intent.work_id == 3 else fourth_started).set()
                await newcomer_release.wait()
            store.completed.add(intent.work_id)
            return "complete"
        finally:
            active_count -= 1

    collector.collect_intent = collect_intent  # type: ignore[method-assign]
    monkeypatch.setattr(collector_module, "_ORDINARY_INTENT_PARALLELISM", 2)

    async def run() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(collector._intent_loop(stop, False, 0.001))
        try:
            await asyncio.wait_for(third_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert not fourth_started.is_set()
            newcomer_release.set()
            await asyncio.wait_for(fourth_started.wait(), timeout=1)
            while store.completed != {2, 3, 4}:
                await asyncio.sleep(0.001)
        finally:
            stop.set()
            oldest_release.set()
            newcomer_release.set()
            await task

    asyncio.run(run())

    assert maximum_active == 2
    assert store.completed == {1, 2, 3, 4}


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
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claims.pop(0),
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert failures == [
        ("a" * 64, "archive_checksum_mismatch", False),
        ("b" * 64, "archive_checksum_mismatch", False),
    ]
    assert collector.archive_health == "degraded"


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
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **kwargs: claim_upload(**kwargs),
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    asyncio.run(asyncio.wait_for(collector._upload_loop(stop, 0.01), timeout=2))

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
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **kwargs: claim_upload(**kwargs),
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "renew_upload",
        lambda _database, _claim, **_kwargs: None,
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    async def run_uploader() -> None:
        with pytest.raises(RuntimeError, match="upload lease lost"):
            await asyncio.wait_for(
                collector._upload_loop(asyncio.Event(), 0.01), timeout=2
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

    monkeypatch.setattr(collector_module, "_UPLOAD_RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def renew(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            renewed_during_write.set()

    monkeypatch.setattr(collector_module.collector_uploads, "renew_upload", renew)
    monkeypatch.setattr(
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert renewals >= 3
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

    monkeypatch.setattr(collector_module, "_UPLOAD_RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def renew(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            lease_lost.set()
            raise collector_module.collector_uploads.UploadLeaseLost(
                "upload lease lost"
            )

    monkeypatch.setattr(collector_module.collector_uploads, "renew_upload", renew)
    monkeypatch.setattr(
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert collector.outcomes["upload_lease_lost"] == 1
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

    monkeypatch.setattr(collector_module, "_UPLOAD_RENEW_INTERVAL", 0.01)
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "claim_upload",
        lambda _database, **_kwargs: claim,
    )

    def lose_renewal(_database: object, _claim: UploadClaim, **_kwargs: object) -> None:
        lease_lost.set()
        raise collector_module.collector_uploads.UploadLeaseLost("upload lease lost")

    monkeypatch.setattr(
        collector_module.collector_uploads, "renew_upload", lose_renewal
    )
    monkeypatch.setattr(
        collector_module.collector_uploads,
        "fail_upload",
        lambda _database, seen, **_kwargs: failed.append(seen.response_hash),
    )

    class Archive:
        instance_config = None

        @staticmethod
        def check_marker_health() -> str:
            assert lease_lost.wait(timeout=1)
            return "degraded"

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert collector.outcomes["upload_lease_lost"] == 1
    assert failed == []


def test_cancelled_thread_waits_for_immutable_operation_to_finish() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_write() -> None:
        started.set()
        release.wait(timeout=1)
        finished.set()

    async def cancel_write() -> None:
        task = asyncio.create_task(collector_module._drain_to_thread(blocking_write))
        await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_write())
    assert finished.is_set()


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
        collector_module.collector_uploads,
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

    collector = _collector(spool, store, _Client(spool))
    collector.archive = Archive()  # type: ignore[assignment]

    assert asyncio.run(collector.upload_once(owner="uploader")) is True
    assert collector._spool_io_failed is True
    assert store.referenced == {digest}


def test_background_cleanup_spool_io_failure_pauses_without_stopping() -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    stop = asyncio.Event()

    def failed_cleanup(*, limit: int) -> int:
        del limit
        raise OSError(errno.EIO, "spool unavailable")

    collector.cleanup_uploaded = failed_cleanup  # type: ignore[method-assign]

    async def run_uploader() -> None:
        task = asyncio.create_task(collector._upload_loop(stop, 0.01))
        for _attempt in range(100):
            if collector._spool_io_failed:
                break
            await asyncio.sleep(0.01)
        assert collector._spool_io_failed is True
        assert task.done() is False
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(run_uploader())


def test_background_sweep_revisits_recent_crash_temporary_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stop = asyncio.Event()
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    reservation = spool.reserve(1024)
    temporary_name = spool._write_temp(b"crash-left-body", reservation)
    reservation.release()
    temporary_path = spool.root / "tmp" / temporary_name
    os.utime(temporary_path, (1000.0, 1000.0))
    cleanup_results: list[int] = []
    cleanup_stale = spool.cleanup_stale

    def tracked_cleanup(age_seconds: float) -> int:
        result = cleanup_stale(age_seconds)
        cleanup_results.append(result)
        return result

    spool.cleanup_stale = tracked_cleanup  # type: ignore[method-assign]

    async def no_wait(stop_requested: asyncio.Event, _seconds: float) -> None:
        if len(cleanup_results) == 2:
            stop_requested.set()
        await asyncio.sleep(0)

    ticks = iter((61.0, 61.0, 122.0, 122.0))
    monkeypatch.setattr(
        collector_module,
        "time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    cleanup_times = iter((1050.0, 1061.0))
    monkeypatch.setattr(
        spool_module,
        "time",
        lambda: next(cleanup_times),
    )
    monkeypatch.setattr(collector_module, "_wait_or_stop", no_wait)
    collector = _collector(spool, _Store(spool), _Client(spool))  # type: ignore[arg-type]

    async def idle_upload(*, owner: str) -> bool:
        del owner
        await stop.wait()
        return False

    collector.upload_once = idle_upload  # type: ignore[method-assign]

    try:
        asyncio.run(asyncio.wait_for(collector._upload_loop(stop, 0.01), timeout=1))
        assert cleanup_results == [0, 1]
        assert temporary_path.exists() is False
    finally:
        spool.close()


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
        content_fingerprint="1900eab6c028483d7126599ee6f50de0d27907b5c65fa90524580b4b0f9852b0",
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


def test_interactive_429_persists_cooldown_before_spool_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RateLimitedClient(_Client):
        async def fetch_player(
            self, pool: KeyPool, tag: str, endpoint: str
        ) -> FetchedResponse:
            response = await super().fetch_player(pool, tag, endpoint)
            status = 429 if self.fetch_count == 1 else 200
            return FetchedResponse(
                endpoint=response.endpoint,
                body=response.body,
                http_status=status,
                request_started_at=response.request_started_at,
                response_completed_at=response.response_completed_at,
                key_label=response.key_label,
                headers={**response.headers, "retry-after": "119.2"},
            )

    spool = _Spool()
    store = _Store(spool)
    client = RateLimitedClient(spool)
    collector = _collector(spool, store, client)
    collector.interactive_fingerprint = "f" * 64
    monkeypatch.setattr(collector_module, "_retry_delay", lambda _attempt: 0.0)

    outcome = asyncio.run(
        collector._collect_endpoint(
            CollectorWork(1, "#2PP", datetime.now(UTC)),
            "profile",
            "interactive",
            collector.interactive_keys,
        )
    )

    assert outcome == "recorded"
    assert store.cooldowns == [("f" * 64, 120)]
    cooldown = spool.events.index("cooldown:120")
    first_publish = next(
        index
        for index, event in enumerate(spool.events)
        if event.startswith("publish:")
    )
    assert cooldown < first_publish


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
