"""Shared setup for collector tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from unittest.mock import MagicMock

from clashlens.collector import Collector
from clashlens.collector_http import ApiKey, FetchedResponse, KeyPool


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

    @contextmanager
    def delete_unreferenced_batch(self):
        yield self.delete_if_unreferenced


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
        self.pool = MagicMock()

    @staticmethod
    def admit_discovery_profiles(_now: datetime) -> None:
        return None

    def record_unchanged_response(self, _handoff: Any) -> bool:
        return False

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

    def delete_spool_if_deletable(self, digests: list[str], delete: Any) -> int:
        self.spool.events.append("locked")
        deleted = [digest for digest in digests if delete(digest)]
        self.marked.extend(deleted)
        return len(deleted)

    def applied_occurrence_keys(self, _keys: list[str]) -> set[str]:
        return set()

    def referenced_spool_hashes(self) -> set[str]:
        return self.referenced


def _collector(spool: _Spool, store: _Store, client: _Client) -> Collector:
    collector = Collector(
        database=store,
        spool=spool,
        archive=None,
        client=client,
        regular_keys=KeyPool(
            [ApiKey("regular-1", "secret")],
            starts_per_second=25,
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
    collector.overlay_players = list  # No overlay is open.
    return collector
