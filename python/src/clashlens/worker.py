from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Semaphore
from time import monotonic, thread_time
from typing import Any

import psycopg
from psycopg.errors import (
    DataError,
    DeadlockDetected,
    Error,
    IdleInTransactionSessionTimeout,
    IntegrityError,
    LockNotAvailable,
    QueryCanceled,
    RaiseException,
    SerializationFailure,
    TransactionTimeout,
)
from psycopg_pool import PoolTimeout, TooManyRequests

from . import (
    army_ingestion,
    army_rank_bands,
    battle_ingestion,
    boundary_publication,
    collector_uploads,
    ingestion,
    job_outcomes,
    late_battle_sweep,
    reconciliation_db,
    reset_settlement,
    snapshots,
)
from .archive import ArchiveReadError, ArchiveReadResult, S3ArchiveReader
from .battle import (
    BattleLogParseError,
    parse_battle_log,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    POPULATION_BUILD_WORK_TYPES,
    PROCESSING_VERSION,
    RESPONSE_WORK_TYPES,
    SUPPORTED_WORK_TYPES,
    Claim,
    Database,
    LeaseLost,
)
from .domain import DomainRuleError
from .league_history import (
    LeagueHistoryParseError,
    complete_league_history,
    parse_league_history,
)
from .operating import WORKER_JOB_STAGES
from .profile import ProfileParseError, parse_profile
from .rankings import (
    RankingParseError,
    parse_global_player_rankings,
)
from .source_observation_contract import validate_source_observation_contract
from .spool import SpoolError
from .worker_liveness import progress_file

MAX_CONCURRENCY = 32
# Each worker process runs its own Python interpreter, which runs one thread
# at a time: on 8 Oct 2026 one process used about one core and processed 894
# responses a minute however many lanes it had.
MAX_PROCESSES = 2
DATABASE_CONFLICT_RETRIES = 3
# Keep current leaderboard evidence moving during a backlog while reserving
# claims for daily results and other derived work. See docs/architecture.md
# for the queue ordering rules.
NEWEST_PLAN_SIZE = 5000
NEWEST_PLAN_MAX_AGE_SECONDS = 30.0
NEWEST_PLAN_EMPTY_RETRY_SECONDS = 1.0
OLDEST_FIRST_CLAIM_EVERY = 4
# Every other job each lane claims takes Reset-priority work first while any
# waits, and the rest take other due work first, so the previous day's board
# and live pages each get at least half of every lane's claims while both
# wait. On 8 Oct 2026 the rest went by waiting time, which Reset work wins for
# 20 minutes, and live responses waited 21 minutes behind the Reset backlog.
RESET_FIRST_CLAIM_EVERY = 2
# A continuous worker with two or more lanes keeps about two thirds of them
# (8 of 12) for responses unless told how many. The rest run derived work:
# daily results, builds and redecodes. Only one of them may run a population
# build, and the timer's Reset publication checks and correction sweep take
# one of their turns.
DERIVED_WITHOUT_BUILDS = tuple(
    work_type
    for work_type in SUPPORTED_WORK_TYPES
    if work_type not in RESPONSE_WORK_TYPES + POPULATION_BUILD_WORK_TYPES
)
STAGE_DURATION_BUCKETS_SECONDS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class StageMetrics:
    """Bounded thread-safe worker stage histograms for production evidence."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stages: dict[str, dict[str, Any]] = {}

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        started, cpu_started = monotonic(), thread_time()
        try:
            yield
        finally:
            cpu_seconds = max(0.0, thread_time() - cpu_started)
            self.record(stage, max(0.0, monotonic() - started), cpu_seconds)

    def record(
        self, stage: str, duration_seconds: float, cpu_seconds: float | None = None
    ) -> None:
        with self._lock:
            values = self._stages.setdefault(
                stage,
                {
                    "count": 0,
                    "sum_seconds": 0.0,
                    # Only job stages are measured with thread time, never a mix.
                    "thread_cpu_seconds": None,
                    "buckets": [0] * (len(STAGE_DURATION_BUCKETS_SECONDS) + 1),
                },
            )
            values["count"] += 1
            values["sum_seconds"] += duration_seconds
            if cpu_seconds is not None:
                values["thread_cpu_seconds"] = (
                    values["thread_cpu_seconds"] or 0.0
                ) + cpu_seconds
            for index, upper_bound in enumerate(STAGE_DURATION_BUCKETS_SECONDS):
                if duration_seconds <= upper_bound:
                    values["buckets"][index] += 1
            values["buckets"][-1] += 1

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            copied = {
                stage: {
                    "count": values["count"],
                    "sum_seconds": values["sum_seconds"],
                    "thread_cpu_seconds": values["thread_cpu_seconds"],
                    "buckets": list(values["buckets"]),
                }
                for stage, values in self._stages.items()
            }
        report: dict[str, dict[str, float | int | None]] = {}
        for stage, values in sorted(copied.items()):
            count = int(values["count"])
            buckets = values["buckets"]

            def percentile(
                fraction: float, *, count: int = count, buckets: list[int] = buckets
            ) -> float | None:
                rank = count * fraction
                for index, bucket_count in enumerate(buckets):
                    if bucket_count >= rank:
                        if index == len(STAGE_DURATION_BUCKETS_SECONDS):
                            return None
                        return STAGE_DURATION_BUCKETS_SECONDS[index] * 1000
                return None

            report[stage] = {
                "count": count,
                "elapsed_seconds": values["sum_seconds"],
                "thread_cpu_seconds": values["thread_cpu_seconds"],
                "average_ms": float(values["sum_seconds"]) * 1000 / count,
                "p50_upper_ms": percentile(0.50),
                "p95_upper_ms": percentile(0.95),
                "p99_upper_ms": percentile(0.99),
            }
        return report


@dataclass(frozen=True, slots=True)
class ProcessResult:
    job_id: int
    outcome: str
    category: str | None = None


def lane_owner(owner: str, lane_index: int) -> str:
    """Stable unique lease owner for one execution lane.

    The lane owner is derived from the configured owner so every concurrent
    lease in the queue is attributable to one container lane, and the same
    lane always claims under the same owner for the life of the process.
    """
    if not owner:
        raise ValueError("lease owner is required")
    if lane_index < 1:
        raise ValueError("lane index must be positive")
    return f"{owner}.lane-{lane_index}"


# Errors PostgreSQL raises when it refuses one job's writes: a trigger's
# check, a constraint or a bad value. Lost connections are not among them.
DATABASE_REJECTIONS = (RaiseException, IntegrityError, DataError)

# The shared connection pool had no free connection in time. Only the lane
# that waited is affected: it retries its claim, or its job, later.
POOL_BUSY = (PoolTimeout, TooManyRequests)

# Time limits that end the database session, rolling back its open
# transaction. The pool replaces the closed connection on its next use.
SESSION_ENDED = (IdleInTransactionSessionTimeout, TransactionTimeout)

# Connections for the maintenance timer, kept apart from the lanes' pool so a
# slow round never holds a connection a lane is waiting for.
MAINTENANCE_POOL_SIZE = 2
# Lane connections one worker process may open.
MAX_WORKER_POOL_SIZE = 16
# Connections all worker processes together may open: each process's lane
# pool, maintenance pool and maintenance permit. docs/architecture.md owns how
# this fits the whole database connection budget.
WORKER_CONNECTION_BUDGET = 38


def check_connection_budget(processes: int, pool_size: int) -> None:
    total = processes * (pool_size + MAINTENANCE_POOL_SIZE + 1)
    if pool_size > MAX_WORKER_POOL_SIZE or total > WORKER_CONNECTION_BUDGET:
        raise ValueError(
            f"worker processes may have at most {MAX_WORKER_POOL_SIZE} database"
            f" connections each and {WORKER_CONNECTION_BUDGET} in all"
        )


def response_lane_count(concurrency: int, response_lanes: int | None = None) -> int:
    """Response-only lanes in a continuous worker of ``concurrency`` lanes.

    ``response_lanes`` sets the share; at least one lane stays for derived work.
    """
    if concurrency < 2:
        return 0
    if response_lanes is not None:
        if not 1 <= response_lanes < concurrency:
            raise ValueError("response lanes must leave at least one derived lane")
        return response_lanes
    return max(1, min(concurrency - 1, round(concurrency * 2 / 3)))


def lane_work_types(
    lane_index: int, concurrency: int, response_lanes: int | None = None
) -> tuple[tuple[str, ...], ...] | None:
    """The work one continuous lane claims, tried in order; None means any."""
    responses = response_lane_count(concurrency, response_lanes)
    if responses == 0:
        return None
    if lane_index <= responses:
        return (RESPONSE_WORK_TYPES,)
    if lane_index == responses + 1:
        return (POPULATION_BUILD_WORK_TYPES, DERIVED_WITHOUT_BUILDS)
    return (DERIVED_WITHOUT_BUILDS,)


# Only one worker process at a time runs the Reset publication checks, the
# correction sweep and the army rank-band count, so two processes neither
# repeat that work nor race on it. The process whose own connection holds
# this lock runs them until it stops. Queue maintenance is cheap and safe to
# repeat, so every process runs it.
MAINTENANCE_PERMIT_KEY = "worker-publication-maintenance"


class MaintenancePermit:
    """A session lock the one maintaining process holds on its own connection."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self.connection: psycopg.Connection[Any] | None = None
        self.held = False

    def acquire(self) -> bool:
        """Whether this process holds the permit, taking it when it is free."""
        try:
            if self.connection is None:
                self.connection = psycopg.connect(
                    self.conninfo, autocommit=True, connect_timeout=10
                )
            if self.held:
                # The lock lasts as long as its connection does.
                self.connection.execute("SELECT 1")
            else:
                self.held = bool(self.connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    (MAINTENANCE_PERMIT_KEY,),
                ).fetchone()[0])
        except Error:
            self.close()
        return self.held

    def close(self) -> None:
        connection, self.connection, self.held = self.connection, None, False
        if connection is not None:
            connection.close()


class TimedMaintenance:
    """Reset publication checks and queue maintenance, each every 10 seconds.

    The publication checks also count the newest leaderboard's army rank-band
    totals once they are missing or stale. They run only in the worker
    process holding the maintenance permit.

    Given ``derived_turns``, the publication checks and correction sweep run
    only after taking a derived lane's turn, and stay due without one; queue
    maintenance does not wait for a turn.
    """

    def __init__(self, database: Database, stage_metrics: StageMetrics) -> None:
        self.database = database
        self.stage_metrics = stage_metrics
        self.late_battles = late_battle_sweep.LateBattleSweep(database)
        self.next_reevaluation_at = float("-inf")
        self.next_queue_maintenance_at = float("-inf")
        self.permit = (
            MaintenancePermit(database.pool.conninfo)
            if isinstance(database, Database)
            else None
        )

    def close(self) -> None:
        if self.permit is not None:
            self.permit.close()

    def reevaluate(self) -> None:
        if isinstance(self.database, Database):
            boundary_publication.reevaluate_boundary_publications(self.database)
            reset_settlement.refresh_terminal_work(self.database)

    def run_due(self, derived_turns: Semaphore | None = None) -> None:
        current_time = monotonic()
        if current_time >= self.next_reevaluation_at and not (
            self.permit is None or self.permit.acquire()
        ):
            # Another process maintains publications; look again later.
            self.next_reevaluation_at = current_time + 10
        if current_time >= self.next_reevaluation_at and (
            derived_turns is None or derived_turns.acquire(blocking=False)
        ):
            try:
                self.next_reevaluation_at = current_time + 10
                self.reevaluate()
                self.late_battles.run_when_due()
                if isinstance(self.database, Database):
                    army_rank_bands.refresh_rank_band_totals(self.database)
            finally:
                if derived_turns is not None:
                    derived_turns.release()
        if current_time >= self.next_queue_maintenance_at:
            self.next_queue_maintenance_at = current_time + 10
            maintenance_started_at = monotonic()
            self.database.maintain_queue(max_jobs=100)
            self.stage_metrics.record(
                "python_queue_maintenance", monotonic() - maintenance_started_at
            )


def _run_lanes(concurrency: int, claim_loop: Callable[[int, Event], None]) -> None:
    """Run ``claim_loop(lane_index, stop_claiming)`` on ``concurrency`` threads.

    An unexpected exception escaping one lane is isolated: ``stop_claiming``
    is set so other lanes finish their in-flight job and make no further
    claims, and a sanitized ``RuntimeError`` is raised after all lanes have
    stopped so no job details or credentials cross this boundary.
    """
    first_failure: Exception | None = None
    failure_lock = threading.Lock()
    stop_claiming = Event()

    def lane(lane_index: int) -> None:
        nonlocal first_failure
        try:
            claim_loop(lane_index, stop_claiming)
        except Exception as error:  # noqa: BLE001 - lane isolation boundary
            with failure_lock:
                if first_failure is None:
                    first_failure = error
            stop_claiming.set()

    threads = [
        threading.Thread(
            target=lane,
            args=(lane_index,),
            name=f"clashlens-worker-lane-{lane_index}",
            daemon=True,
        )
        for lane_index in range(1, concurrency + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if first_failure is not None:
        raise RuntimeError("worker lane failed; job details are not available")


def worker_process_commands(arguments: Any) -> list[list[str]]:
    """One command per worker process: the same worker, with its own owner.

    Each process numbers its own health-check progress file and snapshot
    files, so the health check sees a stuck process even while another works.
    """
    commands = []
    for index in range(1, arguments.processes + 1):
        command = [sys.executable, "-m", "clashlens.cli", *arguments.argv,
                   "--process-index", str(index),
                   "--owner", f"{arguments.owner}.process-{index}"]
        for option in ("operating_snapshot_file", "terminal_snapshot_file"):
            if getattr(arguments, option, ""):
                command += [f"--{option.replace('_', '-')}",
                            f"{getattr(arguments, option)}.{index}"]
        commands.append(command)
    return commands


def start_processes(
    arguments: Any, pool_size: int, on_signals: Callable[[Event], None]
) -> int | None:
    """Check the worker's settings and, as the parent of several, run them.

    Returns their exit status, or None when this process is itself a worker:
    the only one, or one of several its parent started.
    """
    response_lane_count(arguments.concurrency, getattr(arguments, "response_lanes", None))
    processes = getattr(arguments, "processes", 1)
    check_connection_budget(processes, pool_size)
    if processes == 1 or getattr(arguments, "process_index", 0):
        return None
    if not arguments.run_forever:
        raise ValueError("several worker processes need --run-forever")
    stop_requested = Event()
    on_signals(stop_requested)
    return run_processes(worker_process_commands(arguments), stop_requested)


def run_processes(commands: list[list[str]], stop_requested: Event) -> int:
    """Run one worker process per command until a stop or any one exits.

    Then each still running is asked to stop as on a shutdown signal: it
    finishes its current jobs and gives back batched claims. Exit status 0
    only for a requested stop that every process finished cleanly, so the
    container restarts them all when one fails.
    """
    # Each process's health-check file exists from the start, so one stuck
    # before its first job still ages into a failed health check.
    for index in range(1, len(commands) + 1):
        Path(progress_file(index)).touch()
    processes: list[subprocess.Popen[bytes]] = []
    try:
        for command in commands:
            processes.append(subprocess.Popen(command))
        while not stop_requested.is_set() and all(
            process.poll() is None for process in processes
        ):
            stop_requested.wait(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        codes = [process.wait() for process in processes]
    for index, code in enumerate(codes, start=1):
        print(json.dumps({"event": "worker_process", "process": index, "exit_code": code}),
              flush=True)
    clean = stop_requested.is_set() and len(codes) == len(commands)
    return 0 if clean and not any(codes) else 1


def _validate_lanes(concurrency: int, owner: str, lease_seconds: int) -> None:
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if not owner:
        raise ValueError("lease owner is required")
    if lease_seconds <= 0:
        raise ValueError("lease duration must be positive")


def process_concurrently(
    processor: ObservationProcessor,
    *,
    concurrency: int,
    owner: str,
    max_jobs: int,
    lease_seconds: int = 30,
    stop_requested: Event | None = None,
) -> list[ProcessResult]:
    """Process up to ``max_jobs`` jobs across up to ``concurrency`` lanes.

    Lanes are in-process threads that share the processor, the database pool,
    and the archive pool. The database claim transaction (``FOR UPDATE SKIP
    LOCKED`` plus lease owner/token fencing) and the archive pool bound the
    work: at most ``concurrency`` jobs run at once and at most ``max_jobs``
    jobs are claimed per call. A lane stops at the first empty claim, or when
    no database connection comes free for its claim. When
    ``stop_requested`` is set, lanes finish their current job and do not
    claim another; the call then waits for the bounded in-flight set and
    returns its results. Lane failures are isolated as in ``_run_lanes``.
    """
    _validate_lanes(concurrency, owner, lease_seconds)
    if max_jobs < 0:
        raise ValueError("max jobs must not be negative")
    if max_jobs == 0:
        return []
    results: list[ProcessResult] = []
    results_lock = threading.Lock()
    jobs_remaining = max_jobs
    jobs_lock = threading.Lock()

    def claim_loop(lane_index: int, stop_claiming: Event) -> None:
        nonlocal jobs_remaining
        while not stop_claiming.is_set():
            if stop_requested is not None and stop_requested.is_set():
                return
            with jobs_lock:
                if jobs_remaining == 0:
                    return
                jobs_remaining -= 1
            try:
                result = processor.process_once(
                    owner=lane_owner(owner, lane_index),
                    lease_seconds=lease_seconds,
                )
            except POOL_BUSY:
                # No connection for this claim: end this lane's batch as if
                # the queue were empty; the next batch claims again.
                return
            if result is None:
                return
            with results_lock:
                results.append(result)

    _run_lanes(concurrency, claim_loop)
    return results


def process_until_stopped(
    processor: ObservationProcessor,
    *,
    concurrency: int,
    owner: str,
    lease_seconds: int,
    stop_requested: Event,
    idle_seconds: float,
    claims_ready: Callable[[], bool],
    maintain: Callable[[Semaphore], None],
    on_result: Callable[[ProcessResult], None],
    progress: Callable[[], None] = lambda: None,
    response_lanes: int | None = None,
) -> None:
    """Keep ``concurrency`` lanes claiming until ``stop_requested`` is set.

    There is no batch: a lane that finds the queue empty, ``claims_ready``
    false, or no free database connection, waits ``idle_seconds`` and claims
    again, so one long job never leaves the other lanes idle. Queue
    maintenance runs on its own timer thread, calling ``maintain`` every
    ``idle_seconds`` while ``claims_ready`` holds, so it never waits for a
    lane and no lane waits for it. A maintenance failure is reported by type
    only, never its message, and a later tick tries again. Each result goes to
    ``on_result`` as its job finishes, one at a time. Lane failures are
    isolated as in ``_run_lanes``, and the call returns once every lane and
    the timer have stopped.

    With two or more lanes, ``lane_work_types`` reserves ``response_lanes``
    for responses so long derived work can never hold them all. Each derived
    lane takes a turn from a shared semaphore, one per derived lane, before it
    claims, and ``maintain`` receives the same semaphore for its heavy work.
    A batch of claims is never larger than the lanes of its kind not running
    a job, and claims no lane started are given back once every lane stops.

    Each lane and the timer call ``progress`` every time round their loops;
    the health check tracks each thread, so a stuck lane shows even while the
    other lanes and maintenance keep going.
    """
    _validate_lanes(concurrency, owner, lease_seconds)
    report_lock = threading.Lock()
    responses = response_lane_count(concurrency, response_lanes)
    derived_turns = Semaphore(max(1, concurrency - responses))
    batch_lanes = getattr(processor, "batch_lanes", None)
    if isinstance(batch_lanes, dict) and responses:
        # Less the derived lane's turn the maintenance timer may hold.
        batch_lanes.update({RESPONSE_WORK_TYPES: responses,
                            DERIVED_WITHOUT_BUILDS: concurrency - responses - 1})

    def maintenance_timer() -> None:
        while not stop_requested.is_set():
            progress()
            if claims_ready() and not stop_requested.is_set():
                try:
                    maintain(derived_turns)
                except Exception as error:  # noqa: BLE001 - retried next tick
                    print(
                        json.dumps(
                            {
                                "event": "worker_maintenance",
                                "status": "failed",
                                "error": type(error).__name__,
                            }
                        ),
                        flush=True,
                    )
            stop_requested.wait(idle_seconds)

    def claim_loop(lane_index: int, stop_claiming: Event) -> None:
        def stopped() -> bool:
            return stop_claiming.is_set() or stop_requested.is_set()

        work_type_order = lane_work_types(lane_index, concurrency, response_lanes)
        limits = [{"work_types": kinds} for kinds in work_type_order or ()] or [{}]
        takes_turns = work_type_order not in (None, (RESPONSE_WORK_TYPES,))
        while not stopped():
            progress()
            if takes_turns and not derived_turns.acquire(timeout=idle_seconds):
                continue
            try:
                ready = claims_ready()
                if stopped():
                    return
                result = None
                for limit in limits if ready else ():
                    result = processor.process_once(
                        owner=lane_owner(owner, lane_index),
                        lease_seconds=lease_seconds,
                        **limit,
                    )
                    if result is not None:
                        break
            except POOL_BUSY as error:
                # No connection for this lane's claim. It waits like an empty
                # queue and claims again; the other lanes keep working.
                print(
                    json.dumps(
                        {
                            "event": "worker_claim",
                            "status": "pool_busy",
                            "lane": lane_index,
                            "error": type(error).__name__,
                        }
                    ),
                    flush=True,
                )
            finally:
                if takes_turns:
                    derived_turns.release()
            if result is None:
                stop_requested.wait(idle_seconds)
                continue
            with report_lock:
                on_result(result)

    timer = threading.Thread(
        target=maintenance_timer, name="clashlens-worker-maintenance", daemon=True
    )
    timer.start()
    try:
        _run_lanes(concurrency, claim_loop)
    finally:
        stop_requested.set()
        timer.join()
        release = getattr(processor, "release_batched_claims", None)
        try:
            if callable(release):
                release()
        except (Error, *POOL_BUSY) as error:
            # Their leases run out and queue maintenance retries them.
            print(json.dumps({"event": "worker_claim", "status": "release_failed",
                              "error": type(error).__name__}), flush=True)


class ObservationProcessor:
    def __init__(
        self,
        database: Database,
        archive: S3ArchiveReader,
        stage_metrics: StageMetrics | None = None,
        claim_batch: int = 1,
    ) -> None:
        if claim_batch < 1:
            raise ValueError("claim batch must be positive")
        self.database = database
        self.archive = archive
        self.stage_metrics = stage_metrics
        self.database.stage_metrics = stage_metrics
        self._plan: deque[int] = deque()
        self._plan_lock = Lock()
        self._plan_refreshed_at: float | None = None
        self._plan_refreshing = False
        self._claim_count = 0
        self._lane_claims: dict[str, int] = {}
        # Response lanes, and derived lanes outside builds, share one batch of
        # claims per kind: one transaction leases up to ``claim_batch`` jobs,
        # no more than that kind's ``batch_lanes`` not running a job, and
        # whichever lane frees up first takes the next. A build is never
        # batched. Each claim waits with the time it was claimed.
        self.claim_batch = claim_batch
        self._batches: dict[tuple[str, ...], deque[tuple[float, Claim]]] = {
            RESPONSE_WORK_TYPES: deque(), DERIVED_WITHOUT_BUILDS: deque()
        }
        self._batch_locks = {kind: Lock() for kind in self._batches}
        self._batch_turns = dict.fromkeys(self._batches, 0)
        self.batch_lanes = dict.fromkeys(self._batches, 1)
        self._running = dict.fromkeys(self._batches, 0)
        # Process n of N plans only jobs whose number leaves n - 1 divided by
        # N, so processes never race for the same newest jobs.
        self.plan_share = (1, 1)

    def _record_stage(self, stage: str, started_at: float) -> None:
        if self.stage_metrics is not None:
            self.stage_metrics.record(stage, monotonic() - started_at)

    def process_once(
        self,
        *,
        owner: str,
        lease_seconds: int = 30,
        work_types: tuple[str, ...] | None = None,
    ) -> ProcessResult | None:
        started_at = monotonic()
        # A claim counts against its kind's lanes from the moment a lane holds
        # it: a batched claim as it leaves its batch, any other before it is
        # made. The build lane is one of the derived lanes, so it claims no
        # build while claims its batch leased wait for a lane.
        kind = DERIVED_WITHOUT_BUILDS if work_types == POPULATION_BUILD_WORK_TYPES else work_types
        batched = self.claim_batch > 1 and work_types in self._batches
        counted = kind in self._running
        if counted and not batched:
            with self._batch_locks[kind]:
                if self._batches[kind]:
                    return None
                self._count_running(kind, 1)
        claim = None
        try:
            claim = self._claim_next(
                owner=owner, lease_seconds=lease_seconds, work_types=work_types
            )
            self._record_stage("python_claim", started_at)
            if claim is None:
                return None
            return self._process_claim(claim, lease_seconds=lease_seconds)
        finally:
            if counted and (claim is not None or not batched):
                self._count_running(kind, -1)

    def _count_running(self, kind: tuple[str, ...], change: int) -> None:
        with self._plan_lock:
            self._running[kind] += change

    def _claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        work_types: tuple[str, ...] | None = None,
    ) -> Claim | None:
        if self.claim_batch > 1 and work_types in self._batches:
            assert work_types is not None
            with self._batch_locks[work_types]:
                batch = self._batches[work_types]
                if not batch:
                    claimed_at = monotonic()
                    batch.extend((claimed_at, claim) for claim in
                                 self._claim_batch(owner, lease_seconds, work_types))
                while batch:
                    claimed_at, claim = batch[0]
                    if monotonic() - claimed_at >= lease_seconds / 2:
                        # Slow jobs ahead of it, such as army redecodes, used
                        # half its lease: renew it before it starts, unless it
                        # is lost. It stays in the batch until renewed.
                        try:
                            self.database.renew_claim(claim, lease_seconds=lease_seconds)
                        except LeaseLost:
                            batch.popleft()
                            continue
                    batch.popleft()
                    self._count_running(work_types, 1)
                    return claim
                return None
        # The newest-first plan holds only responses, so derived lanes skip it.
        limit = {} if work_types is None else {"work_types": work_types}
        with self._plan_lock:
            turn = self._lane_claims.get(owner, 0)
        reset_turn = turn % RESET_FIRST_CLAIM_EVERY == 0
        # The board's build and checks always go before a slower army build.
        limit["reset_first"] = reset_turn or work_types == POPULATION_BUILD_WORK_TYPES
        planned = False
        if work_types is None or "process_observation" in work_types:
            with self._plan_lock:
                self._claim_count += 1
                planned = self._claim_count % OLDEST_FIRST_CLAIM_EVERY != 0
        claim = None
        if planned:
            for attempt in range(NEWEST_PLAN_SIZE):
                if attempt == 0:
                    job_id = self._next_planned_job()
                else:
                    with self._plan_lock:
                        job_id = self._plan.popleft() if self._plan else None
                if job_id is None:
                    break
                # Only on a Reset turn does the newest live response yield.
                claim = self.database.claim_job(
                    owner=owner,
                    lease_seconds=lease_seconds,
                    job_id=job_id,
                    planned=reset_turn,
                    **limit,
                )
                if claim is not None:
                    break
        if claim is None:
            claim = self.database.claim_job(
                owner=owner, lease_seconds=lease_seconds, **limit
            )
        if claim is not None:
            with self._plan_lock:
                self._lane_claims[owner] = turn + 1
        return claim

    def _claim_batch(
        self, owner: str, lease_seconds: int, work_types: tuple[str, ...]
    ) -> list[Claim]:
        """One batch of claims, taking turns like a lane's single claims do.

        Every other batch takes Reset-priority work first, and three batches
        of responses in four start from the newest-first plan; see
        ``_claim_next``.
        """
        turn = self._batch_turns[work_types]
        self._batch_turns[work_types] = turn + 1
        reset_turn = turn % RESET_FIRST_CLAIM_EVERY == 0
        with self._plan_lock:
            free_lanes = self.batch_lanes[work_types] - self._running[work_types]
        size = max(1, min(self.claim_batch, free_lanes))
        limit = {"owner": owner, "lease_seconds": lease_seconds, "limit": size,
                 "work_types": work_types, "reset_first": reset_turn}
        claims: list[Claim] = []
        if work_types == RESPONSE_WORK_TYPES and turn % OLDEST_FIRST_CLAIM_EVERY != 0:
            planned = self._next_planned_job()
            if planned is not None:
                with self._plan_lock:
                    job_ids = [planned, *(self._plan.popleft() for _ in range(
                        min(size - 1, len(self._plan))))]
                # Only on a Reset turn do the newest live responses yield.
                claims = self.database.claim_jobs(
                    job_ids=job_ids, planned=reset_turn, **limit
                )
        return claims or self.database.claim_jobs(**limit)

    def release_batched_claims(self) -> int:
        """Give back batched claims no lane started; each is claimable at once."""
        claims: list[Claim] = []
        for kind, batch in self._batches.items():
            with self._batch_locks[kind]:
                claims.extend(claim for _claimed_at, claim in batch)
                batch.clear()
        return self.database.release_claims(claims) if claims else 0

    def _next_planned_job(self) -> int | None:
        plan_source = getattr(self.database, "newest_job_plan", None)
        if plan_source is None:
            return None
        with self._plan_lock:
            now = monotonic()
            age = (
                None
                if self._plan_refreshed_at is None
                else now - self._plan_refreshed_at
            )
            due = age is None or age >= (
                NEWEST_PLAN_MAX_AGE_SECONDS
                if self._plan
                else NEWEST_PLAN_EMPTY_RETRY_SECONDS
            )
            # One lane refreshes; the others keep using the current plan, or
            # the oldest-first order when it is empty.
            if not due or self._plan_refreshing:
                return self._plan.popleft() if self._plan else None
            self._plan_refreshing = True
        try:
            share, shares = self.plan_share
            plan = [job_id for job_id in plan_source(limit=NEWEST_PLAN_SIZE)
                    if job_id % shares == share - 1]
        finally:
            with self._plan_lock:
                self._plan_refreshing = False
                self._plan_refreshed_at = monotonic()
        with self._plan_lock:
            self._plan = deque(plan)
            return self._plan.popleft() if self._plan else None

    def process_job(
        self,
        job_id: int,
        *,
        owner: str,
        lease_seconds: int = 30,
    ) -> ProcessResult | None:
        started_at = monotonic()
        claim = self.database.claim_job(
            owner=owner, lease_seconds=lease_seconds, job_id=job_id
        )
        self._record_stage("python_claim", started_at)
        if claim is None:
            return None
        return self._process_claim(claim, lease_seconds=lease_seconds)

    def _process_claim(self, claim: Claim, *, lease_seconds: int) -> ProcessResult:
        stage = WORKER_JOB_STAGES.get(claim.work_type)
        timing = (
            self.stage_metrics.measure(stage)
            if self.stage_metrics is not None and stage is not None
            else nullcontext()
        )
        with timing:
            return self._process_claim_with_retries(claim, lease_seconds=lease_seconds)

    def _process_claim_with_retries(
        self, claim: Claim, *, lease_seconds: int
    ) -> ProcessResult:
        # PostgreSQL rolls back only one side of a deadlock. Rerun that job under
        # the same claim so it neither stops the worker nor uses up an attempt.
        reason = "database_deadlock"
        try:
            for _ in range(DATABASE_CONFLICT_RETRIES):
                try:
                    return self._process_claim_once(claim, lease_seconds=lease_seconds)
                except (DeadlockDetected, SerializationFailure):
                    continue
            return self._fail(claim, "database_deadlock", retryable=True)
        except (DeadlockDetected, SerializationFailure):
            pass
        except DATABASE_REJECTIONS as error:
            return self._fail_rejected(claim, error)
        except LockNotAvailable:
            # A short lock wait limit, such as a battle log's on a busy Reset,
            # gave up and its transaction rolled back.
            reason = "database_lock_busy"
        except QueryCanceled:
            # The worker's statement deadline cancelled stuck work and its
            # transaction rolled back.
            reason = "database_timeout"
        except SESSION_ENDED:
            # The session ended mid-transaction, even while committing. Trust
            # the job's saved attempt, not the error: a commit that landed
            # stays done and is never run again.
            reason = "database_session_timeout"
            saved = self._saved_result(claim, reason)
            if saved is not None:
                return saved
        except POOL_BUSY:
            # No pool connection came free in time, so this job's next write
            # never started.
            reason = "database_pool_timeout"
        finally:
            if claim.work_type in POPULATION_BUILD_WORK_TYPES:
                # Return freed build buffers to the OS instead of retaining them.
                try:
                    ctypes.CDLL("libc.so.6").malloc_trim(ctypes.c_size_t(0))
                except (OSError, AttributeError):
                    pass  # glibc or malloc_trim is unavailable on this platform.
        # The failed or cancelled transaction recorded no outcome. Restore its
        # retry slot so queue maintenance can recover it later, rather than
        # failing it if this was its last attempt, even if conflicts outlast
        # the lease. Report retrying once the refund commits, or once the pool
        # has no connection for it; it is lost only if another worker or
        # maintenance took the job.
        while True:
            try:
                self.database.refund_claim_attempt(claim)
                break
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except (DeadlockDetected, SerializationFailure, QueryCanceled):
                continue
            except (*POOL_BUSY, LockNotAvailable, *SESSION_ENDED):
                # No connection, or the refund's own short lock wait or
                # session ran out. Leave the lease to run out so queue
                # maintenance retries the job or fails its last try.
                break
        return ProcessResult(claim.job_id, "retrying", reason)

    def _process_claim_once(self, claim: Claim, *, lease_seconds: int) -> ProcessResult:
        if claim.work_type == "reconcile_ranked_day":
            if claim.processing_version != PROCESSING_VERSION:
                return self._fail(
                    claim, "unsupported_processing_version", retryable=False
                )
            if claim.domain_rule_version != DOMAIN_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_domain_rule_version", retryable=False
                )
            try:
                self.database.renew_claim(claim, lease_seconds=lease_seconds)
                reconciliation_db.complete_reconciliation(self.database, claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except DomainRuleError as error:
                return self._complete_retired(claim, error)
            return ProcessResult(claim.job_id, "processed")
        if claim.work_type in {"build_snapshot", "build_analytics"}:
            if claim.processing_version != PROCESSING_VERSION:
                return self._fail(
                    claim, "unsupported_processing_version", retryable=False
                )
            if claim.domain_rule_version != DOMAIN_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_domain_rule_version", retryable=False
                )
            if claim.analytics_rule_version != ANALYTICS_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_analytics_rule_version", retryable=False
                )
            try:
                self.database.renew_claim(claim, lease_seconds=lease_seconds)
                if claim.work_type == "build_snapshot":
                    snapshots.complete_snapshot(self.database, claim)
                else:
                    boundary_publication.complete_analytics(self.database, claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except DomainRuleError as error:
                return self._complete_retired(claim, error)
            except (KeyError, TypeError, ValueError) as error:
                return self._fail(
                    claim,
                    "dependency_not_ready"
                    if "dependency" in str(error)
                    else "invalid_work_input",
                    detail=str(error),
                    retryable=False,
                )
            return ProcessResult(claim.job_id, "processed")
        if claim.work_type in {"build_army_analytics", "redecode_army"}:
            if claim.processing_version != PROCESSING_VERSION:
                return self._fail(
                    claim, "unsupported_processing_version", retryable=False
                )
            if claim.domain_rule_version != DOMAIN_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_domain_rule_version", retryable=False
                )
            if claim.analytics_rule_version != ARMY_ANALYTICS_RULE_VERSION:
                return self._fail(
                    claim, "unsupported_analytics_rule_version", retryable=False
                )
            try:
                self.database.renew_claim(claim, lease_seconds=max(lease_seconds, 300))
                if claim.work_type == "build_army_analytics":
                    army_ingestion.complete_army_analytics(self.database, claim)
                else:
                    army_ingestion.complete_army_redecode(self.database, claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except DomainRuleError as error:
                return self._complete_retired(claim, error)
            except (KeyError, TypeError, ValueError) as error:
                is_dependency = (
                    "dependency" in str(error).lower()
                    or "not completed" in str(error).lower()
                    or "pending" in str(error).lower()
                )
                return self._fail(
                    claim,
                    "dependency_not_ready" if is_dependency else "invalid_work_input",
                    detail=str(error),
                    retryable=is_dependency,
                )
            return ProcessResult(claim.job_id, "processed")
        if claim.work_type not in {"process_observation", "replay_observation"}:
            return self._fail(claim, "unsupported_work_type", retryable=False)
        source_contract_error = validate_source_observation_contract(
            claim.endpoint,
            claim.endpoint_version,
            claim.schema_version,
            claim.parser_version,
        )
        if source_contract_error is not None:
            return self._fail(claim, source_contract_error, retryable=False)
        checks = (
            (
                claim.processing_version == PROCESSING_VERSION,
                "unsupported_processing_version",
            ),
            (
                claim.domain_rule_version == DOMAIN_RULE_VERSION,
                "unsupported_domain_rule_version",
            ),
        )
        for valid, category in checks:
            if not valid:
                return self._fail(claim, category, retryable=False)
        assert claim.endpoint_version is not None

        uses_local_spool = (
            claim.work_type == "process_observation"
            and getattr(self.archive, "spool", None) is not None
        )
        if claim.response_hash is None or (
            not uses_local_spool and claim.archive_reference is None
        ):
            return self._fail(claim, "missing_archive_metadata", retryable=False)

        if (
            claim.endpoint == "profile"
            and claim.http_status is not None
            and 200 <= claim.http_status < 300
        ):
            try:
                if ingestion.supersede_profile(self.database, claim):
                    return ProcessResult(claim.job_id, "superseded")
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")

        def renew_lease() -> None:
            # Heartbeat from the reader: keeps the renewed lease window
            # ahead of the bounded remote retry wall time. Lease loss
            # raises and discards any partial fallback result.
            self.database.renew_claim(claim, lease_seconds=lease_seconds, always=True)

        try:
            # Renew before a remote read, which can retry for a bounded time. A
            # local spool read skips this unless the saved copy is gone. The second
            # checks the claim before parsing; it writes a new lease once half is used.
            if not uses_local_spool:
                renewal_started_at = monotonic()
                self.database.renew_claim(claim, lease_seconds=lease_seconds, always=True)
                self._record_stage("python_lease_renew", renewal_started_at)
            archive_started_at = monotonic()
            try:
                if uses_local_spool:
                    archived = self._read_local(claim, renew_lease)
                else:
                    archived = self.archive.read_verified(
                        claim.archive_reference,
                        claim.response_hash,
                        heartbeat=renew_lease,
                    )
            finally:
                self._record_stage(
                    "python_archive_local_verify"
                    if uses_local_spool
                    else "python_archive_get_verify",
                    archive_started_at,
                )
            renewal_started_at = monotonic()
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
            self._record_stage("python_lease_renew", renewal_started_at)
        except ArchiveReadError as error:
            try:
                state = job_outcomes.fail_claim(
                    self.database,
                    claim,
                    category=error.category,
                    detail=str(error),
                    retryable=error.retryable,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(
                claim.job_id,
                "retrying"
                if state in {"waiting_retry", "waiting_dependency"}
                else "failed",
                error.category,
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")

        if claim.http_status is None:
            return self._fail(claim, "missing_http_status", retryable=False)
        if claim.http_status < 200 or claim.http_status >= 300:
            try:
                job_outcomes.complete_classified(
                    self.database, claim, outcome="source_non_success"
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(claim.job_id, "classified", "non_success")

        if claim.observed_at is None:
            return self._fail(claim, "missing_observation_time", retryable=False)

        try:
            if claim.endpoint == "profile":
                if claim.normalized_tag is None or claim.endpoint_version is None:
                    return self._fail(claim, "missing_player_scope", retryable=False)
                parse_started_at = monotonic()
                profile = parse_profile(
                    archived.body,
                    expected_tag=claim.normalized_tag,
                    observed_at=claim.observed_at,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self._record_stage("python_parse_profile", parse_started_at)
                domain_started_at = monotonic()
                ingestion.complete_profile(self.database, claim, profile)
                self._record_stage("python_domain_profile", domain_started_at)
                outcome = "processed"
            elif claim.endpoint == "battle_log":
                if claim.normalized_tag is None or claim.endpoint_version is None:
                    return self._fail(claim, "missing_player_scope", retryable=False)
                parse_started_at = monotonic()
                battle_log = parse_battle_log(
                    archived.body,
                    expected_tag=claim.normalized_tag,
                    observed_at=claim.observed_at,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self._record_stage("python_parse_battle_log", parse_started_at)
                if battle_ingestion.supersede_battle_log(
                    self.database, claim, battle_log
                ):
                    return ProcessResult(claim.job_id, "superseded")
                domain_started_at = monotonic()
                battle_ingestion.complete_battle_log(self.database, claim, battle_log)
                self._record_stage("python_domain_battle_log", domain_started_at)
                outcome = (
                    "processed_with_gaps" if battle_log.has_row_gap else "processed"
                )
            elif claim.endpoint == "league_history":
                if claim.normalized_tag is None:
                    return self._fail(claim, "missing_player_scope", retryable=False)
                parse_started_at = monotonic()
                history = parse_league_history(
                    archived.body,
                    expected_tag=claim.normalized_tag,
                    observed_at=claim.observed_at,
                    parser_version=claim.parser_version,
                )
                self._record_stage("python_parse_league_history", parse_started_at)
                domain_started_at = monotonic()
                complete_league_history(self.database, claim, history)
                self._record_stage("python_domain_league_history", domain_started_at)
                outcome = "processed_with_gaps" if history.has_row_gap else "processed"
            else:
                parse_started_at = monotonic()
                rankings = parse_global_player_rankings(
                    archived.body,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self._record_stage("python_parse_rankings", parse_started_at)
                domain_started_at = monotonic()
                ingestion.complete_rankings(self.database, claim, rankings)
                self._record_stage("python_domain_rankings", domain_started_at)
                outcome = "processed"
        except (
            ProfileParseError,
            BattleLogParseError,
            RankingParseError,
            LeagueHistoryParseError,
        ) as error:
            return self._fail(claim, error.category, detail=str(error), retryable=False)
        except DomainRuleError as error:
            return self._complete_retired(claim, error)
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(claim.job_id, outcome)

    def _read_local(
        self, claim: Claim, renew_lease: Callable[[], None]
    ) -> ArchiveReadResult:
        spool = getattr(self.archive, "spool", None)
        verify = getattr(spool, "verify", None)
        if not callable(verify):
            raise ArchiveReadError(
                "spool_missing",
                "new observation has no local spool reader",
                retryable=False,
            )
        try:
            body = verify(claim.response_hash)
        except (OSError, SpoolError) as error:
            # A failed disk read is not a missing response: wait and retry it
            # without spending an attempt. Database failures stay separate.
            raise ArchiveReadError(
                "spool_io_failed", "local evidence read failed", retryable=True
            ) from error
        if body is None:
            return self._read_archived_copy(claim, renew_lease)
        return ArchiveReadResult(
            body=body,
            reference=claim.archive_reference or "",
            sha256=claim.response_hash or "",
        )

    def _read_archived_copy(
        self, claim: Claim, renew_lease: Callable[[], None]
    ) -> ArchiveReadResult:
        """Read back the archived copy of a response whose saved copy is gone.

        A lost disk, or a database restored to before spool cleanup ran, leaves
        a job without its saved copy while the archive holds one. The reader
        checks its hash and saves it locally again. Only bytes the archive
        cannot hold are missing proof.
        """
        assert claim.response_hash is not None
        bucket = getattr(getattr(self.archive, "archive", None), "bucket", None)
        copy = (
            None
            if bucket is None
            else collector_uploads.archived_copy(
                self.database, claim.response_hash, bucket=bucket
            )
        )
        if copy is None:
            raise ArchiveReadError(
                "spool_missing",
                "new observation is missing from the local spool and the archive",
                retryable=True,
            )
        renew_lease()
        try:
            return self.archive.read_verified(
                copy.reference, claim.response_hash, heartbeat=renew_lease
            )
        except ArchiveReadError as error:
            # A recorded copy that cannot be found yet, or an upload still in
            # flight or whose last write may yet land, is retried within the
            # job's attempts.
            if error.category != "archive_missing" or copy.recorded or copy.uploading:
                raise
            marker = self.archive.check_marker_health()
            if marker == "degraded":
                raise ArchiveReadError(
                    "archive_unavailable",
                    "archive marker could not be checked",
                    retryable=True,
                ) from error
            if marker == "terminal":
                raise ArchiveReadError(
                    "archive_marker_mismatch",
                    "archive marker does not match its configured hash",
                    retryable=True,
                ) from error
            raise ArchiveReadError(
                "spool_missing",
                "new observation is missing from the local spool and was never archived",
                retryable=True,
            ) from error

    def _fail_rejected(self, claim: Claim, error: Error) -> ProcessResult:
        # PostgreSQL refused this job's writes, such as a Reset evidence row
        # its check rejects. Fail only this job, retrying it within its
        # attempts, and record the database's reason without the row values.
        detail = error.diag.message_primary or type(error).__name__
        try:
            return self._fail(claim, "database_rejected", detail=detail, retryable=True)
        except SESSION_ENDED:
            saved = self._saved_result(claim, "database_rejected")
            if saved is not None:
                return saved
        except (
            *DATABASE_REJECTIONS,
            DeadlockDetected,
            SerializationFailure,
            QueryCanceled,
            LockNotAvailable,
            *POOL_BUSY,
        ):
            pass
        # Recording the failure was refused, conflicted or timed out too.
        # Leave the lease to run out so queue maintenance retries the job
        # or fails its last try.
        return ProcessResult(claim.job_id, "retrying", "database_rejected")

    def _saved_result(self, claim: Claim, reason: str) -> ProcessResult | None:
        # None while nothing committed this attempt's outcome.
        try:
            saved = self.database.finished_attempt(claim)
        except (*SESSION_ENDED, QueryCanceled, *POOL_BUSY):
            # Unknown: leave the lease to run out; maintenance recovers
            # the job only if it is still unfinished.
            return ProcessResult(claim.job_id, "retrying", reason)
        if saved is None:
            return None
        # Report what the normal path returns for the saved attempt.
        state, outcome, category = saved
        if state == "stale":
            return ProcessResult(claim.job_id, "lease_lost")
        if state != "complete":
            return ProcessResult(
                claim.job_id,
                "retrying"
                if state in {"waiting_retry", "waiting_dependency"}
                else "failed",
                category,
            )
        if outcome == "superseded":
            return ProcessResult(claim.job_id, "superseded")
        if outcome == "source_non_success":
            return ProcessResult(claim.job_id, "classified", "non_success")
        if outcome == "season_detail_retired":
            return ProcessResult(claim.job_id, outcome, outcome)
        gaps = outcome == "processed_with_gaps" or (
            claim.endpoint == "league_history" and outcome == "official_partial"
        )
        return ProcessResult(
            claim.job_id, "processed_with_gaps" if gaps else "processed"
        )

    def _complete_retired(self, claim: Claim, error: DomainRuleError) -> ProcessResult:
        if error.category != "season_detail_retired":
            raise error
        try:
            job_outcomes.complete_terminal(
                self.database, claim, outcome="season_detail_retired"
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(claim.job_id, "season_detail_retired", error.category)

    def _fail(
        self,
        claim: Claim,
        category: str,
        *,
        detail: str | None = None,
        retryable: bool,
    ) -> ProcessResult:
        try:
            state = job_outcomes.fail_claim(
                self.database,
                claim,
                category=category,
                detail=detail or category,
                retryable=retryable,
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(
            claim.job_id,
            "retrying"
            if state in {"waiting_retry", "waiting_dependency"}
            else "failed",
            category,
        )

    def process_until_idle(
        self,
        *,
        owner: str,
        max_jobs: int = 100,
        lease_seconds: int = 30,
        stop_requested: Event | None = None,
        progress: Callable[[], None] = lambda: None,
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for _ in range(max_jobs):
            progress()
            if stop_requested is not None and stop_requested.is_set():
                break
            try:
                result = self.process_once(owner=owner, lease_seconds=lease_seconds)
            except POOL_BUSY:
                break
            if result is None:
                break
            results.append(result)
        return results
