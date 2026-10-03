from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Semaphore
from time import monotonic
from typing import Any

from psycopg.errors import (
    DataError,
    DeadlockDetected,
    Error,
    IntegrityError,
    QueryCanceled,
    RaiseException,
    SerializationFailure,
)

from . import (
    army_ingestion,
    battle_ingestion,
    boundary_publication,
    ingestion,
    job_outcomes,
    late_battle_sweep,
    reconciliation_db,
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
from .profile import ProfileParseError, parse_profile
from .rankings import (
    RankingParseError,
    parse_global_player_rankings,
)
from .source_observation_contract import validate_source_observation_contract
from .spool import SpoolError

MAX_CONCURRENCY = 32
DATABASE_CONFLICT_RETRIES = 3
# Keep current leaderboard evidence moving during a backlog while reserving
# claims for daily results and other derived work. See docs/architecture.md
# for the queue ordering rules.
NEWEST_PLAN_SIZE = 5000
NEWEST_PLAN_MAX_AGE_SECONDS = 30.0
NEWEST_PLAN_EMPTY_RETRY_SECONDS = 1.0
OLDEST_FIRST_CLAIM_EVERY = 4
# A continuous worker with two or more lanes keeps about two thirds of them
# (8 of 12) for responses. The rest run derived work: daily results, builds
# and redecodes. Only one of them may run a population build, and the timer's
# Reset publication checks and correction sweep take one of their turns.
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

    def record(self, stage: str, duration_seconds: float) -> None:
        with self._lock:
            values = self._stages.setdefault(
                stage,
                {
                    "count": 0,
                    "sum_seconds": 0.0,
                    "buckets": [0] * (len(STAGE_DURATION_BUCKETS_SECONDS) + 1),
                },
            )
            values["count"] += 1
            values["sum_seconds"] += duration_seconds
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

# Connections for the maintenance timer, kept apart from the lanes' pool so a
# slow round never holds a connection a lane is waiting for.
MAINTENANCE_POOL_SIZE = 2


def response_lane_count(concurrency: int) -> int:
    """Response-only lanes in a continuous worker of ``concurrency`` lanes."""
    if concurrency < 2:
        return 0
    return max(1, min(concurrency - 1, round(concurrency * 2 / 3)))


def lane_work_types(
    lane_index: int, concurrency: int
) -> tuple[tuple[str, ...], ...] | None:
    """The work one continuous lane claims, tried in order; None means any."""
    responses = response_lane_count(concurrency)
    if responses == 0:
        return None
    if lane_index <= responses:
        return (RESPONSE_WORK_TYPES,)
    if lane_index == responses + 1:
        return (POPULATION_BUILD_WORK_TYPES, DERIVED_WITHOUT_BUILDS)
    return (DERIVED_WITHOUT_BUILDS,)


class TimedMaintenance:
    """Reset publication checks and queue maintenance, each every 10 seconds.

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

    def reevaluate(self) -> None:
        if isinstance(self.database, Database):
            boundary_publication.reevaluate_boundary_publications(self.database)

    def run_due(self, derived_turns: Semaphore | None = None) -> None:
        current_time = monotonic()
        if current_time >= self.next_reevaluation_at and (
            derived_turns is None or derived_turns.acquire(blocking=False)
        ):
            try:
                self.next_reevaluation_at = current_time + 10
                self.reevaluate()
                self.late_battles.run_when_due()
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
    jobs are claimed per call. A lane stops at the first empty claim. When
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
            result = processor.process_once(
                owner=lane_owner(owner, lane_index),
                lease_seconds=lease_seconds,
            )
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
) -> None:
    """Keep ``concurrency`` lanes claiming until ``stop_requested`` is set.

    There is no batch: a lane that finds the queue empty, or
    ``claims_ready`` false, waits ``idle_seconds`` and claims again, so one
    long job never leaves the other lanes idle. Queue maintenance runs on its
    own timer thread, calling ``maintain`` every ``idle_seconds`` while
    ``claims_ready`` holds, so it never waits for a lane and no lane waits for
    it. A maintenance failure is reported by type only, never its message,
    and a later tick tries again. Each result goes to ``on_result`` as its
    job finishes, one at a time. Lane failures are isolated as in
    ``_run_lanes``, and the call returns once every lane and the timer have
    stopped.

    With two or more lanes, ``lane_work_types`` reserves lanes for responses
    so long derived work can never hold them all. Each derived lane takes a
    turn from a shared semaphore, one per derived lane, before it claims, and
    ``maintain`` receives the same semaphore for its heavy work.
    """
    _validate_lanes(concurrency, owner, lease_seconds)
    report_lock = threading.Lock()
    derived_turns = Semaphore(max(1, concurrency - response_lane_count(concurrency)))

    def maintenance_timer() -> None:
        while not stop_requested.is_set():
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

        work_type_order = lane_work_types(lane_index, concurrency)
        limits = [{"work_types": kinds} for kinds in work_type_order or ()] or [{}]
        takes_turns = work_type_order not in (None, (RESPONSE_WORK_TYPES,))
        while not stopped():
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


class ObservationProcessor:
    def __init__(
        self,
        database: Database,
        archive: S3ArchiveReader,
        stage_metrics: StageMetrics | None = None,
    ) -> None:
        self.database = database
        self.archive = archive
        self.stage_metrics = stage_metrics
        self.database.stage_metrics = stage_metrics
        self._plan: deque[int] = deque()
        self._plan_lock = Lock()
        self._plan_refreshed_at: float | None = None
        self._plan_refreshing = False
        self._claim_count = 0

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
        claim = self._claim_next(
            owner=owner, lease_seconds=lease_seconds, work_types=work_types
        )
        self._record_stage("python_claim", started_at)
        if claim is None:
            return None
        return self._process_claim(claim, lease_seconds=lease_seconds)

    def _claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        work_types: tuple[str, ...] | None = None,
    ) -> Claim | None:
        # The newest-first plan holds only responses, so derived lanes skip it.
        limit = {} if work_types is None else {"work_types": work_types}
        planned = False
        if work_types is None or "process_observation" in work_types:
            with self._plan_lock:
                self._claim_count += 1
                planned = self._claim_count % OLDEST_FIRST_CLAIM_EVERY != 0
        if planned:
            for attempt in range(NEWEST_PLAN_SIZE):
                if attempt == 0:
                    job_id = self._next_planned_job()
                else:
                    with self._plan_lock:
                        job_id = self._plan.popleft() if self._plan else None
                if job_id is None:
                    break
                claim = self.database.claim_job(
                    owner=owner, lease_seconds=lease_seconds, job_id=job_id, **limit
                )
                if claim is not None:
                    return claim
        return self.database.claim_job(
            owner=owner, lease_seconds=lease_seconds, **limit
        )

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
            plan = plan_source(limit=NEWEST_PLAN_SIZE)
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
        except QueryCanceled:
            # The worker's statement deadline cancelled stuck work and its
            # transaction rolled back.
            reason = "database_timeout"
        # The failed or cancelled transaction recorded no outcome. Restore its
        # retry slot so queue maintenance can recover it later, rather than
        # failing it if this was its last attempt, even if conflicts outlast
        # the lease. Only report retrying once the refund commits; it is lost
        # only if another worker or maintenance took the job.
        while True:
            try:
                self.database.refund_claim_attempt(claim)
                break
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            except (DeadlockDetected, SerializationFailure, QueryCanceled):
                continue
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

        try:
            # Renew before the spool miss can enter a bounded remote fallback;
            # the second renewal below fences the result before parsing.
            renewal_started_at = monotonic()
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
            self._record_stage("python_lease_renew", renewal_started_at)
            archive_started_at = monotonic()
            try:
                if uses_local_spool:
                    archived = self._read_local(claim)
                else:

                    def renew_lease() -> None:
                        # Heartbeat from the reader: keeps the renewed lease window
                        # ahead of the bounded remote retry wall time. Lease loss
                        # raises and discards any partial fallback result.
                        self.database.renew_claim(claim, lease_seconds=lease_seconds)

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

    def _read_local(self, claim: Claim) -> ArchiveReadResult:
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
            raise ArchiveReadError(
                "spool_missing",
                "new observation is missing from the local spool",
                retryable=False,
            )
        return ArchiveReadResult(
            body=body,
            reference=claim.archive_reference or "",
            sha256=claim.response_hash or "",
        )

    def _fail_rejected(self, claim: Claim, error: Error) -> ProcessResult:
        # PostgreSQL refused this job's writes, such as a Reset evidence row
        # its check rejects. Fail only this job, retrying it within its
        # attempts, and record the database's reason without the row values.
        detail = error.diag.message_primary or type(error).__name__
        try:
            return self._fail(claim, "database_rejected", detail=detail, retryable=True)
        except (
            *DATABASE_REJECTIONS,
            DeadlockDetected,
            SerializationFailure,
            QueryCanceled,
        ):
            # Recording the failure was refused, conflicted or timed out too.
            # Leave the lease to run out so queue maintenance retries the job
            # or fails its last try.
            return ProcessResult(claim.job_id, "retrying", "database_rejected")

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
    ) -> list[ProcessResult]:
        results: list[ProcessResult] = []
        for _ in range(max_jobs):
            if stop_requested is not None and stop_requested.is_set():
                break
            result = self.process_once(owner=owner, lease_seconds=lease_seconds)
            if result is None:
                break
            results.append(result)
        return results
