from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import math
import random
import time
from contextlib import ExitStack, suppress
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import PoolTimeout

from . import (
    collector_commits,
    collector_intents,
    collector_reset,
    collector_uploads,
    weekly_eligibility,
)
from .archive import ArchiveReadError, S3ArchiveReader
from .battle_log_schedule import BattleLogSchedule
from .collector_db import (
    CollectorDatabase,
    CollectorIntent,
    CollectorWork,
    ResponseHandoff,
    TransportFailure,
)
from .collector_http import (
    SAFE_RESPONSE_HEADERS,
    CollectionWindowClosed,
    FetchedResponse,
    KeyPool,
    OfficialApiClient,
    ProviderFailure,
    retry_after_seconds,
)
from .response_fields import content_fingerprint
from .spool import Spool, SpoolError

_GLOBAL_ARCHIVE_FAILURES = {
    "archive_configuration_error",
    "archive_marker_mismatch",
    "archive_permission_denied",
    "archive_reference_mismatch",
    "archive_unsupported",
}
_UPLOAD_CONCURRENCY = 32
# Upload owners share these slots to limit competition with player checks.
# Archive writes run outside the database limit and can still overlap.
_UPLOAD_DATABASE_SLOTS = 4
_UPLOAD_LEASE_SECONDS = 60
_UPLOAD_RENEW_INTERVAL = 20.0
_HANDOFF_LOCK_STRIPES = 256
_HANDOFF_PROTOCOL = 2
# Short cleanup turns keep publication moving while the deletion queue drains.
_CLEANUP_BATCH_SIZE = 16
# These slots cover HTTP plus durable handoffs; key limits still bound requests.
# A check fetches its profile, saves it, then maybe its battle log, one after
# the other, so a check holds at most one request at a time. The collector
# command sizes this from its keys; see docs/collector-polling.md.
_REGULAR_PARALLELISM = 256
_ORDINARY_INTENT_PARALLELISM = 32


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
        weekly_eligibility_enabled: bool = False,
        regular_parallelism: int = _REGULAR_PARALLELISM,
    ) -> None:
        if regular_parallelism < 1:
            raise ValueError("regular parallelism must be positive")
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
        self.weekly_eligibility_enabled = weekly_eligibility_enabled
        self.regular_parallelism = regular_parallelism
        self.battle_logs = BattleLogSchedule()
        self.outcomes: dict[str, int] = {}
        self.endpoint_outcomes: dict[tuple[str, str, str], int] = {}
        self.latency_seconds: dict[tuple[str, str], float] = {}
        self.refresh_latency_seconds = 0.0
        self.refresh_count = 0
        self.regular_inflight = 0
        self._retries_while_answering: dict[int, int] = {}
        self._regular_admission_lock = asyncio.Lock()
        self.archive_health = "unconfigured" if archive is None else "unknown"
        self._archive_terminal = False
        self._archive_identity_validated = False
        self._next_upload_release = 0.0
        self._upload_database_slots = asyncio.Semaphore(_UPLOAD_DATABASE_SLOTS)
        self._spool_io_failed = False
        self._spool_capacity_failed = False
        self._spool_recovery_lock = asyncio.Lock()
        self._spool_probe_after = 0.0
        self._handoff_locks = tuple(
            asyncio.Lock() for _index in range(_HANDOFF_LOCK_STRIPES)
        )
        self._handoff_recovery_required = False
        # Newest saved response per player and request type, kept while it waits; it
        # resolves when it commits or fails, or to the task committing it later.
        self._handoff_turns: dict[collector_commits.Identity, collector_commits.Turn] = {}
        self._later_commits: dict[asyncio.Task[None], int | None] = {}
        self._unrecovered: list[tuple[str, ResponseHandoff, bool]] = []
        self._stopping = asyncio.Event()
        # Newest committed (seen time, field fingerprint) per scope, identity
        # and endpoint, from this process only; empty after a restart.
        self._committed: dict[tuple[str, str, str], tuple[datetime, str]] = {}
        self._metrics_lock = asyncio.Lock()
        self._metrics_refresh_after = 0.0
        self._database_metrics: dict[str, int | float] = {}

    async def _database_call(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                return await asyncio.to_thread(operation, *args, **kwargs)
            except psycopg.errors.LockNotAvailable:
                raise
            except (psycopg.Error, PoolTimeout):
                self._count("database_failure")
                if attempt == 2:
                    raise
                await asyncio.sleep(_retry_delay(attempt))
        raise AssertionError("unreachable database retry loop")

    async def _upload_database_call(
        self, operation: Any, *args: Any, **kwargs: Any
    ) -> Any:
        async with self._upload_database_slots:
            return await _drain_awaitable(
                self._database_call(operation, *args, **kwargs)
            )

    async def collect_player(
        self,
        work: CollectorWork,
        *,
        lane: str,
        endpoints: tuple[str, ...] = ("profile", "battle_log"),
    ) -> list[str]:
        outage = getattr(self.client, "provider_outage", None)
        # Regular checks wait out an outage pause as paused work, not in flight.
        if not await self._spool_available() or (lane == "ordinary" and outage is not None and outage.paused):
            return ["capacity_paused"] * len(endpoints)
        pool = self.interactive_keys if lane == "interactive" else self.regular_keys
        regular_check = lane == "ordinary" and endpoints == ("profile", "battle_log")
        reuse_fresh_profile = (
            regular_check
            and work.profile_fresh_until is not None
            and datetime.now(UTC) < work.profile_fresh_until
        )
        profile_first = regular_check and not reuse_fresh_profile
        try:
            stack, reservations = await self._reserve_endpoints_safely(
                endpoints[:1] if profile_first else endpoints
            )
            if profile_first:
                try:
                    return await self._collect_profile_first(
                        work, pool, reservations[0]
                    )
                finally:
                    await _drain_to_thread(stack.close)
            selected_endpoints = endpoints
            selected_reservations = reservations
            if reuse_fresh_profile:
                selected_endpoints = ("battle_log",)
                selected_reservations = (reservations[1],)
            # Reset responses go out one after another, so each battle log is
            # collected after the profile it must cover.
            in_order = lane == "reset"
            tasks = [
                asyncio.create_task(
                    self._collect_endpoint(
                        work,
                        endpoint,
                        lane,
                        pool,
                        reservation=reservation,
                    )
                )
                for endpoint, reservation in zip(
                    selected_endpoints, selected_reservations, strict=True
                )
                if not in_order
            ]
            try:
                outcomes = list(await asyncio.gather(*tasks))
                for endpoint, reservation in zip(selected_endpoints if in_order else (), selected_reservations):
                    outcomes.append(await self._collect_endpoint(work, endpoint, lane, pool, reservation=reservation))
                if (
                    reuse_fresh_profile
                    and work.profile_fresh_until is not None
                    and (
                        outcomes != ["recorded"]
                        or datetime.now(UTC) >= work.profile_fresh_until
                    )
                ):
                    profile_task = asyncio.create_task(
                        self._collect_endpoint(
                            work,
                            "profile",
                            lane,
                            pool,
                            reservation=reservations[0],
                        )
                    )
                    tasks.append(profile_task)
                    outcomes.insert(0, await profile_task)
                return outcomes
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await _drain_awaitable(asyncio.gather(*tasks, return_exceptions=True))
                raise
            finally:
                await _drain_to_thread(stack.close)
        except (OSError, SpoolError) as error:
            self._record_spool_failure(error)
            return ["capacity_paused"] * len(endpoints)

    async def _collect_profile_first(
        self, work: CollectorWork, pool: KeyPool, reservation: Any
    ) -> list[str]:
        """Fetch the profile, then the battle log only when it can have changed."""
        usable: list[bool] = []
        profile = await self._collect_endpoint(
            work, "profile", "ordinary", pool, reservation=reservation, usable=usable
        )
        if profile == "capacity_paused":
            return [profile]
        profile_usable = usable[-1:] == [True]
        if not self.battle_logs.due(
            work.normalized_tag, profile_usable=profile_usable, now=datetime.now(UTC)
        ):
            await self._defer_finished_player(work, profile_usable)
            return [profile]
        stack, reservations = await self._reserve_endpoints_safely(("battle_log",))
        try:
            battle_log = await self._collect_endpoint(
                work, "battle_log", "ordinary", pool, reservation=reservations[0]
            )
        finally:
            await _drain_to_thread(stack.close)
        if battle_log != "capacity_paused":
            await self._defer_finished_player(work, profile_usable)
        return [profile, battle_log]

    async def _defer_finished_player(
        self, work: CollectorWork, profile_usable: bool
    ) -> None:
        """Check a Clasher who finished the Legend day less often until Reset."""
        until = self.battle_logs.finished_recheck_at(
            work.normalized_tag, profile_usable=profile_usable, now=datetime.now(UTC)
        )
        if (
            until is None
            or work.player_id is None
            or work.collector_work_id is not None
        ):
            return
        try:
            await self._database_call(
                self.database.defer_regular_check, work.player_id, until
            )
        except (psycopg.Error, PoolTimeout):
            pass  # The player keeps the normal cadence.

    def _reserve_endpoints(
        self, endpoints: tuple[str, ...]
    ) -> tuple[ExitStack, list[Any]]:
        stack = ExitStack()
        try:
            reservations = [
                stack.enter_context(self.spool.reserve(self.max_body_bytes))
                for _endpoint in endpoints
            ]
        except BaseException:
            stack.close()
            raise
        return stack, reservations

    async def _reserve_endpoints_safely(
        self, endpoints: tuple[str, ...]
    ) -> tuple[ExitStack, list[Any]]:
        task = asyncio.create_task(
            asyncio.to_thread(self._reserve_endpoints, endpoints)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if task.exception() is not None:
                raise asyncio.CancelledError from None
            stack, _reservations = task.result()
            await _drain_to_thread(stack.close)
            raise

    async def collect_rankings(self) -> str:
        return await self._collect_endpoint(
            CollectorWork(None, "global", datetime.now(UTC)),
            "global_player_rankings",
            "ranking",
            self.regular_keys,
        )

    async def collect_intent(self, intent: CollectorIntent) -> str:
        """Run one durable Reset, settlement, interactive, ranking, or discovery job."""
        return await collector_intents.collect_intent(self, intent)

    async def _collect_endpoint(
        self,
        work: CollectorWork,
        endpoint: str,
        lane: str,
        pool: KeyPool,
        *,
        reservation: Any | None = None,
        usable: list[bool] | None = None,
    ) -> str:
        important = (
            lane in {"interactive", "reset"}
            or endpoint == "global_player_rankings"
            or work.eligibility_recheck
        )
        attempts = 3 if important else 1
        pool_name = "interactive" if pool is self.interactive_keys else "regular"
        for attempt in range(attempts):
            if not await self._spool_available():
                return "capacity_paused"
            owned_reservation = reservation is None or attempt > 0
            current_reservation = None
            try:
                current_reservation = (
                    self.spool.reserve(self.max_body_bytes)
                    if owned_reservation
                    else reservation
                )
                if owned_reservation:
                    current_reservation.__enter__()
                started_at = datetime.now(UTC)
                try:
                    try:
                        if endpoint == "global_player_rankings":
                            response = await self.client.fetch_rankings(pool)
                        else:
                            response = await self.client.fetch_player(
                                pool,
                                work.normalized_tag,
                                endpoint,
                                **(
                                    {}
                                    if work.collect_before is None
                                    else {"start_before": work.collect_before}
                                ),
                            )
                    except asyncio.CancelledError as cancelled:
                        # Only cancelling this task stops it; a request's own
                        # cancellation is a retryable failure.
                        if asyncio.current_task().cancelling():
                            raise
                        raise ProviderFailure("request_cancelled", retryable=True) from cancelled
                except CollectionWindowClosed:
                    return "window_closed"
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
                            key_label=getattr(error, "key_label", None) or "unassigned",
                        ),
                    )
                    self._count(error.category)
                    key = (endpoint, pool_name, error.category)
                    self.endpoint_outcomes[key] = self.endpoint_outcomes.get(key, 0) + 1
                    if not error.retryable or attempt + 1 == attempts:
                        return "transient" if error.retryable else "failed"
                    await asyncio.sleep(_retry_delay(attempt))
                    continue
                if (
                    lane == "interactive"
                    and response.http_status == 429
                    and self.interactive_fingerprint is not None
                ):
                    await self._database_call(
                        self.database.cooldown_interactive_key,
                        self.interactive_fingerprint,
                        math.ceil(
                            retry_after_seconds(response.headers.get("retry-after"))
                        ),
                    )
                digest = hashlib.sha256(response.body).hexdigest()
                handoff = self._make_handoff(work, response, digest)
                name, payload = self.serialize_handoff(handoff)
                # Only a known-unchanged sighting may skip the spool: an
                # ordinary, work-free response whose used fields match the ones
                # this process last committed for the same endpoint, checked
                # while no response for its lock stripe is in flight. A crash
                # before its database commit loses only that sighting; the next
                # poll records it again. The check holds no lock, so no other
                # response waits behind it. Every other response (first since
                # restart, changed, reset or work-bound) is saved to the spool
                # before its own database work, as is a known-unchanged one
                # the check does not compact.
                identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
                lock = self._handoff_lock(handoff)
                pending = self._handoff_turns.get(identity)
                committed = self._committed.get(identity)
                cancelled = False
                compacted = False
                if (
                    lane == "ordinary"
                    and handoff.collector_work_id is None
                    and committed is not None
                    and committed[1] == handoff.content_fingerprint
                    and not lock.locked()
                    and collector_commits.settled(pending)
                    and not self._handoff_recovery_required
                ):
                    check = asyncio.ensure_future(
                        asyncio.to_thread(
                            self.database.record_unchanged_response, handoff
                        )
                    )
                    try:
                        await _drain_awaitable(check)
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:  # noqa: BLE001, S110 - the spool handoff keeps it.
                        pass
                    compacted = (
                        not check.cancelled()
                        and check.exception() is None
                        and check.result() is True
                    )
                seen = (handoff.response_completed_at, handoff.content_fingerprint)
                if compacted:
                    self._committed[identity] = max(
                        seen, self._committed.get(identity) or seen
                    )
                else:
                    published = False
                    later: asyncio.Task[None] | None = None
                    turn = asyncio.get_running_loop().create_future()
                    try:
                        async with lock:
                            # A predecessor may have failed after its durable
                            # publish while this response was waiting for the
                            # same stripe. Recovery must run before any
                            # successor can become current.
                            if self._handoff_recovery_required:
                                if cancelled:
                                    raise asyncio.CancelledError
                                return "capacity_paused"
                            await _drain_to_thread(
                                self.spool.publish_handoff,
                                response.body,
                                digest,
                                name,
                                payload,
                                current_reservation,
                            )
                            published = True
                            previous = self._handoff_turns.get(identity)
                            self._handoff_turns[identity] = turn
                        # Saved responses commit in publish order, but no
                        # database wait holds the lock, so a later response
                        # always reaches the spool first.
                        behind = None if previous is None else await asyncio.shield(previous)
                        if self._handoff_recovery_required:
                            if cancelled:
                                raise asyncio.CancelledError
                            return "capacity_paused"
                        committed_now = False
                        if behind is None or behind.done():
                            with suppress(psycopg.errors.LockNotAvailable):
                                await self._commit_saved(handoff, name)
                                committed_now = True
                        if not committed_now:
                            later = collector_commits.commit_later(self, behind, handoff, name)
                    except BaseException as error:
                        if published or self._sidecar_exists(name):
                            self._handoff_recovery_required = True
                            if isinstance(error, (OSError, SpoolError)):
                                raise _HandoffRecoveryRequired(
                                    "durable response handoff requires restart recovery"
                                ) from error
                        raise
                    finally:
                        turn.set_result(later)
                        collector_commits.forget(self, identity, later)
                    if later is not None and work.collector_work_id is not None:
                        await asyncio.wait({later})
                        if not cancelled and (later.cancelled() or later.exception()):
                            return "capacity_paused"
                if cancelled:
                    raise asyncio.CancelledError
                self._count("recorded")
                # Discovery work (ordinary lane with a work row) is not tracked.
                noted = (
                    200 <= response.http_status < 300
                    and (lane != "ordinary" or work.collector_work_id is None)
                    and self.battle_logs.note_response(
                        work.normalized_tag,
                        endpoint,
                        response.body,
                        started_at=response.request_started_at,
                        completed_at=response.response_completed_at,
                    )
                )
                if usable is not None:
                    usable.append(noted)
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
                    return "failed" if response.http_status in {401, 403} else "transient"
                return "recorded"
            except (OSError, SpoolError) as error:
                self._record_spool_failure(error)
                return "capacity_paused"
            finally:
                if owned_reservation and current_reservation is not None:
                    try:
                        current_reservation.__exit__(None, None, None)
                    except (OSError, SpoolError) as error:
                        self._record_spool_failure(error)
        raise AssertionError("unreachable collector retry loop")

    async def _commit_saved(
        self, handoff: ResponseHandoff, name: str, serialized: bool | None = None
    ) -> None:
        record = self.database.record_response if serialized is None else partial(
            self.database.record_recovered_response, serialized=serialized
        )
        await _drain_awaitable(self._database_call(record, handoff))
        identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
        seen = (handoff.response_completed_at, handoff.content_fingerprint)
        self._committed[identity] = max(seen, self._committed.get(identity) or seen)
        await _drain_to_thread(self.spool.remove_handoff, name)

    def held_work(self) -> list[int]:
        """Work whose saved responses still wait to commit; it is not fetched again."""
        return [work for work in self._later_commits.values() if work is not None]

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
            content_fingerprint=content_fingerprint(
                response.endpoint,
                response.body,
                http_status=response.http_status,
                response_hash=digest,
            ),
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

    def _handoff_lock(self, handoff: ResponseHandoff) -> asyncio.Lock:
        identity = (
            f"{handoff.scope}\0{handoff.identity_key}\0{handoff.endpoint}"
        ).encode()
        stripe = int.from_bytes(hashlib.sha256(identity).digest()[:4], "big")
        return self._handoff_locks[stripe % len(self._handoff_locks)]

    def _sidecar_exists(self, name: str) -> bool:
        try:
            return any(item_name == name for item_name, _ in self.spool.iter_handoffs())
        except BaseException as error:
            self._handoff_recovery_required = True
            raise _HandoffRecoveryRequired(
                "response handoff state cannot be inspected safely"
            ) from error

    @staticmethod
    def serialize_handoff(handoff: ResponseHandoff) -> tuple[str, bytes]:
        payload = asdict(handoff)
        payload["handoff_protocol"] = _HANDOFF_PROTOCOL
        payload["request_started_at"] = handoff.request_started_at.isoformat()
        payload["response_completed_at"] = handoff.response_completed_at.isoformat()
        return handoff.occurrence_key, json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @staticmethod
    def deserialize_handoff(payload: bytes) -> ResponseHandoff:
        handoff, _serialized = Collector._deserialize_handoff(payload)
        return handoff

    @staticmethod
    def _deserialize_handoff(payload: bytes) -> tuple[ResponseHandoff, bool]:
        value = json.loads(payload)
        protocol = value.pop("handoff_protocol", None)
        if protocol not in {None, _HANDOFF_PROTOCOL}:
            raise ValueError("unsupported response handoff protocol")
        value["request_started_at"] = datetime.fromisoformat(
            value["request_started_at"]
        )
        value["response_completed_at"] = datetime.fromisoformat(
            value["response_completed_at"]
        )
        # Sidecars written before field compaction replay with the raw digest:
        # they count as changed once, which stores rather than loses them.
        value.setdefault("content_fingerprint", value["response_hash"])
        return ResponseHandoff(**value), protocol == _HANDOFF_PROTOCOL

    def recover_handoffs(self) -> int:
        records = [
            (name, *self._deserialize_handoff(payload))
            for name, payload in self.spool.iter_handoffs()
        ]
        # Responses already applied go first, then the rest in the order they
        # were received, so a later one never compacts away an earlier change
        # or replaces the commit marker of one already applied.
        applied = (
            self.database.applied_occurrence_keys(
                [handoff.occurrence_key for _name, handoff, _serialized in records]
            )
            if records
            else set()
        )
        records.sort(
            key=lambda record: (
                record[1].occurrence_key not in applied,
                record[1].response_completed_at,
            )
        )
        for _name, handoff, _serialized in records:
            if self.spool.verify(handoff.response_hash, handoff.byte_size) is None:
                raise SpoolError("handoff raw response is missing or corrupt")
        recovered = 0
        for index, (name, handoff, serialized) in enumerate(records):
            try:
                self.database.record_recovered_response(handoff, serialized=serialized)
            except psycopg.errors.LockNotAvailable:
                self._unrecovered = records[index:]
                break
            self.spool.remove_handoff(name)
            recovered += 1
        self.spool.remove_unreferenced(self.database.referenced_spool_hashes)
        return recovered

    async def upload_once(self, *, owner: str) -> bool:
        if self.archive is None or self._archive_terminal:
            return False
        # One of the upload owners releases expired leases each half lease.
        release_expired = time.monotonic() >= self._next_upload_release
        if release_expired:
            self._next_upload_release = time.monotonic() + _UPLOAD_LEASE_SECONDS / 2
        claim = await self._upload_database_call(
            collector_uploads.claim_upload,
            self.database,
            owner=owner,
            lease_seconds=_UPLOAD_LEASE_SECONDS,
            release_expired=release_expired,
        )
        if claim is None:
            return False
        renewal_stop = asyncio.Event()
        renewal = asyncio.create_task(self._renew_upload_lease(claim, renewal_stop))
        try:
            config = self.archive.instance_config
            if config is not None and not self._archive_identity_validated:
                if not await self._upload_database_call(
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
            try:
                body = await asyncio.to_thread(
                    self.spool.verify, claim.response_hash, claim.byte_size
                )
            except (OSError, SpoolError) as error:
                self._record_spool_failure(error)
                return True
            if body is None:
                raise ArchiveReadError(
                    "spool_missing",
                    "pending upload has no local raw response",
                    retryable=False,
                )
            await self._ensure_upload_lease(renewal, claim)
            reference = await _drain_to_thread(
                self.archive.write_immutable,
                body,
                claim.response_hash,
                generation=claim.generation or None,
            )
            await self._ensure_upload_lease(renewal, claim)
            await _stop_task(renewal_stop, renewal)
            await _drain_awaitable(
                self._upload_database_call(
                    collector_uploads.complete_upload,
                    self.database,
                    claim,
                    archive_reference=reference,
                    archive_instance_id=self.archive_instance_id,
                )
            )
            self._count("uploaded")
            self.archive_health = "ready"
        except ArchiveReadError as error:
            try:
                await self._ensure_upload_lease(renewal, claim)
                await _stop_task(renewal_stop, renewal)
                await _drain_awaitable(
                    self._upload_database_call(
                        collector_uploads.fail_upload,
                        self.database,
                        claim,
                        category=error.category,
                        detail=str(error),
                        retryable=error.retryable,
                    )
                )
            except collector_uploads.UploadLeaseLost:
                self._count("upload_lease_lost")
                return True
            self._count(error.category)
            self._archive_terminal = error.category in _GLOBAL_ARCHIVE_FAILURES
            self.archive_health = "terminal" if self._archive_terminal else "degraded"
        except collector_uploads.UploadLeaseLost:
            # A competing owner can safely retry: immutable archive writes are
            # content-addressed, and no stale owner reaches the database commit.
            self._count("upload_lease_lost")
        finally:
            renewal_stop.set()
            await _cancel_task(renewal)
        return True

    async def _renew_upload_lease(
        self, claim: collector_uploads.UploadClaim, stop_requested: asyncio.Event
    ) -> None:
        while not stop_requested.is_set():
            try:
                await asyncio.wait_for(
                    stop_requested.wait(), timeout=_UPLOAD_RENEW_INTERVAL
                )
            except TimeoutError:
                await self._upload_database_call(
                    collector_uploads.renew_upload,
                    self.database,
                    claim,
                    lease_seconds=_UPLOAD_LEASE_SECONDS,
                )

    async def _ensure_upload_lease(
        self,
        renewal: asyncio.Task[None],
        claim: collector_uploads.UploadClaim,
    ) -> None:
        if renewal.done():
            await renewal
        await self._upload_database_call(
            collector_uploads.renew_upload,
            self.database,
            claim,
            lease_seconds=_UPLOAD_LEASE_SECONDS,
        )

    def cleanup_uploaded(self, *, limit: int = 100) -> int:
        candidates = self.database.deletable_hashes(limit=limit)
        if not candidates:
            return 0
        with self.spool.delete_unreferenced_batch() as delete:
            return self.database.delete_spool_if_deletable(candidates, delete)

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
        self._stopping = stop_requested
        await _drain_to_thread(self.recover_handoffs)
        collector_commits.commit_unrecovered(self, self._unrecovered)
        await _drain_to_thread(self.spool.cleanup_stale, 60.0)
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
        if self.weekly_eligibility_enabled:
            tasks.append(
                asyncio.create_task(weekly_eligibility.run(self, stop_requested))
            )
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
            outage = getattr(self.client, "provider_outage", None)
            if outage is not None:
                outage.stop()
            await asyncio.gather(*tasks)
            await _drain_to_thread(
                self.spool.remove_unreferenced,
                self.database.referenced_spool_hashes,
            )
        finally:

            async def finish() -> None:
                stop_task.cancel()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_task, *tasks, return_exceptions=True)
                server.close()
                await server.wait_closed()

            await _drain_awaitable(finish())

    async def _regular_loop(
        self, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        pending: dict[asyncio.Task[list[str]], CollectorWork] = {}
        paused_tasks: set[asyncio.Task[list[str]]] = set()
        retry_work: list[CollectorWork] = []
        stop_wait = asyncio.create_task(stop_requested.wait())
        graceful = False
        try:
            while not stop_requested.is_set():
                for task in [task for task in pending if task.done()]:
                    item = pending.pop(task)
                    paused_tasks.discard(task)
                    self.regular_inflight -= 1
                    if "capacity_paused" in task.result():
                        retry_work.append(item)
                if retry_work:
                    await _wait_or_stop(stop_requested, max(1.0, idle_seconds))
                    if stop_requested.is_set():
                        break
                    # Paused work is not in flight, so a Reset can start
                    # meanwhile; it waits for the next pass once admission closes.
                    async with self._regular_admission_lock:
                        if await self._database_call(
                            self.database.regular_admission_open, datetime.now(UTC)
                        ):
                            for item in retry_work:
                                task = asyncio.create_task(
                                    self.collect_player(item, lane="ordinary")
                                )
                                pending[task] = item
                                paused_tasks.add(task)
                            self.regular_inflight += len(retry_work)
                    retry_work.clear()
                    continue
                if paused_tasks:
                    await asyncio.wait(
                        {*paused_tasks, stop_wait},
                        timeout=idle_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    continue
                work: list[CollectorWork] = []
                async with self._regular_admission_lock:

                    def admit(
                        items: list[CollectorWork], total: list[CollectorWork] = work
                    ) -> None:
                        for item in items:
                            pending[
                                asyncio.create_task(
                                    self.collect_player(item, lane="ordinary")
                                )
                            ] = item
                        total.extend(items)
                        self.regular_inflight += len(items)

                    claim_time = datetime.now(UTC)
                    available = self.regular_parallelism - len(pending)
                    repeat_inflight = sum(
                        not item.first_battle_pending for item in pending.values()
                    )
                    # Cold discovery measured 21.21 players/s against 29.27
                    # regular jobs/s. A quarter of the slots keeps overdue
                    # revisits moving until discovery drains.
                    repeat_limit = min(
                        available,
                        max(1, self.regular_parallelism // 4) - repeat_inflight,
                    )
                    if repeat_limit > 0:
                        admit(
                            await self._database_call(
                                self.database.claim_due_players,
                                limit=repeat_limit,
                                now=claim_time,
                                first_battle_pending=False,
                            )
                        )
                    available -= len(work)
                    if available > 0:
                        admit(
                            await self._database_call(
                                self.database.claim_due_players,
                                limit=available,
                                now=claim_time,
                            )
                        )
                if work:
                    continue
                if pending:
                    await asyncio.wait(
                        {*pending, stop_wait},
                        timeout=idle_seconds,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    await _wait_or_stop(stop_requested, idle_seconds)
            graceful = True
        finally:
            stop_wait.cancel()
            if not graceful:
                for task in pending:
                    if not task.done():
                        task.cancel()
            results = await _drain_awaitable(
                asyncio.gather(stop_wait, *pending, return_exceptions=True)
            )
            pending_results = results[1:]
            self.regular_inflight -= len(pending_results)
            if graceful:
                failure = next(
                    (
                        result
                        for result in pending_results
                        if isinstance(result, BaseException)
                    ),
                    None,
                )
                if failure is not None:
                    raise failure

    async def _intent_loop(
        self,
        stop_requested: asyncio.Event,
        rankings_enabled: bool,
        idle_seconds: float,
    ) -> None:
        active: dict[int, tuple[bool, asyncio.Task[str]]] = {}
        scheduled_boundary: datetime | None = None
        next_rankings_at = datetime.min.replace(tzinfo=UTC)
        next_expiry_at = next_rankings_at
        graceful = False
        try:
            while not stop_requested.is_set():
                now = datetime.now(UTC)
                if rankings_enabled and now >= next_rankings_at:
                    await self._database_call(
                        self.database.schedule_rankings_cycle, now
                    )
                    next_rankings_at = _next_five_minute_cycle(now)
                boundary = now.replace(hour=5, minute=0, second=0, microsecond=0)
                if now >= next_expiry_at:
                    expired = await self._database_call(
                        self.database.expire_settlement_checks, now
                    )
                    if expired == 0:
                        closes = boundary - timedelta(days=1) + collector_reset.COLLECTION_WINDOW
                        next_expiry_at = closes if closes > now else closes + timedelta(days=1)
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
                for is_interactive, limit in (
                    (True, 6),
                    (False, _ORDINARY_INTENT_PARALLELISM),
                ):
                    used = sum(
                        kind == is_interactive for kind, _task in active.values()
                    )
                    available = limit - used
                    if available <= 0:
                        continue
                    intents = await self._database_call(
                        self.database.pending_intents,
                        limit=limit,
                        now=now,
                        interactive=is_interactive,
                        held=self.held_work(),
                    )
                    for intent in intents:
                        if intent.work_id is not None and intent.work_id not in active:
                            active[intent.work_id] = (
                                is_interactive,
                                asyncio.create_task(self.collect_intent(intent)),
                            )
                            available -= 1
                            if available == 0:
                                break
                await _wait_or_stop(stop_requested, idle_seconds)
            graceful = True
        finally:
            if not graceful:
                for _interactive, task in active.values():
                    if not task.done():
                        task.cancel()
            if active:
                results = await _drain_awaitable(
                    asyncio.gather(
                        *(task for _interactive, task in active.values()),
                        return_exceptions=True,
                    )
                )
                if graceful:
                    failure = next(
                        (
                            result
                            for result in results
                            if isinstance(result, BaseException)
                        ),
                        None,
                    )
                    if failure is not None:
                        raise failure

    async def _upload_loop(
        self, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        owners = [
            f"python-collector-{uuid4()}" for _index in range(_UPLOAD_CONCURRENCY)
        ]
        owner_tasks = {
            owner: asyncio.create_task(
                self._upload_owner_loop(owner, stop_requested, idle_seconds)
            )
            for owner in owners
        }
        last_sweep = 0.0
        graceful = False
        try:
            while not stop_requested.is_set():
                for owner, task in list(owner_tasks.items()):
                    if task.done():
                        await task
                        owner_tasks[owner] = asyncio.create_task(
                            self._upload_owner_loop(owner, stop_requested, idle_seconds)
                        )
                if self._spool_io_failed:
                    await _wait_or_stop(stop_requested, max(1.0, idle_seconds))
                    continue
                deleted = 0
                try:
                    deleted = await asyncio.to_thread(
                        self.cleanup_uploaded, limit=_CLEANUP_BATCH_SIZE
                    )
                    # Compacted responses leave no upload row or observation, so
                    # their spool bytes are unreferenced. Sweep them under the
                    # cleanup barrier; the referenced set is evaluated inside it so
                    # sidecars still in flight and freshly committed rows protect.
                    # The sweep walks the whole spool, so it is paced per minute,
                    # not per loop.
                    if time.monotonic() - last_sweep >= 60.0:
                        last_sweep = time.monotonic()
                        # Startup leaves crash debris younger than its 60-second
                        # safety window alone. Revisit it here so an immediate
                        # restart cannot leave that temporary file forever.
                        await asyncio.to_thread(self.spool.cleanup_stale, 60.0)
                        await asyncio.to_thread(
                            self.spool.remove_unreferenced,
                            self.database.referenced_spool_hashes,
                        )
                except (OSError, SpoolError) as error:
                    self._record_spool_failure(error)
                if self._spool_capacity_failed:
                    # Uploads and deletion continue while collection is
                    # paused, so archived bytes can make room for this probe.
                    await self._spool_available()
                await _wait_or_stop(
                    stop_requested,
                    idle_seconds
                    if deleted == _CLEANUP_BATCH_SIZE
                    else max(1.0, idle_seconds),
                )
            await asyncio.gather(*owner_tasks.values())
            try:
                await _drain_to_thread(self.cleanup_uploaded, limit=_UPLOAD_CONCURRENCY)
            except (OSError, SpoolError) as error:
                self._record_spool_failure(error)
            graceful = True
        finally:
            if not graceful:
                for task in owner_tasks.values():
                    if not task.done():
                        task.cancel()
            if owner_tasks:
                await _drain_awaitable(
                    asyncio.gather(*owner_tasks.values(), return_exceptions=True)
                )

    async def _upload_owner_loop(
        self, owner: str, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        while not stop_requested.is_set():
            if self._spool_io_failed:
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))
                continue
            if not await self.upload_once(owner=owner):
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))

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
        if self._handoff_recovery_required:
            ready, reason = False, "handoff_recovery_required"
        elif self._spool_io_failed:
            ready, reason = False, "spool_io_failure"
        elif self._spool_capacity_failed:
            ready, reason = False, "degraded_capacity"
        else:
            try:
                ready, reason = await asyncio.to_thread(self.spool.readiness)
            except (OSError, SpoolError) as error:
                self._record_spool_failure(error)
                ready, reason = False, "spool_io_failure"
            if reason.startswith("storage_error:"):
                self._spool_io_failed = True
                self._count("spool_io_failure")
                ready, reason = False, "spool_io_failure"
        regular = self.regular_keys.health()
        interactive = self.interactive_keys.health()
        if path == "/readyz":
            # Keep /metrics row counts out of readiness: they can exceed the
            # container health-check deadline even when collection can continue.
            try:
                await asyncio.to_thread(self._ping_database)
            except psycopg.Error:
                return 503, "text/plain", b"database_unavailable\n"
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
        try:
            async with self._metrics_lock:
                if time.monotonic() >= self._metrics_refresh_after:
                    self._database_metrics = await asyncio.to_thread(
                        self.database.health_metrics
                    )
                    self._metrics_refresh_after = time.monotonic() + 30
                database_metrics = self._database_metrics
        except psycopg.Error:
            return 503, "text/plain", b"database_unavailable\n"
        stats = None
        if not self._spool_io_failed:
            try:
                stats = await asyncio.to_thread(self.spool.stats)
            except (OSError, SpoolError) as error:
                self._record_spool_failure(error)
        lines = [
            f'clashlens_collector_keys_healthy{{pool="regular"}} {regular["healthy"]}',
            f'clashlens_collector_keys_healthy{{pool="interactive"}} {interactive["healthy"]}',
            f"clashlens_collector_regular_inflight {self.regular_inflight}",
            f"clashlens_collector_spool_io_failed {int(self._spool_io_failed)}",
            f"clashlens_collector_spool_capacity_failed {int(self._spool_capacity_failed)}",
            (
                "clashlens_collector_handoff_recovery_required "
                f"{int(self._handoff_recovery_required)}"
            ),
            f'clashlens_collector_archive_health{{state="{self.archive_health}"}} 1',
        ]
        for pool_name, pool in (
            ("regular", self.regular_keys),
            ("interactive", self.interactive_keys),
        ):
            for label, healthy, paused, starts in pool.key_health():
                key = f'{{pool="{pool_name}",key="{label}"}}'
                lines += [
                    f"clashlens_collector_key_healthy{key} {int(healthy)}",
                    f"clashlens_collector_key_paused{key} {int(paused)}",
                    (
                        "clashlens_collector_key_rate_limit_per_second"
                        f"{key} {pool.starts_per_second}"
                    ),
                    f"clashlens_collector_key_requests_started_total{key} {starts}",
                ]
        if stats is not None:
            lines.extend(
                [
                    f"clashlens_spool_bytes {stats['final_bytes']}",
                    f"clashlens_spool_objects {stats['final_objects']}",
                    f"clashlens_spool_reserved_bytes {stats['reserved_bytes']}",
                    f"clashlens_spool_free_bytes {stats['free_bytes']}",
                    f"clashlens_spool_free_inodes {stats['free_inodes']}",
                ]
            )
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

    def _ping_database(self) -> None:
        # PoolTimeout is a psycopg.Error, so a pool stuck for 2 s is unready.
        with self.database.pool.connection(timeout=2.0) as connection:
            connection.execute("SELECT 1")

    def _count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1

    def _record_spool_failure(self, error: OSError | SpoolError) -> None:
        capacity_error = (
            isinstance(error, SpoolError)
            and str(error).startswith("degraded_capacity:")
        ) or (
            isinstance(error, OSError) and error.errno in {errno.EDQUOT, errno.ENOSPC}
        )
        if capacity_error:
            self._spool_capacity_failed = True
            self._count("degraded_capacity")
            return
        # An I/O or integrity failure is not known to be safe just because the
        # next capacity probe succeeds. Keep collection paused and readiness
        # failed until the service restarts and startup recovery rechecks the
        # spool before admitting another request.
        self._spool_io_failed = True
        self._count("spool_io_failure")

    async def _spool_available(self) -> bool:
        if self._handoff_recovery_required:
            return False
        if self._spool_io_failed:
            return False
        if not self._spool_capacity_failed:
            return True
        async with self._spool_recovery_lock:
            if not self._spool_capacity_failed:
                return True
            if time.monotonic() < self._spool_probe_after:
                return False
            try:
                await asyncio.to_thread(self.spool.probe_writable, self.max_body_bytes)
            except (OSError, SpoolError) as error:
                self._spool_probe_after = time.monotonic() + 1.0
                self._record_spool_failure(error)
                return False
            if self._spool_io_failed:
                return False
            self._spool_capacity_failed = False
            self._spool_probe_after = 0.0
            self._count("capacity_recovered")
            return True


def _retry_delay(attempt: int) -> float:
    return min(5.0, 0.25 * 2**attempt) + random.uniform(0.0, 0.1)


async def _drain_to_thread(operation: Any, *args: Any, **kwargs: Any) -> Any:
    return await _drain_awaitable(asyncio.to_thread(operation, *args, **kwargs))


async def _drain_awaitable(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation cannot stop a running thread or synchronous database
            # call. Keep ownership until it reaches a safe endpoint, even if
            # the owner receives a second cancellation while draining.
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class _HandoffRecoveryRequired(RuntimeError):
    pass


async def _stop_task(stop_requested: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop_requested.set()
    await task


async def _cancel_task(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _wait_or_stop(stop_requested: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_requested.wait(), seconds)
    except TimeoutError:
        pass


def _next_five_minute_cycle(now: datetime) -> datetime:
    rounded = now.astimezone(UTC).replace(second=0, microsecond=0)
    return rounded + timedelta(minutes=5 - rounded.minute % 5)
