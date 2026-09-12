from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
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
