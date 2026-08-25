from __future__ import annotations

import threading
from dataclasses import dataclass
from threading import Event, Lock
from time import monotonic
from typing import Any

from .archive import ArchiveReadError, S3ArchiveReader
from .battle import (
    BattleLogParseError,
    parse_battle_log,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    LeaseLost,
)
from .profile import ProfileParseError, parse_profile
from .rankings import (
    RankingParseError,
    parse_global_player_rankings,
)
from .source_observation_contract import validate_source_observation_contract

MAX_CONCURRENCY = 32
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
    jobs are claimed per call. When ``stop_requested`` is set, lanes finish
    their current job and do not claim another; the call then waits for the
    bounded in-flight set and returns its results.

    An unexpected exception escaping one lane is isolated: other lanes finish
    their in-flight job, no further claims are made, and a sanitized
    ``RuntimeError`` is raised after all lanes have stopped so no job details
    or credentials cross this boundary.
    """
    if concurrency < 1 or concurrency > MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if not owner:
        raise ValueError("lease owner is required")
    if max_jobs < 0:
        raise ValueError("max jobs must not be negative")
    if lease_seconds <= 0:
        raise ValueError("lease duration must be positive")
    if max_jobs == 0:
        return []
    results: list[ProcessResult] = []
    results_lock = threading.Lock()
    jobs_remaining = max_jobs
    jobs_lock = threading.Lock()
    first_failure: Exception | None = None
    failure_lock = threading.Lock()
    stop_claiming = Event()

    def lane(lane_index: int) -> None:
        nonlocal first_failure, jobs_remaining
        while True:
            if stop_claiming.is_set():
                return
            if stop_requested is not None and stop_requested.is_set():
                return
            with jobs_lock:
                if jobs_remaining <= 0:
                    return
                jobs_remaining -= 1
            try:
                result = processor.process_once(
                    owner=lane_owner(owner, lane_index),
                    lease_seconds=lease_seconds,
                )
            except Exception as error:  # noqa: BLE001 - lane isolation boundary
                with failure_lock:
                    if first_failure is None:
                        first_failure = error
                stop_claiming.set()
                return
            if result is None:
                return
            with results_lock:
                results.append(result)

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
    return results


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

    def _record_stage(self, stage: str, started_at: float) -> None:
        if self.stage_metrics is not None:
            self.stage_metrics.record(stage, monotonic() - started_at)

    def process_once(
        self, *, owner: str, lease_seconds: int = 30
    ) -> ProcessResult | None:
        started_at = monotonic()
        claim = self.database.claim_job(owner=owner, lease_seconds=lease_seconds)
        self._record_stage("python_claim", started_at)
        if claim is None:
            return None
        return self._process_claim(claim, lease_seconds=lease_seconds)

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
                self.database.complete_reconciliation(claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
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
                    self.database.complete_snapshot(claim)
                else:
                    self.database.complete_analytics(claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
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
                    self.database.complete_army_analytics(claim)
                else:
                    self.database.complete_army_redecode(claim)
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
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

        if claim.archive_reference is None or claim.response_hash is None:
            return self._fail(claim, "missing_archive_metadata", retryable=False)

        try:
            # Renew before the spool miss can enter a bounded remote fallback;
            # the second renewal below fences the result before parsing.
            renewal_started_at = monotonic()
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
            self._record_stage("python_lease_renew", renewal_started_at)
            archive_started_at = monotonic()
            try:
                def renew_lease() -> None:
                    # Heartbeat from the reader: keeps the renewed lease window
                    # ahead of the bounded remote retry wall time. Lease loss
                    # raises and discards any partial fallback result.
                    self.database.renew_claim(claim, lease_seconds=lease_seconds)

                archived = self.archive.read_verified(
                    claim.archive_reference, claim.response_hash, heartbeat=renew_lease
                )
            finally:
                self._record_stage("python_archive_get_verify", archive_started_at)
            renewal_started_at = monotonic()
            self.database.renew_claim(claim, lease_seconds=lease_seconds)
            self._record_stage("python_lease_renew", renewal_started_at)
        except ArchiveReadError as error:
            try:
                state = self.database.fail_claim(
                    claim,
                    category=error.category,
                    detail=str(error),
                    retryable=error.retryable,
                )
            except LeaseLost:
                return ProcessResult(claim.job_id, "lease_lost")
            return ProcessResult(
                claim.job_id,
                "retrying" if state == "waiting_retry" else "failed",
                error.category,
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")

        if claim.http_status is None:
            return self._fail(claim, "missing_http_status", retryable=False)
        if claim.http_status < 200 or claim.http_status >= 300:
            try:
                self.database.complete_classified(claim, outcome="source_non_success")
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
                self.database.complete_profile(claim, profile)
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
                domain_started_at = monotonic()
                self.database.complete_battle_log(claim, battle_log)
                self._record_stage("python_domain_battle_log", domain_started_at)
                outcome = (
                    "processed_with_gaps" if battle_log.has_row_gap else "processed"
                )
            else:
                parse_started_at = monotonic()
                rankings = parse_global_player_rankings(
                    archived.body,
                    endpoint_version=claim.endpoint_version,
                    parser_version=claim.parser_version,
                )
                self._record_stage("python_parse_rankings", parse_started_at)
                domain_started_at = monotonic()
                self.database.complete_rankings(claim, rankings)
                self._record_stage("python_domain_rankings", domain_started_at)
                outcome = "processed"
        except (ProfileParseError, BattleLogParseError, RankingParseError) as error:
            return self._fail(claim, error.category, detail=str(error), retryable=False)
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(claim.job_id, outcome)

    def _fail(
        self,
        claim: Claim,
        category: str,
        *,
        detail: str | None = None,
        retryable: bool,
    ) -> ProcessResult:
        try:
            state = self.database.fail_claim(
                claim,
                category=category,
                detail=detail or category,
                retryable=retryable,
            )
        except LeaseLost:
            return ProcessResult(claim.job_id, "lease_lost")
        return ProcessResult(
            claim.job_id,
            "retrying" if state == "waiting_retry" else "failed",
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
