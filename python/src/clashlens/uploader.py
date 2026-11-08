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
from enum import StrEnum
from typing import Any
from uuid import uuid4

from . import collector_uploads
from .archive import ArchiveReadError, failure_evidence, immutable_reference
from .async_runtime import (
    cancel_task,
    drain_awaitable,
    drain_to_thread,
    retry_database,
    stop_task,
    wait_or_stop,
)
from .collector_db import CollectorDatabase
from .spool import SpoolError
from .stage_metrics import StageMetrics

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


class ArchiveHealth(StrEnum):
    """What the last upload to finish says about the archive.

    - ``UNCONFIGURED``: no archive is set; nothing is ever uploaded.
    - ``UNKNOWN``: no upload has finished since this process started.
    - ``READY``: the last upload to finish succeeded.
    - ``DEGRADED``: the last to finish failed in a way that may pass, and the
      failed upload is due again in 5 s; or the archive marker could not be
      checked, and nothing is claimed until the shared re-check passes.
    - ``TERMINAL``: the last to finish failed in a way no retry fixes
      (configuration, permission or marker); see ``UploadGate.ARCHIVE_STOPPED``.

    ``Uploader._record_outcome`` is the only place it moves. It is reported to
    the collector every REPORT_SECONDS and resets when the process restarts.
    """

    UNCONFIGURED = "unconfigured"
    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    TERMINAL = "terminal"


class UploadGate(StrEnum):
    """Whether an owner may claim an upload; ``Uploader.gate`` derives it.

    - ``OPEN``: claim the next due upload.
    - ``NO_ARCHIVE``: no archive is set.
    - ``ARCHIVE_STOPPED``: the last failure was terminal. Owners stop claiming
      until the process restarts, except that a later retryable failure from an
      upload already in flight reopens it.
    - ``SPOOL_FAILED``: a saved response could not be read. Not known to be
      safe until a restart rechecks the spool; the collector pauses too.

    The first claim also checks the archive's identity against PostgreSQL once
    per process; a mismatch is a terminal failure. Owners that may not claim
    wait a second and ask again.
    """

    OPEN = "open"
    NO_ARCHIVE = "no_archive"
    ARCHIVE_STOPPED = "archive_stopped"
    SPOOL_FAILED = "spool_failed"


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
        self.archive_health = (
            ArchiveHealth.UNCONFIGURED if archive is None else ArchiveHealth.UNKNOWN
        )
        self.spool_io_failed = False
        # The latest archive failure's failure_evidence, kept after recovery.
        self.archive_failure: dict[str, Any] | None = None
        # Whether the last failure to finish was terminal. Kept apart from
        # archive_health: a success finishing after it does not reopen claims.
        self._last_failure_terminal = False
        self._archive_identity_validated = False
        self._next_release = 0.0
        self._database_slots = asyncio.Semaphore(DATABASE_CONNECTIONS)

    def gate(self) -> UploadGate:
        if self.archive is None:
            return UploadGate.NO_ARCHIVE
        if self._last_failure_terminal:
            return UploadGate.ARCHIVE_STOPPED
        if self.spool_io_failed:
            return UploadGate.SPOOL_FAILED
        return UploadGate.OPEN

    def _record_outcome(self, failure: str | None, *, marker: bool = False) -> None:
        """``marker``: the archive's marker could not be checked, which says
        nothing about an upload, so an earlier terminal failure still stops
        claims."""
        if failure is None:
            self.archive_health = ArchiveHealth.READY
            return
        terminal = failure in _GLOBAL_ARCHIVE_FAILURES
        if not marker:
            self._last_failure_terminal = terminal
        self.archive_health = (
            ArchiveHealth.TERMINAL if terminal else ArchiveHealth.DEGRADED
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "archive_health": self.archive_health,
            "spool_io_failed": self.spool_io_failed,
            "archive_failure": self.archive_failure,
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
                await wait_or_stop(stop_requested, max(1.0, idle_seconds))
            await asyncio.gather(*owner_tasks.values())
            graceful = True
        finally:
            if not graceful:
                for task in owner_tasks.values():
                    if not task.done():
                        task.cancel()
            await drain_awaitable(
                asyncio.gather(*owner_tasks.values(), return_exceptions=True)
            )

    async def _owner_loop(
        self, owner: str, stop_requested: asyncio.Event, idle_seconds: float
    ) -> None:
        while not stop_requested.is_set():
            if self.gate() is UploadGate.SPOOL_FAILED:
                await wait_or_stop(stop_requested, max(1.0, idle_seconds))
                continue
            if not await self.upload_once(owner=owner):
                await wait_or_stop(stop_requested, max(1.0, idle_seconds))

    async def upload_once(self, *, owner: str) -> bool:
        if self.gate() in {UploadGate.NO_ARCHIVE, UploadGate.ARCHIVE_STOPPED}:
            return False
        if time.monotonic() >= self._next_release:
            # One owner returns expired leases each half lease.
            self._next_release = time.monotonic() + LEASE_SECONDS / 2
            await self._database_call(
                "release", collector_uploads.release_expired_uploads, self.database
            )
        # While the archive's marker cannot be checked nothing is claimed. Each
        # owner waits for the one shared re-check instead of claiming saved
        # responses only to fail them: one failed check used to fail every
        # claim for five minutes, each failed response due again five seconds
        # later.
        marker_health = await self._timed(
            "marker_check", self.archive.check_marker_health
        )
        if marker_health == "degraded":
            self._record_outcome("archive_unavailable", marker=True)
            self.archive_failure = getattr(self.archive, "marker_failure", None)
            return False
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
                    error = ArchiveReadError(
                        "archive_configuration_error",
                        "archive configuration contradicts PostgreSQL",
                        retryable=False,
                    )
                    error.stage = "validate"
                    raise error
                self._archive_identity_validated = True
            if marker_health == "terminal":
                error = ArchiveReadError(
                    "archive_configuration_error",
                    "archive marker validation failed",
                    retryable=False,
                )
                error.stage = "marker_check"
                raise error
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
            await stop_task(renewal_stop, renewal)
            await drain_awaitable(
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
            self._record_outcome(None)
        except ArchiveReadError as error:
            detail = str(error)
            if claim.unresolved_write is not None and not wrote:
                detail = claim.unresolved_write.failure_detail(detail)
            try:
                await self._keep_lease(renewal, claim, lease)
                await stop_task(renewal_stop, renewal)
                await drain_awaitable(
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
            self.archive_failure = failure_evidence(error.stage or "upload", error)
            self._record_outcome(error.category)
        except collector_uploads.UploadLeaseLost:
            # A competing owner can safely retry: immutable archive writes are
            # content-addressed, and no stale owner reaches the database commit.
            self._count("upload_lease_lost")
        finally:
            renewal_stop.set()
            await cancel_task(renewal)
        return True

    async def _archived_copy(self, claim: collector_uploads.UploadClaim) -> str:
        """Use the archive's copy when the saved one is gone.

        A database restored to before an upload finished forgets that upload,
        and spool cleanup may already have removed the saved copy. The bytes
        are then at this upload's own location; reading them back checks their
        hash. Only bytes the archive does not hold are missing proof, and not
        on a first attempt or while an earlier attempt's write may yet land.
        """
        reference = immutable_reference(
            self.archive.bucket, claim.response_hash, claim.generation or None
        )
        try:
            await self._timed(
                "archive_read", self.archive.read_verified, reference, claim.response_hash
            )
        except ArchiveReadError as error:
            if error.category == "archive_missing" and claim.missing_copy_is_proof():
                missing = ArchiveReadError(
                    "spool_missing",
                    "pending upload has no local raw response and no archived copy",
                    retryable=False,
                )
                missing.stage = error.stage
                raise missing from error
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
                return await drain_awaitable(
                    retry_database(operation, *args, on_failure=self._count, **kwargs)
                )
        finally:
            self.stages.record(f"upload_{step}", time.monotonic() - started)

    async def _timed(self, step: str, operation: Any, *args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return await drain_to_thread(operation, *args, **kwargs)
        except ArchiveReadError as error:
            error.stage = error.stage or step
            raise
        finally:
            self.stages.record(f"upload_{step}", time.monotonic() - started)

    def _count(self, outcome: str) -> None:
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1


class UploaderProcessState(StrEnum):
    """Where the uploads process stands; ``UploaderProcess`` alone moves it.

    - ``STOPPED``: no process runs: before ``run`` and once the collector stops.
    - ``STARTING``: a new process is being spawned.
    - ``RUNNING``: it runs and its reports are read.
    - ``ENDING``: it exited, sent nothing for SILENT_SECONDS, or the collector
      is stopping: terminate it, wait up to STOP_SECONDS, then kill it.
    - ``RESTART_WAIT``: waiting to start it again (``restart_delay``).

    A start that fails ends the same way as a process that exits. Only a
    collector stop leaves this cycle; uploads never stop collection.
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ENDING = "ending"
    RESTART_WAIT = "restart_wait"


def restart_delay(previous: float, ran_seconds: float) -> float:
    """One that ran a while starts again soon; one that keeps failing waits
    twice as long each time, up to a minute."""
    return RESTART_SECONDS if ran_seconds >= 60 else min(60.0, previous * 2)


class UploaderProcess:
    """Keeps the uploads process running for the collector."""

    def __init__(self, arguments: Any) -> None:
        self.arguments = arguments
        self.report: dict[str, Any] = {}
        self.restarts = 0
        self.state = UploaderProcessState.STOPPED
        self._reported_at: float | None = None

    async def run(self, collector: Any, stop_requested: asyncio.Event) -> None:
        delay = RESTART_SECONDS / 2
        try:
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
                delay = restart_delay(delay, time.monotonic() - started)
                print(json.dumps({**event, "wait_seconds": delay}), flush=True)
                self.state = UploaderProcessState.RESTART_WAIT
                await wait_or_stop(stop_requested, delay)
        finally:
            self.state = UploaderProcessState.STOPPED

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
        self.state = UploaderProcessState.STARTING
        try:
            process.start()
            sender.close()
            loop.add_reader(receiver.fileno(), readable)
            self.state = UploaderProcessState.RUNNING
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
            self.state = UploaderProcessState.ENDING
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
        if report.get("spool_io_failed") and not collector.spool_health.io_failed:
            # The same pause a failed spool read in the collector causes.
            collector.spool_health.record_io_failure()

    def metric_lines(self) -> list[str]:
        lines = [
            f"clashlens_uploader_running {int(self.state is UploaderProcessState.RUNNING)}",
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
    from .runtime import database_url, open_archive

    database = CollectorDatabase(database_url(arguments), max_size=DATABASE_CONNECTIONS)
    reader = None
    try:
        reader = open_archive(
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
        await wait_or_stop(stop_requested, REPORT_SECONDS)


def _send_reports(reports: queue.Queue[bytes], sender: Any) -> None:
    while True:
        message = reports.get()
        try:
            sender.send_bytes(message)
        except OSError:
            return
