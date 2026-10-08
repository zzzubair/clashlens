"""Archive uploads, in their own process beside the collector.

The collector saves each raw response to the spool and records it; this
process copies saved responses to the archive. Uploads used to run inside the
collector, on its threads and its database connections. After the 8 October
2026 Reset they fell from about 1,300 a minute to 145-300 a minute for two
hours while the worker loaded the database, and raw responses waited up to
76.5 minutes for their archive copy. Here uploads have their own process,
threads and four database connections, make two database calls each instead
of four, and record how long each step takes, so a slowdown shows where the
time goes.

``UploaderProcess`` runs inside the collector. It starts this process with the
collector's own settings, starts it again if it exits or stops reporting, and
shows its reports on the collector's ``/metrics``. The process stops when the
collector stops it or goes away.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import queue
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import psycopg
from psycopg_pool import PoolTimeout

from . import collector_uploads
from .archive import ArchiveReadError, immutable_reference
from .collector import (
    _cancel_task,
    _drain_awaitable,
    _drain_to_thread,
    _retry_delay,
    _stop_task,
    _wait_or_stop,
)
from .collector_db import CollectorDatabase
from .spool import SpoolError
from .worker import StageMetrics

UPLOADS_AT_ONCE = 16
DATABASE_CONNECTIONS = 4
LEASE_SECONDS = 60
RENEW_INTERVAL = 20.0
# A step renews the lease only when less than this is left. Completing or
# failing an upload checks the lease itself, so a lost one is never recorded.
RENEW_BELOW_SECONDS = 40.0
REPORT_SECONDS = 5.0
LOG_SECONDS = 60.0
# The process reports every few seconds; this long without a report means it
# is stuck, so it is stopped and started again.
SILENT_SECONDS = 120.0
STOP_SECONDS = 30.0
RESTART_SECONDS = 1.0
_GLOBAL_ARCHIVE_FAILURES = {
    "archive_configuration_error",
    "archive_marker_mismatch",
    "archive_permission_denied",
    "archive_reference_mismatch",
    "archive_unsupported",
}


class Uploader:
    """Copies saved responses to the archive, one upload per owner at a time."""

    def __init__(
        self,
        *,
        database: Any,
        spool: Any,
        archive: Any,
        archive_instance_id: str,
    ) -> None:
        self.database = database
        self.spool = spool
        self.archive = archive
        self.archive_instance_id = archive_instance_id
        self.stages = StageMetrics()
        self.outcomes: dict[str, int] = {}
        self.archive_health = "unconfigured" if archive is None else "unknown"
        self.spool_io_failed = False
        self._archive_terminal = False
        self._archive_identity_validated = False
        self._next_release = 0.0
        self._database_slots = asyncio.Semaphore(DATABASE_CONNECTIONS)

    def snapshot(self) -> dict[str, Any]:
        return {
            "archive_health": self.archive_health,
            "spool_io_failed": self.spool_io_failed,
            "outcomes": dict(self.outcomes),
            "stages": self.stages.snapshot(),
        }

    async def run(self, stop_requested: asyncio.Event, idle_seconds: float = 0.1) -> None:
        owners = [f"python-uploader-{uuid4()}" for _index in range(UPLOADS_AT_ONCE)]
        owner_tasks = {
            owner: asyncio.create_task(
                self._owner_loop(owner, stop_requested, idle_seconds)
            )
            for owner in owners
        }
        graceful = False
        try:
            while not stop_requested.is_set():
                for owner, task in list(owner_tasks.items()):
                    if task.done():
                        await task
                        owner_tasks[owner] = asyncio.create_task(
                            self._owner_loop(owner, stop_requested, idle_seconds)
                        )
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))
            await asyncio.gather(*owner_tasks.values())
            graceful = True
        finally:
            if not graceful:
                for task in owner_tasks.values():
                    if not task.done():
                        task.cancel()
            await _drain_awaitable(
                asyncio.gather(*owner_tasks.values(), return_exceptions=True)
            )

    async def _owner_loop(
        self, owner: str, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        while not stop_requested.is_set():
            if self.spool_io_failed:
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))
                continue
            if not await self.upload_once(owner=owner):
                await _wait_or_stop(stop_requested, max(1.0, idle_seconds))

    async def upload_once(self, *, owner: str) -> bool:
        if self.archive is None or self._archive_terminal:
            return False
        if time.monotonic() >= self._next_release:
            # One owner returns expired leases each half lease.
            self._next_release = time.monotonic() + LEASE_SECONDS / 2
            await self._database_call(
                "release", collector_uploads.release_expired_uploads, self.database
            )
        claimed_at = time.monotonic()
        claim = await self._database_call(
            "claim",
            collector_uploads.claim_upload,
            self.database,
            owner=owner,
            lease_seconds=LEASE_SECONDS,
        )
        if claim is None:
            return False
        # Counted from before the claim, so never later than the real expiry.
        lease = {"until": claimed_at + LEASE_SECONDS}
        renewal_stop = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease(claim, lease, renewal_stop))
        wrote = False
        try:
            config = self.archive.instance_config
            if config is not None and not self._archive_identity_validated:
                if not await self._database_call(
                    "validate", self.database.validate_archive_instance, config
                ):
                    raise ArchiveReadError(
                        "archive_configuration_error",
                        "archive configuration contradicts PostgreSQL",
                        retryable=False,
                    )
                self._archive_identity_validated = True
            marker_health = await self._timed(
                "marker_check", self.archive.check_marker_health
            )
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
                body = await self._timed(
                    "spool_read", self.spool.verify, claim.response_hash, claim.byte_size
                )
            except (OSError, SpoolError):
                # Not known to be safe until a restart rechecks the spool. The
                # claim's lease runs out and another attempt takes it later.
                self.spool_io_failed = True
                self._count("spool_io_failure")
                return True
            if body is None:
                reference = await self._archived_copy(claim)
            else:
                await self._keep_lease(renewal, claim, lease)
                wrote = True
                reference = await self._timed(
                    "archive_write",
                    self.archive.write_immutable,
                    body,
                    claim.response_hash,
                    generation=claim.generation or None,
                )
            await self._keep_lease(renewal, claim, lease)
            await _stop_task(renewal_stop, renewal)
            await _drain_awaitable(
                self._database_call(
                    "complete",
                    collector_uploads.complete_upload,
                    self.database,
                    claim,
                    archive_reference=reference,
                    archive_instance_id=self.archive_instance_id,
                )
            )
            self._count("uploaded")
            self.stages.record("upload_total", time.monotonic() - claimed_at)
            self.archive_health = "ready"
        except ArchiveReadError as error:
            detail = str(error)
            if claim.unresolved_write is not None and not wrote:
                detail = collector_uploads.unresolved_write_detail(
                    claim.unresolved_write, detail
                )
            try:
                await self._keep_lease(renewal, claim, lease)
                await _stop_task(renewal_stop, renewal)
                await _drain_awaitable(
                    self._database_call(
                        "fail",
                        collector_uploads.fail_upload,
                        self.database,
                        claim,
                        category=error.category,
                        detail=detail,
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

    async def _archived_copy(self, claim: collector_uploads.UploadClaim) -> str:
        """Use the archive's copy when the saved one is gone.

        A database restored to before an upload finished forgets that upload,
        and spool cleanup may already have removed the saved copy. The bytes
        are then at this upload's own location; reading them back checks their
        hash. Only bytes the archive does not hold are missing proof, and not
        while the last attempt's write may yet land.
        """
        reference = immutable_reference(
            self.archive.bucket, claim.response_hash, claim.generation or None
        )
        try:
            await self._timed(
                "archive_read", self.archive.read_verified, reference, claim.response_hash
            )
        except ArchiveReadError as error:
            if error.category == "archive_missing" and claim.unresolved_write is None:
                raise ArchiveReadError(
                    "spool_missing",
                    "pending upload has no local raw response and no archived copy",
                    retryable=False,
                ) from error
            raise
        self._count("archived_copy_found")
        return reference

    async def _renew_lease(
        self,
        claim: collector_uploads.UploadClaim,
        lease: dict[str, float],
        stop_requested: asyncio.Event,
    ) -> None:
        while not stop_requested.is_set():
            try:
                await asyncio.wait_for(stop_requested.wait(), timeout=RENEW_INTERVAL)
            except TimeoutError:
                await self._renew(claim, lease)

    async def _keep_lease(
        self,
        renewal: asyncio.Task[None],
        claim: collector_uploads.UploadClaim,
        lease: dict[str, float],
    ) -> None:
        if renewal.done():
            await renewal
        if lease["until"] - time.monotonic() < RENEW_BELOW_SECONDS:
            await self._renew(claim, lease)

    async def _renew(
        self, claim: collector_uploads.UploadClaim, lease: dict[str, float]
    ) -> None:
        renewed_at = time.monotonic()
        await self._database_call(
            "renew",
            collector_uploads.renew_upload,
            self.database,
            claim,
            lease_seconds=LEASE_SECONDS,
        )
        lease["until"] = renewed_at + LEASE_SECONDS

    async def _database_call(
        self, step: str, operation: Any, *args: Any, **kwargs: Any
    ) -> Any:
        started = time.monotonic()
        try:
            async with self._database_slots:
                self.stages.record("upload_database_wait", time.monotonic() - started)
                return await _drain_awaitable(
                    self._retrying(operation, *args, **kwargs)
                )
        finally:
            self.stages.record(f"upload_{step}", time.monotonic() - started)

    async def _retrying(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
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

    async def _timed(self, step: str, operation: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return await _drain_to_thread(operation, *args, **kwargs)
        finally:
            self.stages.record(f"upload_{step}", time.monotonic() - started)

    def _count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class UploaderProcess:
    """Keeps the uploads process running for the collector."""

    def __init__(self, arguments: Any) -> None:
        self.arguments = arguments
        self.report: dict[str, Any] = {}
        self.restarts = 0
        self.running = False
        self._reported_at: float | None = None

    async def run(self, collector: Any, stop_requested: asyncio.Event) -> None:
        delay = RESTART_SECONDS / 2
        while not stop_requested.is_set():
            started = time.monotonic()
            event: dict[str, Any] = {"event": "uploader_restart"}
            try:
                event["exit_code"] = await self._run_once(collector, stop_requested)
            except Exception as error:  # noqa: BLE001 - restart, never stop collection
                event["error"] = type(error).__name__
            if stop_requested.is_set():
                return
            self.restarts += 1
            # One that ran a while starts again soon; one that keeps failing
            # waits twice as long each time, up to a minute.
            ran_a_while = time.monotonic() - started >= 60
            delay = RESTART_SECONDS if ran_a_while else min(60.0, delay * 2)
            print(json.dumps({**event, "wait_seconds": delay}), flush=True)
            await _wait_or_stop(stop_requested, delay)

    async def _run_once(
        self, collector: Any, stop_requested: asyncio.Event
    ) -> int | None:
        """Run the process until it exits, goes silent or the collector stops."""
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=run_process,
            args=(self.arguments, sender),
            name="clashlens-uploader",
            daemon=True,
        )
        loop = asyncio.get_running_loop()
        reports: asyncio.Queue[bytes | None] = asyncio.Queue()

        def readable() -> None:
            try:
                reports.put_nowait(receiver.recv_bytes())
            except (EOFError, OSError):
                # The process ended: its end of the pipe closed.
                loop.remove_reader(receiver.fileno())
                reports.put_nowait(None)

        stopping = asyncio.create_task(stop_requested.wait())
        try:
            process.start()
            sender.close()
            loop.add_reader(receiver.fileno(), readable)
            self.running = True
            while True:
                report = asyncio.create_task(reports.get())
                done, _pending = await asyncio.wait(
                    {report, stopping},
                    timeout=SILENT_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if report not in done:
                    report.cancel()
                    break
                message = report.result()
                if message is None:
                    break
                self._apply(message, collector)
        finally:
            self.running = False
            stopping.cancel()
            loop.remove_reader(receiver.fileno())
            sender.close()
            await self._end(process)
            receiver.close()
        return process.exitcode

    async def _end(self, process: Any) -> None:
        if process.pid is None:
            return
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, STOP_SECONDS)
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 5.0)

    def _apply(self, message: bytes, collector: Any) -> None:
        try:
            report = json.loads(message)
        except ValueError:
            return
        if not isinstance(report, dict):
            return
        self.report = report
        self._reported_at = time.monotonic()
        collector.archive_health = str(report.get("archive_health", "unknown"))
        if report.get("spool_io_failed") and not collector._spool_io_failed:
            # The same pause a failed spool read in the collector causes.
            collector._spool_io_failed = True
            collector._count("spool_io_failure")

    def metric_lines(self) -> list[str]:
        lines = [
            f"clashlens_uploader_running {int(self.running)}",
            f"clashlens_uploader_restarts_total {self.restarts}",
        ]
        if self._reported_at is not None:
            age = time.monotonic() - self._reported_at
            lines.append(f"clashlens_uploader_report_age_seconds {age:.3f}")
        for outcome, count in sorted(self.report.get("outcomes", {}).items()):
            lines.append(f'clashlens_uploader_uploads_total{{outcome="{outcome}"}} {count}')
        for stage, values in sorted(self.report.get("stages", {}).items()):
            labels = f'step="{stage.removeprefix("upload_")}"'
            lines += [
                f"clashlens_uploader_step_seconds_sum{{{labels}}} {values['elapsed_seconds']:.6f}",
                f"clashlens_uploader_step_seconds_count{{{labels}}} {values['count']}",
            ]
            if values.get("p95_upper_ms") is not None:
                lines.append(
                    f"clashlens_uploader_step_p95_upper_ms{{{labels}}} {values['p95_upper_ms']}"
                )
        return lines


def run_process(arguments: Any, sender: Any) -> None:
    """The uploads process: run until stopped or the collector goes away."""
    from .cli import _archive, _database_url

    database = CollectorDatabase(_database_url(arguments), max_size=DATABASE_CONNECTIONS)
    reader = None
    try:
        reader = _archive(
            arguments, pool_size=UPLOADS_AT_ONCE, validate_archive_instance=False
        )
        uploader = Uploader(
            database=database,
            spool=reader.spool,
            archive=reader.archive,
            archive_instance_id=arguments.archive_instance_id,
        )
        reports: queue.Queue[bytes] = queue.Queue(maxsize=1)
        # A daemon thread sends, so a collector too busy to read never stalls
        # uploads, and a send still waiting never holds up the exit.
        threading.Thread(
            target=_send_reports, args=(reports, sender), daemon=True
        ).start()
        asyncio.run(_serve(uploader, reports))
    finally:
        if reader is not None:
            reader.spool.close()
        database.close()


async def _serve(uploader: Uploader, reports: queue.Queue[bytes]) -> None:
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(
            max_workers=UPLOADS_AT_ONCE + DATABASE_CONNECTIONS,
            thread_name_prefix="uploader",
        )
    )
    stop_requested = asyncio.Event()
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(shutdown_signal, stop_requested.set)
    parent = multiprocessing.parent_process()
    if parent is not None:
        # Readable once the collector has gone, however it ended.
        loop.add_reader(parent.sentinel, stop_requested.set)
    reporter = asyncio.create_task(_report(uploader, reports, stop_requested))
    try:
        await uploader.run(stop_requested)
    finally:
        stop_requested.set()
        await reporter


async def _report(
    uploader: Uploader, reports: queue.Queue[bytes], stop_requested: asyncio.Event
) -> None:
    next_log = 0.0
    while True:
        snapshot = uploader.snapshot()
        # Only the newest report is worth sending.
        try:
            reports.get_nowait()
        except queue.Empty:
            pass
        reports.put_nowait(json.dumps(snapshot).encode())
        if time.monotonic() >= next_log:
            print(json.dumps({"event": "uploader_health", **snapshot}), flush=True)
            next_log = time.monotonic() + LOG_SECONDS
        if stop_requested.is_set():
            return
        await _wait_or_stop(stop_requested, REPORT_SECONDS)


def _send_reports(reports: queue.Queue[bytes], sender: Any) -> None:
    while True:
        message = reports.get()
        try:
            sender.send_bytes(message)
        except OSError:
            return
