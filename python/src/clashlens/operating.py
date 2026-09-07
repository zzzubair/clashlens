from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4

LATENCY_BUCKETS_SECONDS = (
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
API_OUTCOMES = ("success", "expected_4xx", "safe_5xx", "response_size_limit")
API_ROUTES = (
    "livez",
    "readyz",
    "operator",
    "player_search",
    "player_read",
    "player_refresh",
    "refresh_status",
    "leaderboard_live",
    "leaderboard_frozen",
    "army_analytics",
    "battle_army",
    "basic_analytics",
    "public_user",
    "account",
    "saved_players",
    "groups",
    "exports",
    "providers",
    "verification",
    "other",
)
WORKER_OUTCOMES = (
    "processed",
    "processed_with_gaps",
    "retrying",
    "failed",
    "lease_lost",
    "classified",
    "other",
)
WORKER_STAGES = (
    "python_archive_get_verify",
    "python_archive_local_verify",
    "python_archive_pool_acquire",
    "python_archive_repair",
    "python_claim",
    "python_database_pool_acquire",
    "python_domain_battle_log",
    "python_domain_profile",
    "python_domain_rankings",
    "python_lease_renew",
    "python_parse_battle_log",
    "python_parse_profile",
    "python_parse_rankings",
    "python_queue_maintenance",
)
WORKER_SNAPSHOT_INTERVAL_SECONDS = 60.0
WORKER_SNAPSHOT_MAX_AGE_SECONDS = WORKER_SNAPSHOT_INTERVAL_SECONDS * 2
COLLECTOR_STAGES = (
    "ambiguous_commit_proof",
    "archive_get_verify",
    "archive_head",
    "archive_put",
    "archive_write",
    "attempt_resolution",
    "claim",
    "claim_pool_acquire",
    "database_pool_acquire",
    "dependency_readiness",
    "job",
    "observation_commit",
    "observation_job_lock",
    "official_api_battle_log",
    "official_api_global_player_rankings",
    "official_api_profile",
    "prepare_attempt",
    "request_start",
    "schedule_due_regular",
    "spool_cleanup",
)
COLLECTOR_JOB_OUTCOMES = ("scheduled", "claimed", "handled", "error")
COLLECTOR_API_OUTCOMES = (
    "success",
    "expected_4xx",
    "safe_5xx",
    "transport_failure",
    "other",
)
POOL_FIELDS = (
    "pool_min",
    "pool_max",
    "pool_size",
    "pool_available",
    "requests_waiting",
    "requests_num",
    "requests_queued",
    "requests_wait_ms",
    "usage_ms",
)
POOL_MEASURE_FIELDS = POOL_FIELDS[:5]
POOL_COUNTER_FIELDS = POOL_FIELDS[5:]
SPOOL_FIELDS = (
    "final_bytes",
    "final_objects",
    "temporary_bytes",
    "temporary_objects",
    "abandoned_temporary_bytes",
    "abandoned_temporary_objects",
    "reserved_bytes",
    "reserved_objects",
    "high_water_bytes",
    "free_bytes",
    "free_inodes",
)
SPOOL_CONFIG_FIELDS = (
    "max_body_bytes",
    "max_bytes",
    "max_objects",
    "free_space_floor",
    "free_inode_floor",
)
DATABASE_IDENTITY_FIELDS = ("system_identifier", "database_oid")
QUEUE_STATUSES = (
    "pending",
    "leased",
    "waiting_retry",
    "waiting_dependency",
    "complete",
    "failed",
    "cancelled",
)
BOUNDARY_STATES = (
    "pending",
    "ready",
    "building",
    "published",
    "superseded",
    "failed",
)
BOUNDARY_ARTIFACTS = ("snapshot", "army")
FAILURE_CATEGORIES = (
    "storage",
    "transport",
    "lease_expired",
    "dependency",
    "unsupported",
    "data_quality",
    "other",
)
CHECK_REASONS = (
    "active_boundary_blocked",
    "active_boundary_failure",
    "active_boundary_unrecoverable_lease",
    "invalid_previous_snapshot",
    "required_fact_invalid",
    "required_fact_missing",
    "spool_bytes_capacity_exceeded",
    "spool_free_inodes_below_floor",
    "spool_free_space_below_floor",
    "spool_objects_capacity_exceeded",
)
RELATION_NAMES = (
    "account_export_requests",
    "account_group_players",
    "account_groups",
    "account_provider_identities",
    "account_saved_players",
    "analytics_breakdowns",
    "analytics_summaries",
    "api_frozen_leaderboard_entries",
    "api_frozen_leaderboards",
    "api_player_daily_logs",
    "api_refresh_requests",
    "archive_catalogue",
    "archive_instances",
    "army_analytics_battle_facts",
    "army_analytics_breakdowns",
    "army_analytics_completed_days",
    "army_analytics_day_summaries",
    "army_analytics_season_summaries",
    "battle_army_decodes",
    "battle_evidence",
    "battle_log_observation_rows",
    "battle_log_observations",
    "battle_payload_rows",
    "battle_perspectives",
    "battle_source_rows",
    "boundary_publication_artifact_identities",
    "boundary_publication_corrections",
    "boundary_publication_events",
    "boundary_publication_generation_members",
    "boundary_publication_generations",
    "boundary_publication_legacy_job_migrations",
    "boundary_publication_manifest_rows",
    "boundary_publication_manifests",
    "clash_lens_accounts",
    "clash_lens_contract",
    "clash_lens_schema_migrations",
    "collector_attempt_events",
    "collector_attempts",
    "collector_boundary_admission",
    "collector_endpoint_results",
    "collector_interactive_intent_events",
    "collector_jobs",
    "collector_observations",
    "collector_reset_baseline_sweeps",
    "collector_reset_sweep_members",
    "collector_reset_sweeps",
    "collector_transport_failures",
    "discovery_profile_intents",
    "exact_armies",
    "global_rankings_intents",
    "known_player_discoveries",
    "leaderboard_snapshot_entries",
    "leaderboard_snapshots",
    "legend_battles",
    "legend_season_anchors",
    "observation_processing_outcomes",
    "official_top200_attempt_entries",
    "official_top200_attempts",
    "official_top200_entries",
    "official_top200_version_entries",
    "official_top200_versions",
    "parsed_source_payloads",
    "player_discovery_events",
    "player_link_verification_audits",
    "player_profile_effects",
    "player_profile_versions",
    "players",
    "private_api_requests",
    "processed_observation_versions",
    "provider_identity_audits",
    "python_processing_attempts",
    "python_processing_job_events",
    "python_processing_jobs",
    "python_replay_requests",
    "ranked_day_adjustments",
    "ranked_day_versions",
    "reset_baseline_evidence",
    "season_anchor_evidence",
    "source_response_parses",
    "shared_api_credential_events",
    "shared_api_credentials",
    "shared_api_permits",
    "support_player_link_transfer_audits",
    "support_player_link_transfer_candidates",
    "unit_catalog_versions",
    "verified_player_links",
)
OBSERVATION_CATEGORIES = ("profile", "battle_log", "global_player_rankings")
FACT_CATEGORIES = (
    "ranked_day_complete",
    "ranked_day_partial",
    "ranked_day_inconsistent",
    "ranked_day_malformed",
    "army_decoded",
    "army_failed",
)
RESULT_CATEGORIES = (
    "leaderboard_snapshots",
    "army_analytics_days",
    "analytics_summaries",
)
MEMBER_CLASSIFICATIONS = (
    "complete",
    "partial",
    "failed",
    "missing",
    "unavailable",
    "inconsistent",
    "malformed",
    "pending",
)
OPTIONAL_STATISTICS = ("statement_timing", "io_timing")
OPTIONAL_UNAVAILABLE_REASONS = (
    "available",
    "extension_unavailable",
    "not_collected",
    "privilege_unavailable",
)


def process_identity(
    *, process_id: str | None = None, started_at: datetime | None = None
) -> dict[str, str]:
    started = started_at or datetime.now(tz=UTC)
    return {
        "id": process_id or str(uuid4()),
        "started_at": started.astimezone(UTC).isoformat(),
    }


def _histogram() -> dict[str, Any]:
    return {
        "count": 0,
        "sum_seconds": 0.0,
        "buckets": [0] * (len(LATENCY_BUCKETS_SECONDS) + 1),
    }


def _record_histogram(histogram: dict[str, Any], duration_seconds: float) -> None:
    duration = max(0.0, float(duration_seconds))
    histogram["count"] += 1
    histogram["sum_seconds"] += duration
    for index, upper_bound in enumerate(LATENCY_BUCKETS_SECONDS):
        if duration <= upper_bound:
            histogram["buckets"][index] += 1
    histogram["buckets"][-1] += 1


def _pool_snapshot(database_pool: dict[str, int]) -> dict[str, int | None]:
    return {
        key: int(database_pool[key]) if key in database_pool else None
        for key in POOL_FIELDS
    }


def database_pool_health(pool: Any) -> dict[str, int]:
    """Normalize psycopg pool measures and legitimately absent counters."""
    stats = pool.get_stats()
    return {
        **{
            key: int(stats[key])
            for key in POOL_MEASURE_FIELDS
            if key in stats
        },
        **{key: int(stats.get(key, 0)) for key in POOL_COUNTER_FIELDS},
    }


def api_route(path: str) -> str:
    if path == "/livez":
        return "livez"
    if path == "/readyz":
        return "readyz"
    if path == "/operatorz":
        return "operator"
    if path == "/v1/players/search":
        return "player_search"
    if path.startswith("/v1/players/") and path.endswith("/verifytoken"):
        return "verification"
    if path.startswith("/v1/players/") and path.endswith("/refresh"):
        return "player_refresh"
    if path.startswith("/v1/refreshes/"):
        return "refresh_status"
    if path.startswith("/v1/players/"):
        return "player_read"
    if path == "/v1/leaderboards/live":
        return "leaderboard_live"
    if path.startswith("/v1/leaderboards/frozen"):
        return "leaderboard_frozen"
    if path.startswith("/v1/analytics/army"):
        return "army_analytics"
    if path.startswith("/v1/battles/") and path.endswith("/army"):
        return "battle_army"
    if path == "/v1/analytics/basic":
        return "basic_analytics"
    if path.startswith("/v1/users/"):
        return "public_user"
    if path.startswith("/v1/account/saved-tags"):
        return "saved_players"
    if path.startswith("/v1/account/groups"):
        return "groups"
    if path.startswith("/v1/account/exports"):
        return "exports"
    if path.startswith("/v1/account/providers"):
        return "providers"
    if path.startswith("/v1/account/verification"):
        return "verification"
    if path.startswith("/v1/account"):
        return "account"
    return "other"


def api_outcome(status_code: int, *, response_size_limited: bool = False) -> str:
    if response_size_limited:
        return "response_size_limit"
    if 200 <= status_code < 400:
        return "success"
    if 400 <= status_code < 500:
        return "expected_4xx"
    return "safe_5xx"


class ApiMetrics:
    """Bounded process-local private API facts behind one snapshot interface."""

    def __init__(
        self,
        *,
        process_id: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._identity = process_identity(
            process_id=process_id, started_at=started_at
        )
        self._lock = Lock()
        self._requests = {
            route: {
                "outcomes": dict.fromkeys(API_OUTCOMES, 0),
                "latency": _histogram(),
                "response_bytes": {"count": 0, "sum": 0, "max": 0},
            }
            for route in API_ROUTES
        }

    def record(
        self,
        path: str,
        status_code: int,
        duration_seconds: float,
        response_bytes: int,
        *,
        response_size_limited: bool = False,
    ) -> None:
        route = api_route(path)
        outcome = api_outcome(
            status_code, response_size_limited=response_size_limited
        )
        size = max(0, int(response_bytes))
        with self._lock:
            values = self._requests[route]
            values["outcomes"][outcome] += 1
            _record_histogram(values["latency"], duration_seconds)
            values["response_bytes"]["count"] += 1
            values["response_bytes"]["sum"] += size
            values["response_bytes"]["max"] = max(
                values["response_bytes"]["max"], size
            )

    def snapshot(self, database_pool: dict[str, int]) -> dict[str, Any]:
        with self._lock:
            requests = json.loads(json.dumps(self._requests))
        return {
            "schema_version": 1,
            "process": dict(self._identity),
            "database_pool": _pool_snapshot(database_pool),
            "latency_bucket_upper_bounds_seconds": list(LATENCY_BUCKETS_SECONDS)
            + [None],
            "requests": requests,
        }


class WorkerMetrics:
    """Bounded worker outcomes and existing stage/pool facts."""

    def __init__(
        self,
        *,
        process_id: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._identity = process_identity(
            process_id=process_id, started_at=started_at
        )
        self._lock = Lock()
        self._outcomes: Counter[str] = Counter()

    def record_outcome(self, outcome: str) -> None:
        category = outcome if outcome in WORKER_OUTCOMES else "other"
        with self._lock:
            self._outcomes[category] += 1

    def snapshot(
        self,
        *,
        stages: dict[str, Any],
        database_pool: dict[str, int],
        queue: dict[str, Any],
        spool: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            outcomes = {
                category: self._outcomes.get(category, 0)
                for category in WORKER_OUTCOMES
            }
        unknown_stages = set(stages) - set(WORKER_STAGES)
        if unknown_stages:
            raise ValueError("worker metrics contain an unknown stage")
        empty_stage = {
            "count": 0,
            "average_ms": None,
            "p50_upper_ms": None,
            "p95_upper_ms": None,
            "p99_upper_ms": None,
        }
        bounded_stages = {
            stage: deepcopy(stages.get(stage, empty_stage))
            for stage in WORKER_STAGES
        }
        bounded_queue = {
            name: queue.get(name)
            for name in (
                "pending",
                "waiting_retry",
                "waiting_dependency",
                "leased",
                "failed",
                "failed_count_capped",
                "oldest_due_seconds",
            )
        }
        raw_reason = spool.get("reason")
        spool_reason = (
            "storage_error"
            if isinstance(raw_reason, str) and raw_reason.startswith("storage_error:")
            else raw_reason
        )
        bounded_spool = {
            "ready": spool.get("ready"),
            "component": spool.get("component"),
            "reason": spool_reason,
        }
        return {
            "schema_version": 1,
            "captured_at": datetime.now(tz=UTC).isoformat(),
            "process": dict(self._identity),
            "stages": bounded_stages,
            "outcomes": outcomes,
            "database_pool": _pool_snapshot(database_pool),
            "queue": bounded_queue,
            "spool": bounded_spool,
        }


def write_private_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    """Atomically replace one process-private runtime snapshot."""
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def elapsed(started_at: float) -> float:
    return max(0.0, perf_counter() - started_at)


class OperatingFactsError(ValueError):
    """A fixed-class validation failure with no source data in its message."""

    def __init__(self, reason: str) -> None:
        if reason not in {"required_fact_invalid", "required_fact_missing"}:
            raise ValueError("invalid operating-facts reason")
        super().__init__(reason)
        self.reason = reason


def _canonical_digest(value: dict[str, Any], *, omit: str) -> str:
    canonical = {key: item for key, item in value.items() if key != omit}
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return sha256(encoded).hexdigest()


def _required(mapping: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(mapping, dict):
        raise OperatingFactsError("required_fact_invalid")
    if any(field not in mapping for field in fields):
        raise OperatingFactsError("required_fact_missing")
    return mapping


def _exact_keys(mapping: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    result = _required(mapping, fields)
    if set(result) != set(fields):
        raise OperatingFactsError("required_fact_invalid")
    return result


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperatingFactsError("required_fact_invalid")
    return value


def _nonnegative_number(value: Any) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise OperatingFactsError("required_fact_invalid")
    return value


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise OperatingFactsError("required_fact_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise OperatingFactsError("required_fact_invalid") from error
    if parsed.tzinfo is None:
        raise OperatingFactsError("required_fact_invalid")
    return parsed.astimezone(UTC)


def _validate_process_identity(value: Any) -> None:
    identity = _exact_keys(value, ("id", "started_at"))
    process_id = identity["id"]
    if not isinstance(process_id, str) or re.fullmatch(
        r"(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
        r"[0-9a-f]{4}-[0-9a-f]{12})",
        process_id,
    ) is None:
        raise OperatingFactsError("required_fact_invalid")
    _timestamp(identity["started_at"])


def _validate_pool(value: Any) -> None:
    pool = _exact_keys(value, POOL_FIELDS)
    for item in pool.values():
        _nonnegative_int(item)


def _validate_database_identity(value: Any) -> None:
    identity = _exact_keys(value, DATABASE_IDENTITY_FIELDS)
    system_identifier = identity["system_identifier"]
    # pg_control_system() exposes an unsigned 64-bit decimal identifier. Keep
    # the wire contract textual and bounded so arbitrary server output can
    # never enter the private snapshot.
    if (
        not isinstance(system_identifier, str)
        or re.fullmatch(r"[1-9][0-9]{0,19}", system_identifier) is None
        or int(system_identifier) > 2**64 - 1
    ):
        raise OperatingFactsError("required_fact_invalid")
    database_oid = _nonnegative_int(identity["database_oid"])
    if database_oid == 0 or database_oid > 2**32 - 1:
        raise OperatingFactsError("required_fact_invalid")


def _validate_histogram(value: Any) -> None:
    histogram = _exact_keys(value, ("count", "sum_seconds", "buckets"))
    count = _nonnegative_int(histogram["count"])
    total = _nonnegative_number(histogram["sum_seconds"])
    if count == 0 and total != 0:
        raise OperatingFactsError("required_fact_invalid")
    buckets = histogram["buckets"]
    if not isinstance(buckets, list) or len(buckets) != len(
        LATENCY_BUCKETS_SECONDS
    ) + 1:
        raise OperatingFactsError("required_fact_invalid")
    prior = 0
    for item in buckets:
        current = _nonnegative_int(item)
        if current < prior or current > count:
            raise OperatingFactsError("required_fact_invalid")
        prior = current
    if buckets[-1] != count:
        raise OperatingFactsError("required_fact_invalid")


def _validate_api(value: Any) -> None:
    api = _exact_keys(
        value,
        (
            "schema_version",
            "process",
            "database_pool",
            "latency_bucket_upper_bounds_seconds",
            "requests",
        ),
    )
    if api["schema_version"] != 1:
        raise OperatingFactsError("required_fact_invalid")
    _validate_process_identity(api["process"])
    _validate_pool(api["database_pool"])
    if api["latency_bucket_upper_bounds_seconds"] != [
        *LATENCY_BUCKETS_SECONDS,
        None,
    ]:
        raise OperatingFactsError("required_fact_invalid")
    routes = _exact_keys(api["requests"], API_ROUTES)
    for request in routes.values():
        request = _exact_keys(
            request, ("outcomes", "latency", "response_bytes")
        )
        outcomes = _exact_keys(request["outcomes"], API_OUTCOMES)
        for count in outcomes.values():
            _nonnegative_int(count)
        _validate_histogram(request["latency"])
        sizes = _exact_keys(request["response_bytes"], ("count", "sum", "max"))
        for size in sizes.values():
            _nonnegative_int(size)
        request_count = sum(outcomes.values())
        if (
            request_count != request["latency"]["count"]
            or request_count != sizes["count"]
            or (sizes["count"] == 0 and (sizes["sum"] != 0 or sizes["max"] != 0))
            or sizes["max"] > sizes["sum"]
        ):
            raise OperatingFactsError("required_fact_invalid")


def _validate_worker(value: Any) -> None:
    worker = _exact_keys(
        value,
        (
            "schema_version",
            "captured_at",
            "process",
            "stages",
            "outcomes",
            "database_pool",
            "queue",
            "spool",
        ),
    )
    if worker["schema_version"] != 1:
        raise OperatingFactsError("required_fact_invalid")
    _timestamp(worker["captured_at"])
    _validate_process_identity(worker["process"])
    _validate_pool(worker["database_pool"])
    stages = _exact_keys(worker["stages"], WORKER_STAGES)
    for stage in stages.values():
        stage = _exact_keys(
            stage,
            (
                "count",
                "average_ms",
                "p50_upper_ms",
                "p95_upper_ms",
                "p99_upper_ms",
            ),
        )
        count = _nonnegative_int(stage["count"])
        for name in ("average_ms", "p50_upper_ms", "p95_upper_ms", "p99_upper_ms"):
            if stage[name] is not None:
                _nonnegative_number(stage[name])
        if count == 0 and any(stage[name] is not None for name in stage if name != "count"):
            raise OperatingFactsError("required_fact_invalid")
    outcomes = _exact_keys(worker["outcomes"], WORKER_OUTCOMES)
    for count in outcomes.values():
        _nonnegative_int(count)
    queue = _exact_keys(
        worker["queue"],
        (
            "pending",
            "waiting_retry",
            "waiting_dependency",
            "leased",
            "failed",
            "failed_count_capped",
            "oldest_due_seconds",
        ),
    )
    for name in (
        "pending",
        "waiting_retry",
        "waiting_dependency",
        "leased",
        "failed",
    ):
        _nonnegative_int(queue[name])
    if not isinstance(queue["failed_count_capped"], bool):
        raise OperatingFactsError("required_fact_invalid")
    if queue["oldest_due_seconds"] is not None:
        _nonnegative_number(queue["oldest_due_seconds"])
    spool = _exact_keys(worker["spool"], ("ready", "component", "reason"))
    if (
        not isinstance(spool["ready"], bool)
        or spool["component"] != "spool"
        or spool["reason"]
        not in {
            "ready",
            "degraded_capacity",
            "degraded_free_space",
            "degraded_free_inodes",
            "storage_error",
        }
    ):
        raise OperatingFactsError("required_fact_invalid")


def _validate_workers(value: Any, database_captured_at: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise OperatingFactsError("required_fact_invalid")
    database_time = _timestamp(database_captured_at)
    for worker in value:
        _validate_worker(worker)
        age = (database_time - _timestamp(worker["captured_at"])).total_seconds()
        if not 0 <= age <= WORKER_SNAPSHOT_MAX_AGE_SECONDS:
            raise OperatingFactsError("required_fact_invalid")


def _validate_collector(value: Any) -> None:
    collector = _exact_keys(
        value,
        (
            "schema_version",
            "process",
            "database_pool",
            "stages",
            "outcomes",
            "spool",
        ),
    )
    if collector["schema_version"] != 1:
        raise OperatingFactsError("required_fact_invalid")
    _validate_process_identity(collector["process"])
    pool = _exact_keys(
        collector["database_pool"],
        (
            "max_connections",
            "acquired_connections",
            "idle_connections",
            "empty_acquires_total",
            "cancelled_requests_total",
            "acquire_wait_seconds_total",
        ),
    )
    for item in pool.values():
        _nonnegative_number(item)
    stages = _exact_keys(collector["stages"], COLLECTOR_STAGES)
    for histogram in stages.values():
        _validate_histogram(histogram)
    outcomes = _exact_keys(collector["outcomes"], ("jobs", "official_api"))
    jobs = _exact_keys(outcomes["jobs"], COLLECTOR_JOB_OUTCOMES)
    official_api = _exact_keys(outcomes["official_api"], COLLECTOR_API_OUTCOMES)
    for count in (*jobs.values(), *official_api.values()):
        _nonnegative_int(count)
    spool = _exact_keys(collector["spool"], SPOOL_FIELDS)
    for item in spool.values():
        _nonnegative_int(item)


def _validate_queue(value: Any) -> None:
    queue = _exact_keys(
        value,
        (
            "by_status",
            "oldest_due_seconds",
            "retry_jobs",
            "dependency_jobs",
            "valid_leases",
            "expired_recoverable_leases",
            "expired_unrecoverable_leases",
        ),
    )
    statuses = _exact_keys(queue["by_status"], QUEUE_STATUSES)
    for count in statuses.values():
        _nonnegative_int(count)
    for name in (
        "retry_jobs",
        "dependency_jobs",
        "valid_leases",
        "expired_recoverable_leases",
        "expired_unrecoverable_leases",
    ):
        _nonnegative_int(queue[name])
    if queue["oldest_due_seconds"] is not None:
        _nonnegative_number(queue["oldest_due_seconds"])


def _validate_failures(value: Any) -> None:
    failures = _exact_keys(value, ("total", "by_category"))
    total = _nonnegative_int(failures["total"])
    categories = _exact_keys(failures["by_category"], FAILURE_CATEGORIES)
    for count in categories.values():
        _nonnegative_int(count)
    if sum(categories.values()) != total:
        raise OperatingFactsError("required_fact_invalid")


def _validate_boundary(value: Any) -> None:
    boundary = _exact_keys(value, ("active_count", "artifacts"))
    active_count = _nonnegative_int(boundary["active_count"])
    artifacts = boundary["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > 32:
        raise OperatingFactsError("required_fact_invalid")
    if active_count != sum(
        artifact.get("state") not in {"published", "superseded"}
        for artifact in artifacts
        if isinstance(artifact, dict)
    ):
        raise OperatingFactsError("required_fact_invalid")
    seen: set[tuple[int, int, str]] = set()
    for artifact in artifacts:
        artifact = _exact_keys(
            artifact,
            (
                "generation_id",
                "generation",
                "artifact",
                "state",
                "boundary_at",
                "target_at",
                "target_rule",
                "member_classifications",
                "publication_outcome",
                "queued_work",
                "valid_leases",
                "due_retries",
                "dependency_transitions",
                "recoverable_expired_leases",
                "unrecoverable_expired_leases",
                "coordinator_transition",
                "blocking_failures",
            ),
        )
        generation_id = _nonnegative_int(artifact["generation_id"])
        generation = _nonnegative_int(artifact["generation"])
        kind = artifact["artifact"]
        if generation_id == 0 or generation == 0 or kind not in BOUNDARY_ARTIFACTS:
            raise OperatingFactsError("required_fact_invalid")
        identity = (generation_id, generation, kind)
        if identity in seen:
            raise OperatingFactsError("required_fact_invalid")
        seen.add(identity)
        if artifact["state"] not in BOUNDARY_STATES:
            raise OperatingFactsError("required_fact_invalid")
        _timestamp(artifact["boundary_at"])
        _timestamp(artifact["target_at"])
        if artifact["target_rule"] != "boundary-delay-v1":
            raise OperatingFactsError("required_fact_invalid")
        classifications = _exact_keys(
            artifact["member_classifications"], MEMBER_CLASSIFICATIONS
        )
        for count in classifications.values():
            _nonnegative_int(count)
        publication_outcome = artifact["publication_outcome"]
        if publication_outcome not in {
            "not_published",
            "published",
            "superseded",
            "failed",
        }:
            raise OperatingFactsError("required_fact_invalid")
        expected_publication_outcome = {
            "published": "published",
            "superseded": "superseded",
            "failed": "failed",
        }.get(artifact["state"], "not_published")
        if publication_outcome != expected_publication_outcome:
            raise OperatingFactsError("required_fact_invalid")
        for name in (
            "queued_work",
            "valid_leases",
            "due_retries",
            "dependency_transitions",
            "recoverable_expired_leases",
            "unrecoverable_expired_leases",
            "blocking_failures",
        ):
            _nonnegative_int(artifact[name])
        if not isinstance(artifact["coordinator_transition"], bool):
            raise OperatingFactsError("required_fact_invalid")


def _validate_database(value: Any) -> None:
    database = _exact_keys(
        value,
        (
            "schema_version",
            "captured_at",
            "identity",
            "isolation",
            "contract_version",
            "migrations",
            "queues",
            "processed",
            "failures",
            "boundary",
            "storage",
        ),
    )
    if (
        database["schema_version"] != 1
        or database["isolation"] != "repeatable_read_read_only"
        or database["contract_version"] != 5
    ):
        raise OperatingFactsError("required_fact_invalid")
    _timestamp(database["captured_at"])
    _validate_database_identity(database["identity"])
    migrations = database["migrations"]
    if not isinstance(migrations, list) or any(
        not isinstance(item, dict) for item in migrations
    ) or [item.get("version") for item in migrations] != list(range(1, 19)):
        raise OperatingFactsError("required_fact_invalid")
    for item in migrations:
        _exact_keys(item, ("version", "applied_at"))
        _timestamp(item["applied_at"])
    queues = _exact_keys(database["queues"], ("collector", "python"))
    for queue in queues.values():
        _validate_queue(queue)
    processed = _exact_keys(
        database["processed"], ("observations", "facts", "results")
    )
    for name, categories in (
        ("observations", OBSERVATION_CATEGORIES),
        ("facts", FACT_CATEGORIES),
        ("results", RESULT_CATEGORIES),
    ):
        values = _exact_keys(processed[name], categories)
        for count in values.values():
            _nonnegative_int(count)
    failures = _exact_keys(
        database["failures"],
        ("active_boundary_blocking", "retained_historical"),
    )
    for failure in failures.values():
        _validate_failures(failure)
    _validate_boundary(database["boundary"])
    storage = _exact_keys(
        database["storage"], ("relations", "wal", "optional_statistics")
    )
    relations = storage["relations"]
    if not isinstance(relations, list) or not relations:
        raise OperatingFactsError("required_fact_invalid")
    seen_relations: set[str] = set()
    for relation in relations:
        relation = _exact_keys(
            relation,
            ("name", "table_bytes", "index_bytes", "toast_bytes", "total_bytes"),
        )
        if relation["name"] not in RELATION_NAMES or relation["name"] in seen_relations:
            raise OperatingFactsError("required_fact_invalid")
        seen_relations.add(relation["name"])
        for name in ("table_bytes", "index_bytes", "toast_bytes", "total_bytes"):
            _nonnegative_int(relation[name])
        if relation["total_bytes"] < relation["table_bytes"]:
            raise OperatingFactsError("required_fact_invalid")
    if seen_relations != set(RELATION_NAMES):
        raise OperatingFactsError("required_fact_missing")
    wal = _exact_keys(storage["wal"], ("retained_bytes",))
    _nonnegative_int(wal["retained_bytes"])
    optional = _exact_keys(storage["optional_statistics"], OPTIONAL_STATISTICS)
    for statistic in optional.values():
        statistic = _exact_keys(statistic, ("value", "reason"))
        if statistic["reason"] not in OPTIONAL_UNAVAILABLE_REASONS:
            raise OperatingFactsError("required_fact_invalid")
        if (statistic["reason"] == "available") != (statistic["value"] is not None):
            raise OperatingFactsError("required_fact_invalid")
        if statistic["value"] is not None:
            _nonnegative_number(statistic["value"])


def _validate_spool_config(value: Any) -> None:
    config = _exact_keys(value, SPOOL_CONFIG_FIELDS)
    for name, item in config.items():
        if _nonnegative_int(item) == 0 and name in {
            "max_body_bytes",
            "max_bytes",
            "max_objects",
        }:
            raise OperatingFactsError("required_fact_invalid")


def _comparison(
    current: dict[str, Any], previous: dict[str, Any] | None
) -> tuple[dict[str, Any], bool]:
    empty = {
        "previous_snapshot_id": None,
        "interval_seconds": None,
        "deltas": None,
        "runway": None,
        "reason": "previous_snapshot_missing",
    }
    if previous is None:
        return empty, False
    try:
        previous = _required(
            previous,
            (
                "schema_version",
                "captured_at",
                "snapshot_id",
                "database",
                "configuration",
                "processes",
                "check",
                "comparison",
            ),
        )
        if (
            previous["schema_version"] != 1
            or not isinstance(previous["snapshot_id"], str)
            or previous["snapshot_id"]
            != _canonical_digest(previous, omit="snapshot_id")
        ):
            return {**empty, "reason": "invalid_previous_snapshot"}, True
        _validate_database(previous["database"])
        if previous["captured_at"] != previous["database"]["captured_at"]:
            return {**empty, "reason": "invalid_previous_snapshot"}, True
        if previous["database"]["identity"] != current["database"]["identity"]:
            return {**empty, "reason": "database_identity_mismatch"}, False
        previous_processes = _exact_keys(
            previous["processes"],
            ("collector", "python_api", "python_workers"),
        )
        _validate_collector(previous_processes["collector"])
        _validate_api(previous_processes["python_api"])
        _validate_workers(
            previous_processes["python_workers"],
            previous["database"]["captured_at"],
        )
        previous_check = _exact_keys(
            previous["check"], ("status", "exit_code", "reasons")
        )
        expected_exit = {
            "healthy": 0,
            "objective_failure": 1,
            "indeterminate": 2,
        }
        if (
            previous_check["status"] not in expected_exit
            or previous_check["exit_code"]
            != expected_exit[previous_check["status"]]
            or not isinstance(previous_check["reasons"], list)
            or any(reason not in CHECK_REASONS for reason in previous_check["reasons"])
        ):
            return {**empty, "reason": "invalid_previous_snapshot"}, True
        previous_configuration = _exact_keys(
            previous["configuration"], ("fingerprint", "spool")
        )
        _validate_spool_config(previous_configuration["spool"])
        if previous_configuration["fingerprint"] != _canonical_digest(
            previous_configuration["spool"], omit=""
        ):
            return {**empty, "reason": "invalid_previous_snapshot"}, True
        current_time = _timestamp(current["captured_at"])
        previous_time = _timestamp(previous["captured_at"])
        interval = (current_time - previous_time).total_seconds()
        if interval <= 0:
            return {**empty, "reason": "invalid_previous_snapshot"}, True
        if previous_configuration["fingerprint"] != current["configuration"][
            "fingerprint"
        ]:
            return {**empty, "reason": "configuration_changed"}, False
        current_relations = {
            item["name"]: item for item in current["database"]["storage"]["relations"]
        }
        previous_relations = {
            item["name"]: item
            for item in previous["database"]["storage"]["relations"]
        }
        if set(current_relations) != set(previous_relations):
            return {**empty, "reason": "relation_set_changed"}, False
        relation_deltas = [
            {
                "name": name,
                **{
                    field: current_relations[name][field]
                    - previous_relations[name][field]
                    for field in (
                        "table_bytes",
                        "index_bytes",
                        "toast_bytes",
                        "total_bytes",
                    )
                },
            }
            for name in sorted(current_relations)
        ]
        current_spool = current["processes"]["collector"]["spool"]
        previous_spool = previous["processes"]["collector"]["spool"]
        logical_fields = (
            "final_bytes",
            "temporary_bytes",
            "abandoned_temporary_bytes",
            "reserved_bytes",
        )
        current_logical = sum(current_spool[name] for name in logical_fields)
        previous_logical = sum(previous_spool[name] for name in logical_fields)
        spool_delta = current_logical - previous_logical
        headroom = current["configuration"]["spool"]["max_bytes"] - (
            current_logical + current["configuration"]["spool"]["max_body_bytes"]
        )
        days = None
        if spool_delta > 0 and headroom >= 0:
            days = headroom * interval / spool_delta / 86400
        return (
            {
                "previous_snapshot_id": previous["snapshot_id"],
                "interval_seconds": interval,
                "deltas": {
                    "relations": relation_deltas,
                    "wal_retained_bytes": current["database"]["storage"]["wal"][
                        "retained_bytes"
                    ]
                    - previous["database"]["storage"]["wal"]["retained_bytes"],
                    "spool_logical_bytes": spool_delta,
                },
                "runway": {"spool_days_to_hard_capacity": days},
                "reason": None,
            },
            False,
        )
    except (OperatingFactsError, KeyError, TypeError, ValueError):
        return {**empty, "reason": "invalid_previous_snapshot"}, True


def build_operating_snapshot(
    *,
    database: dict[str, Any],
    collector: dict[str, Any],
    python_api: dict[str, Any],
    python_workers: list[dict[str, Any]],
    spool_config: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate bounded facts, evaluate objective failure, and digest one snapshot."""
    try:
        _validate_database(database)
        _validate_collector(collector)
        _validate_api(python_api)
        _validate_workers(python_workers, database["captured_at"])
        identities = [
            collector["process"]["id"],
            python_api["process"]["id"],
            *(worker["process"]["id"] for worker in python_workers),
        ]
        if len(set(identities)) != len(identities):
            raise OperatingFactsError("required_fact_invalid")
        _validate_spool_config(spool_config)
    except OperatingFactsError as error:
        result = {
            "schema_version": 1,
            "captured_at": datetime.now(tz=UTC).isoformat(),
            "check": {
                "status": "indeterminate",
                "exit_code": 2,
                "reasons": [error.reason],
            },
            "configuration": None,
            "processes": None,
            "database": None,
            "comparison": None,
        }
        result["snapshot_id"] = _canonical_digest(result, omit="snapshot_id")
        return result

    configuration = {
        "fingerprint": _canonical_digest(spool_config, omit=""),
        "spool": deepcopy(spool_config),
    }
    result = {
        "schema_version": 1,
        "captured_at": database["captured_at"],
        "check": {"status": "healthy", "exit_code": 0, "reasons": []},
        "configuration": configuration,
        "processes": {
            "collector": deepcopy(collector),
            "python_api": deepcopy(python_api),
            "python_workers": deepcopy(python_workers),
        },
        "database": deepcopy(database),
        "comparison": None,
    }
    reasons: set[str] = set()
    spool = collector["spool"]
    logical_bytes = sum(
        spool[name]
        for name in (
            "final_bytes",
            "temporary_bytes",
            "abandoned_temporary_bytes",
            "reserved_bytes",
        )
    )
    logical_objects = sum(
        spool[name]
        for name in (
            "final_objects",
            "temporary_objects",
            "abandoned_temporary_objects",
            "reserved_objects",
        )
    )
    if logical_bytes + spool_config["max_body_bytes"] > spool_config["max_bytes"]:
        reasons.add("spool_bytes_capacity_exceeded")
    if logical_objects + 1 > spool_config["max_objects"]:
        reasons.add("spool_objects_capacity_exceeded")
    if spool["free_bytes"] < (
        spool_config["free_space_floor"] + spool_config["max_body_bytes"]
    ):
        reasons.add("spool_free_space_below_floor")
    if spool["free_inodes"] < spool_config["free_inode_floor"] + 1:
        reasons.add("spool_free_inodes_below_floor")
    if database["failures"]["active_boundary_blocking"]["total"]:
        reasons.add("active_boundary_failure")
    for artifact in database["boundary"]["artifacts"]:
        # Published and superseded rows are retained publication history. They
        # must remain visible in the snapshot, but cannot block the current
        # operating check or inflate active_count.
        if artifact["state"] in {"published", "superseded"}:
            continue
        if artifact["unrecoverable_expired_leases"]:
            reasons.add("active_boundary_unrecoverable_lease")
        progress_paths = sum(
            artifact[name]
            for name in (
                "queued_work",
                "valid_leases",
                "due_retries",
                "dependency_transitions",
                "recoverable_expired_leases",
            )
        )
        if artifact["coordinator_transition"]:
            progress_paths += 1
        if artifact["blocking_failures"]:
            reasons.add("active_boundary_failure")
        if progress_paths == 0:
            reasons.add("active_boundary_blocked")
    comparison, invalid_previous = _comparison(result, previous)
    result["comparison"] = comparison
    if invalid_previous:
        reasons.add("invalid_previous_snapshot")
    if reasons:
        result["check"] = {
            "status": "indeterminate" if invalid_previous else "objective_failure",
            "exit_code": 2 if invalid_previous else 1,
            "reasons": sorted(reasons),
        }
    result["snapshot_id"] = _canonical_digest(result, omit="snapshot_id")
    return result
