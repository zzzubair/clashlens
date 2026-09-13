from __future__ import annotations

import asyncio
import hashlib
import json
import random
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import PoolTimeout

from .archive import ArchiveReadError, S3ArchiveReader
from .collector_db import (
    CollectorDatabase,
    CollectorIntent,
    CollectorWork,
    ResponseHandoff,
    TransportFailure,
)
from .collector_http import (
    SAFE_RESPONSE_HEADERS,
    FetchedResponse,
    KeyPool,
    OfficialApiClient,
    ProviderFailure,
)
from .spool import Spool, SpoolError

_GLOBAL_ARCHIVE_FAILURES = {
    "archive_configuration_error",
    "archive_marker_mismatch",
    "archive_permission_denied",
    "archive_reference_mismatch",
    "archive_unsupported",
}
_UPLOAD_CONCURRENCY = 32


class Collector:
    def __init__(
        self,
        *,
        database: CollectorDatabase,
        spool: Spool,
        archive: S3ArchiveReader | None,
        client: OfficialApiClient,
        regular_keys: KeyPool,
        interactive_keys: KeyPool,
        archive_instance_id: str,
        collector_version: str,
        max_body_bytes: int,
        interactive_fingerprint: str | None = None,
    ) -> None:
        self.database = database
        self.spool = spool
        self.archive = archive
        self.client = client
        self.regular_keys = regular_keys
        self.interactive_keys = interactive_keys
        self.archive_instance_id = archive_instance_id
        self.collector_version = collector_version
        self.max_body_bytes = max_body_bytes
        self.interactive_fingerprint = interactive_fingerprint
        self.outcomes: dict[str, int] = {}
        self.endpoint_outcomes: dict[tuple[str, str, str], int] = {}
        self.latency_seconds: dict[tuple[str, str], float] = {}
        self.refresh_latency_seconds = 0.0
        self.refresh_count = 0
        self.last_success_at: datetime | None = None
        self.regular_inflight = 0
        self._regular_admission_lock = asyncio.Lock()
        self.archive_health = "unconfigured" if archive is None else "unknown"
        self._archive_terminal = False
        self._archive_identity_validated = False

    async def _database_call(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(operation, *args, **kwargs)
            except (psycopg.Error, PoolTimeout):
                self._count("database_failure")
                if attempt == 2:
                    raise
                await asyncio.sleep(_retry_delay(attempt))
        raise AssertionError("unreachable database retry loop")

    async def collect_player(
        self,
        work: CollectorWork,
        *,
        lane: str,
        endpoints: tuple[str, ...] = ("profile", "battle_log"),
    ) -> list[str]:
        pool = self.interactive_keys if lane == "interactive" else self.regular_keys
        try:
            with ExitStack() as stack:
                reservations = [
                    stack.enter_context(self.spool.reserve(self.max_body_bytes))
                    for _endpoint in endpoints
                ]
                return list(
                    await asyncio.gather(
                        *(
                            self._collect_endpoint(
                                work,
                                endpoint,
                                lane,
                                pool,
                                reservation=reservation,
                            )
                            for endpoint, reservation in zip(
                                endpoints, reservations, strict=True
                            )
                        )
                    )
                )
        except SpoolError:
            self._count("degraded_capacity")
            return ["capacity_paused"] * len(endpoints)

    async def collect_rankings(self) -> str:
        return await self._collect_endpoint(
            CollectorWork(None, "global", datetime.now(UTC)),
            "global_player_rankings",
            "ranking",
            self.regular_keys,
        )

    async def collect_intent(self, intent: CollectorIntent) -> str:
        """Run one durable Reset, interactive, ranking, or discovery job."""
        if intent.work_id is None:
            raise ValueError("collector intent has no durable work row")
        if intent.kind == "global_player_rankings":
            work = CollectorWork(
                None,
                "global",
                intent.due_at or intent.cycle_at,
                collector_work_id=intent.work_id,
            )
            endpoints = ("global_player_rankings",)
            lane = "ordinary"
        else:
            if intent.player_id is None or intent.normalized_tag is None:
                raise ValueError("player collector intent has no player identity")
            work = CollectorWork(
                intent.player_id,
                intent.normalized_tag,
                intent.due_at or intent.cycle_at,
                collector_work_id=intent.work_id,
            )
            endpoints = (
                ("profile",)
                if intent.kind == "discovery_profile"
                else ("profile", "battle_log")
            )
            lane = (
                "reset"
                if intent.kind == "reset_baseline"
                else (
                    "interactive"
                    if intent.kind in {"initial_collection", "live_refresh"}
                    else "ordinary"
                )
            )
        outcomes = await self.collect_player(work, lane=lane, endpoints=endpoints)
        if "capacity_paused" in outcomes:
            return "capacity_paused"
        if outcomes != ["recorded"] * len(endpoints):
            await self._database_call(
                self.database.fail_intent,
                intent.work_id,
                category="provider_failure",
                detail="one or more required endpoint requests failed",
                retryable=False,
            )
            return "failed"
        completed = await self._database_call(
            self.database.complete_intent, intent.work_id
        )
        if completed and intent.kind == "live_refresh":
            self.refresh_latency_seconds += max(
                0.0, (datetime.now(UTC) - intent.cycle_at).total_seconds()
            )
            self.refresh_count += 1
        return "complete" if completed else "incomplete"

    async def _collect_endpoint(
        self,
        work: CollectorWork,
        endpoint: str,
        lane: str,
        pool: KeyPool,
        *,
        reservation: Any | None = None,
    ) -> str:
        important = (
            lane in {"interactive", "reset"} or endpoint == "global_player_rankings"
        )
        attempts = 3 if important else 1
        pool_name = "interactive" if pool is self.interactive_keys else "regular"
        for attempt in range(attempts):
            owned_reservation = reservation is None or attempt > 0
            current_reservation = (
                self.spool.reserve(self.max_body_bytes)
                if owned_reservation
                else reservation
            )
            try:
                if owned_reservation:
                    current_reservation.__enter__()
                started_at = datetime.now(UTC)
                try:
                    if endpoint == "global_player_rankings":
                        response = await self.client.fetch_rankings(pool)
                    else:
                        response = await self.client.fetch_player(
                            pool, work.normalized_tag, endpoint
                        )
                except ProviderFailure as error:
                    await self._database_call(
                        self.database.record_transport_failure,
                        TransportFailure(
                            occurrence_key=str(uuid4()),
                            scope="global"
                            if endpoint == "global_player_rankings"
                            else "player",
                            identity_key="global"
                            if endpoint == "global_player_rankings"
                            else work.normalized_tag,
                            endpoint=endpoint,
                            player_id=work.player_id,
                            normalized_tag=None
                            if endpoint == "global_player_rankings"
                            else work.normalized_tag,
                            request_started_at=started_at,
                            failed_at=datetime.now(UTC),
                            failure_category=error.category,
                            retry_state=(
                                "bounded_retry"
                                if error.retryable and attempt + 1 < attempts
                                else "next_pass"
                            ),
                            key_label=getattr(error, "key_label", "unassigned"),
                        ),
                    )
                    self._count(error.category)
                    key = (endpoint, pool_name, error.category)
                    self.endpoint_outcomes[key] = self.endpoint_outcomes.get(key, 0) + 1
                    if not error.retryable or attempt + 1 == attempts:
                        return "failed"
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                digest = hashlib.sha256(response.body).hexdigest()
                handoff = self._make_handoff(work, response, digest)
                name, payload = self.serialize_handoff(handoff)
                await asyncio.to_thread(
                    self.spool.publish_handoff,
                    response.body,
                    digest,
                    name,
                    payload,
                    current_reservation,
                )
                await self._database_call(self.database.record_response, handoff)
                await asyncio.to_thread(self.spool.remove_handoff, name)
                if 200 <= response.http_status < 300:
                    self.last_success_at = response.response_completed_at
                self._count("recorded")
                outcome = f"http_{response.http_status}"
                key = (endpoint, pool_name, outcome)
                self.endpoint_outcomes[key] = self.endpoint_outcomes.get(key, 0) + 1
                elapsed = (
                    response.response_completed_at - response.request_started_at
                ).total_seconds()
                latency_key = (endpoint, pool_name)
                self.latency_seconds[latency_key] = self.latency_seconds.get(
                    latency_key, 0.0
                ) + max(0.0, elapsed)
                if (
                    lane == "interactive"
                    and response.http_status in {401, 403}
                    and self.interactive_fingerprint is not None
                ):
                    await asyncio.to_thread(
                        self.database.quarantine_interactive_key,
                        self.interactive_fingerprint,
                        reason=f"provider_http_{response.http_status}",
                    )
                terminal_status = (
                    response.http_status in {401, 403, 429}
                    or response.http_status >= 500
                )
                if terminal_status and important:
                    if attempt + 1 < attempts:
                        await asyncio.sleep(_retry_delay(attempt))
                        continue
                    return "failed"
                return "recorded"
            except SpoolError:
                self._count("degraded_capacity")
                return "capacity_paused"
            finally:
                if owned_reservation:
                    current_reservation.__exit__(None, None, None)
        raise AssertionError("unreachable collector retry loop")

    def _make_handoff(
        self,
        work: CollectorWork,
        response: FetchedResponse,
        digest: str,
    ) -> ResponseHandoff:
        global_scope = response.endpoint == "global_player_rankings"
        return ResponseHandoff(
            occurrence_key=str(uuid4()),
            scope="global" if global_scope else "player",
            identity_key="global" if global_scope else work.normalized_tag,
            endpoint=response.endpoint,
            player_id=None if global_scope else work.player_id,
            normalized_tag=None if global_scope else work.normalized_tag,
            request_started_at=response.request_started_at,
            response_completed_at=response.response_completed_at,
            http_status=response.http_status,
            response_hash=digest,
            byte_size=len(response.body),
            spool_key=f"sha256/{digest[:2]}/{digest}",
            collector_version=self.collector_version,
            key_label=response.key_label,
            evidence_headers={
                name.lower(): value
                for name, value in response.headers.items()
                if name.lower() in SAFE_RESPONSE_HEADERS
            },
            collector_work_id=work.collector_work_id,
        )

    @staticmethod
    def serialize_handoff(handoff: ResponseHandoff) -> tuple[str, bytes]:
        payload = asdict(handoff)
        payload["request_started_at"] = handoff.request_started_at.isoformat()
        payload["response_completed_at"] = handoff.response_completed_at.isoformat()
        return handoff.occurrence_key, json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @staticmethod
    def deserialize_handoff(payload: bytes) -> ResponseHandoff:
        value = json.loads(payload)
        value["request_started_at"] = datetime.fromisoformat(
            value["request_started_at"]
        )
        value["response_completed_at"] = datetime.fromisoformat(
            value["response_completed_at"]
        )
        return ResponseHandoff(**value)

    def recover_handoffs(self) -> int:
        recovered = 0
        for name, payload in self.spool.iter_handoffs():
            handoff = self.deserialize_handoff(payload)
            if self.spool.verify(handoff.response_hash, handoff.byte_size) is None:
                raise SpoolError("handoff raw response is missing or corrupt")
            self.database.record_response(handoff)
            self.spool.remove_handoff(name)
            recovered += 1
        referenced = self.database.referenced_spool_hashes()
        self.spool.remove_unreferenced(referenced)
        return recovered

    async def upload_once(self, *, owner: str) -> bool:
        if self.archive is None or self._archive_terminal:
            return False
        claim = await self._database_call(
            self.database.claim_upload,
            owner=owner,
            lease_seconds=60,
            now=datetime.now(UTC),
        )
        if claim is None:
            return False
        try:
            config = self.archive.instance_config
            if config is not None and not self._archive_identity_validated:
                if not await self._database_call(
                    self.database.validate_archive_instance, config
                ):
                    raise ArchiveReadError(
                        "archive_configuration_error",
                        "archive configuration contradicts PostgreSQL",
                        retryable=False,
                    )
                self._archive_identity_validated = True
            marker_health = await asyncio.to_thread(self.archive.check_marker_health)
            if marker_health == "terminal":
                raise ArchiveReadError(
                    "archive_configuration_error",
                    "archive marker validation failed",
                    retryable=False,
                )
            if marker_health == "degraded":
                raise ArchiveReadError(
                    "archive_unavailable",
                    "archive marker could not be checked",
                    retryable=True,
                )
            body = await asyncio.to_thread(
                self.spool.verify, claim.response_hash, claim.byte_size
            )
            if body is None:
                raise ArchiveReadError(
                    "spool_missing",
                    "pending upload has no local raw response",
                    retryable=False,
                )
            reference = await asyncio.to_thread(
                self.archive.write_immutable, body, claim.response_hash
            )
            await self._database_call(
                self.database.complete_upload,
                claim,
                archive_reference=reference,
                archive_instance_id=self.archive_instance_id,
            )
            self._count("uploaded")
            self.archive_health = "ready"
        except ArchiveReadError as error:
            await self._database_call(
                self.database.fail_upload,
                claim,
                category=error.category,
                detail=str(error),
                retryable=error.retryable,
            )
            self._count(error.category)
            self._archive_terminal = error.category in _GLOBAL_ARCHIVE_FAILURES
            self.archive_health = "terminal" if self._archive_terminal else "degraded"
        return True

    def cleanup_uploaded(self, *, limit: int = 100) -> int:
        deleted = 0
        for digest in self.database.deletable_hashes(limit=limit):
            if self.database.delete_spool_if_deletable(
                digest, self.spool.delete_if_unreferenced
            ):
                deleted += 1
        return deleted

    async def run(
        self,
        stop_requested: asyncio.Event,
        *,
        health_host: str,
        health_port: int,
        rankings_enabled: bool = True,
        idle_seconds: float = 0.1,
    ) -> None:
        """Run admissions, intent work, uploads, cleanup, and health together."""
        await asyncio.to_thread(self.recover_handoffs)
        await asyncio.to_thread(self.spool.cleanup_stale, 60.0)
        server = await asyncio.start_server(
            self._handle_health, health_host, health_port
        )
        tasks = [
            asyncio.create_task(self._regular_loop(stop_requested, idle_seconds)),
            asyncio.create_task(
                self._intent_loop(stop_requested, rankings_enabled, idle_seconds)
            ),
            asyncio.create_task(self._upload_loop(stop_requested, idle_seconds)),
        ]
        stop_task = asyncio.create_task(stop_requested.wait())
        try:
            done, _pending = await asyncio.wait(
                [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
            )
            failed = next(
                (task for task in done if task is not stop_task and task.exception()),
                None,
            )
            if failed is not None:
                error = failed.exception()
                assert error is not None
                raise error
            stop_requested.set()
            await asyncio.gather(*tasks)
        finally:
            stop_task.cancel()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_task, *tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()

    async def _regular_loop(
        self, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        retry_work: list[CollectorWork] = []
        while not stop_requested.is_set():
            if retry_work:
                work = retry_work
            else:
                async with self._regular_admission_lock:
                    work = await self._database_call(
                        self.database.claim_due_players,
                        limit=12,
                        now=datetime.now(UTC),
                    )
                    self.regular_inflight += len(work)
            if not work:
                await _wait_or_stop(stop_requested, idle_seconds)
                continue
            results = await asyncio.gather(
                *(self.collect_player(item, lane="ordinary") for item in work)
            )
            retry_work = [
                item
                for item, outcomes in zip(work, results, strict=True)
                if "capacity_paused" in outcomes
            ]
            async with self._regular_admission_lock:
                self.regular_inflight -= len(work) - len(retry_work)
            if retry_work:
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))

    async def _intent_loop(
        self,
        stop_requested: asyncio.Event,
        rankings_enabled: bool,
        idle_seconds: float,
    ) -> None:
        active: dict[int, tuple[bool, asyncio.Task[str]]] = {}
        scheduled_boundary: datetime | None = None
        next_rankings_at = datetime.min.replace(tzinfo=UTC)
        while not stop_requested.is_set():
            now = datetime.now(UTC)
            if rankings_enabled and now >= next_rankings_at:
                await self._database_call(self.database.schedule_rankings_cycle, now)
                next_rankings_at = _next_five_minute_cycle(now)
            boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
            if now >= boundary and boundary != scheduled_boundary:
                async with self._regular_admission_lock:
                    if self.regular_inflight == 0:
                        sweep_id = await self._database_call(
                            self.database.begin_reset,
                            boundary,
                            local_regular_inflight=0,
                        )
                        if sweep_id is not None:
                            scheduled_boundary = boundary
            for job_id, (_interactive, task) in list(active.items()):
                if task.done():
                    await task
                    del active[job_id]
            for is_interactive, limit in ((True, 6), (False, 24)):
                used = sum(kind == is_interactive for kind, _task in active.values())
                available = limit - used
                if available <= 0:
                    continue
                intents = await self._database_call(
                    self.database.pending_intents,
                    limit=available,
                    now=now,
                    interactive=is_interactive,
                )
                for intent in intents:
                    if intent.work_id is not None and intent.work_id not in active:
                        active[intent.work_id] = (
                            is_interactive,
                            asyncio.create_task(self.collect_intent(intent)),
                        )
            await _wait_or_stop(stop_requested, idle_seconds)
        if active:
            await asyncio.gather(*(task for _interactive, task in active.values()))

    async def _upload_loop(
        self, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        owners = [
            f"python-collector-{uuid4()}" for _index in range(_UPLOAD_CONCURRENCY)
        ]
        while not stop_requested.is_set():
            uploaded = await self.upload_once(owner=owners[0])
            if uploaded:
                uploaded = (
                    any(
                        await asyncio.gather(
                            *(self.upload_once(owner=owner) for owner in owners[1:])
                        )
                    )
                    or uploaded
                )
            await asyncio.to_thread(self.cleanup_uploaded, limit=128)
            if not uploaded:
                await _wait_or_stop(stop_requested, idle_seconds)

    async def _handle_health(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2.0)
            first_line = request.split(b"\r\n", 1)[0].decode("ascii", "replace")
            parts = first_line.split()
            path = parts[1] if len(parts) == 3 and parts[0] == "GET" else ""
            status, content_type, body = await self.health_response(path)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            status, content_type, body = 400, "text/plain", b"bad request\n"
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            503: "Service Unavailable",
        }
        response = (
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
            f"Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def health_response(self, path: str) -> tuple[int, str, bytes]:
        if path == "/livez":
            return 200, "text/plain", b"ok\n"
        if path not in {"/readyz", "/metrics"}:
            return 404, "text/plain", b"not found\n"
        ready, reason = await asyncio.to_thread(self.spool.readiness)
        regular = self.regular_keys.health()
        interactive = self.interactive_keys.health()
        try:
            database_metrics = await asyncio.to_thread(self.database.health_metrics)
        except psycopg.Error:
            return 503, "text/plain", b"database_unavailable\n"
        if path == "/readyz":
            healthy = ready and regular["healthy"] > 0 and interactive["healthy"] > 0
            if not ready:
                state = reason
            elif regular["healthy"] == 0:
                state = "regular_keys_unhealthy"
            elif interactive["healthy"] == 0:
                state = "interactive_key_unhealthy"
            else:
                state = "ready"
            body = state.encode() + b"\n"
            return (200 if healthy else 503), "text/plain", body
        stats = await asyncio.to_thread(self.spool.stats)
        lines = [
            f'clashlens_collector_keys_healthy{{pool="regular"}} {regular["healthy"]}',
            f'clashlens_collector_keys_healthy{{pool="interactive"}} {interactive["healthy"]}',
            f"clashlens_collector_regular_inflight {self.regular_inflight}",
            f"clashlens_spool_bytes {stats['final_bytes']}",
            f"clashlens_spool_objects {stats['final_objects']}",
            f"clashlens_spool_reserved_bytes {stats['reserved_bytes']}",
            f"clashlens_spool_free_bytes {stats['free_bytes']}",
            f"clashlens_spool_free_inodes {stats['free_inodes']}",
            f'clashlens_collector_archive_health{{state="{self.archive_health}"}} 1',
        ]
        if self.last_success_at is not None:
            age = max(0.0, (datetime.now(UTC) - self.last_success_at).total_seconds())
            lines.append(f"clashlens_collector_last_success_age_seconds {age:.6f}")
        for (endpoint, pool, outcome), count in sorted(self.endpoint_outcomes.items()):
            lines.append(
                "clashlens_collector_requests_total"
                f'{{endpoint="{endpoint}",pool="{pool}",outcome="{outcome}"}} {count}'
            )
        for (endpoint, pool), total in sorted(self.latency_seconds.items()):
            count = sum(
                value
                for (
                    seen_endpoint,
                    seen_pool,
                    outcome,
                ), value in self.endpoint_outcomes.items()
                if seen_endpoint == endpoint
                and seen_pool == pool
                and outcome.startswith("http_")
            )
            labels = f'endpoint="{endpoint}",pool="{pool}"'
            lines.append(
                f"clashlens_collector_response_latency_seconds_sum{{{labels}}} {total:.6f}"
            )
            lines.append(
                f"clashlens_collector_response_latency_seconds_count{{{labels}}} {count}"
            )
        lines.append(
            "clashlens_collector_refresh_latency_seconds_sum "
            f"{self.refresh_latency_seconds:.6f}"
        )
        lines.append(
            f"clashlens_collector_refresh_latency_seconds_count {self.refresh_count}"
        )
        for name, value in sorted(database_metrics.items()):
            lines.append(f"clashlens_collector_{name} {value}")
        return 200, "text/plain; version=0.0.4", ("\n".join(lines) + "\n").encode()

    def _count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


def _retry_delay(attempt: int) -> float:
    return min(5.0, 0.25 * 2**attempt) + random.uniform(0.0, 0.1)


async def _wait_or_stop(stop_requested: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_requested.wait(), seconds)
    except TimeoutError:
        pass


def _next_five_minute_cycle(now: datetime) -> datetime:
    rounded = now.astimezone(UTC).replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=5 - rounded.minute % 5)
