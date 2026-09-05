from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from clashlens.operating import (
    COLLECTOR_STAGES,
    LATENCY_BUCKETS_SECONDS,
    RELATION_NAMES,
    WORKER_SNAPSHOT_MAX_AGE_SECONDS,
    ApiMetrics,
    WorkerMetrics,
    api_route,
    build_operating_snapshot,
    database_pool_health,
    write_private_snapshot,
)

CAPTURED_AT = "2026-08-28T20:00:00+00:00"


def _pool() -> dict[str, int]:
    return {
        "pool_min": 1,
        "pool_max": 4,
        "pool_size": 2,
        "pool_available": 1,
        "requests_waiting": 0,
        "requests_num": 3,
        "requests_queued": 0,
        "requests_wait_ms": 2,
        "usage_ms": 5,
    }


def _failures(total: int = 0, *, category: str = "other") -> dict[str, object]:
    categories = {
        name: 0
        for name in (
            "storage",
            "transport",
            "lease_expired",
            "dependency",
            "unsupported",
            "data_quality",
            "other",
        )
    }
    categories[category] = total
    return {"total": total, "by_category": categories}


def _queue() -> dict[str, object]:
    return {
        "by_status": {
            name: 0
            for name in (
                "pending",
                "leased",
                "waiting_retry",
                "waiting_dependency",
                "complete",
                "failed",
                "cancelled",
            )
        },
        "oldest_due_seconds": None,
        "retry_jobs": 0,
        "dependency_jobs": 0,
        "valid_leases": 0,
        "expired_recoverable_leases": 0,
        "expired_unrecoverable_leases": 0,
    }


def _database(*, captured_at: str = CAPTURED_AT) -> dict[str, object]:
    return {
        "schema_version": 1,
        "captured_at": captured_at,
        "identity": {
            "system_identifier": "1234567890",
            "database_oid": 16384,
        },
        "isolation": "repeatable_read_read_only",
        "contract_version": 5,
        "migrations": [
            {"version": version, "applied_at": "2026-08-28T19:00:00+00:00"}
            for version in range(1, 19)
        ],
        "queues": {"collector": _queue(), "python": _queue()},
        "processed": {
            "observations": {
                "profile": 0,
                "battle_log": 0,
                "global_player_rankings": 0,
            },
            "facts": {
                "ranked_day_complete": 0,
                "ranked_day_partial": 0,
                "ranked_day_inconsistent": 0,
                "ranked_day_malformed": 0,
                "army_decoded": 0,
                "army_failed": 0,
            },
            "results": {
                "leaderboard_snapshots": 0,
                "army_analytics_days": 0,
                "analytics_summaries": 0,
            },
        },
        "failures": {
            "active_boundary_blocking": _failures(),
            "retained_historical": _failures(),
        },
        "boundary": {"active_count": 0, "artifacts": []},
        "storage": {
            "relations": [
                {
                    "name": name,
                    "table_bytes": 100,
                    "index_bytes": 20,
                    "toast_bytes": 0,
                    "total_bytes": 120,
                }
                for name in RELATION_NAMES
            ],
            "wal": {"retained_bytes": 10},
            "optional_statistics": {
                "statement_timing": {
                    "value": None,
                    "reason": "extension_unavailable",
                },
                "io_timing": {
                    "value": None,
                    "reason": "privilege_unavailable",
                },
            },
        },
    }


def _collector() -> dict[str, object]:
    empty_histogram = {
        "count": 0,
        "sum_seconds": 0,
        "buckets": [0] * (len(LATENCY_BUCKETS_SECONDS) + 1),
    }
    return {
        "schema_version": 1,
        "process": {
            "id": "11" * 16,
            "started_at": "2026-08-28T19:30:00+00:00",
        },
        "database_pool": {
            "max_connections": 32,
            "acquired_connections": 1,
            "idle_connections": 3,
            "empty_acquires_total": 0,
            "cancelled_requests_total": 0,
            "acquire_wait_seconds_total": 0.1,
        },
        "stages": {
            stage: deepcopy(empty_histogram) for stage in COLLECTOR_STAGES
        },
        "outcomes": {
            "jobs": {
                "scheduled": 0,
                "claimed": 0,
                "handled": 0,
                "error": 0,
            },
            "official_api": {
                "success": 0,
                "expected_4xx": 0,
                "safe_5xx": 0,
                "transport_failure": 0,
                "other": 0,
            },
        },
        "spool": {
            "final_bytes": 10,
            "final_objects": 1,
            "temporary_bytes": 0,
            "temporary_objects": 0,
            "abandoned_temporary_bytes": 0,
            "abandoned_temporary_objects": 0,
            "reserved_bytes": 0,
            "reserved_objects": 0,
            "high_water_bytes": 10,
            "free_bytes": 10_000,
            "free_inodes": 1000,
        },
    }


def _api() -> dict[str, object]:
    return ApiMetrics(
        process_id="00000000-0000-4000-8000-000000000082",
        started_at=datetime(2026, 8, 28, 19, 31, tzinfo=UTC),
    ).snapshot(_pool())


def _worker(*, captured_at: str = CAPTURED_AT) -> dict[str, object]:
    snapshot = WorkerMetrics(
        process_id="00000000-0000-4000-8000-000000000083",
        started_at=datetime(2026, 8, 28, 19, 32, tzinfo=UTC),
    ).snapshot(
        stages={},
        database_pool=_pool(),
        queue={
            "pending": 0,
            "waiting_retry": 0,
            "waiting_dependency": 0,
            "leased": 0,
            "failed": 0,
            "failed_count_capped": False,
            "oldest_due_seconds": None,
        },
        spool={"ready": True, "component": "spool", "reason": "ready"},
    )
    snapshot["captured_at"] = captured_at
    return snapshot


def _config() -> dict[str, int]:
    return {
        "max_body_bytes": 100,
        "max_bytes": 1000,
        "max_objects": 100,
        "free_space_floor": 100,
        "free_inode_floor": 10,
    }


def _snapshot(
    *,
    database: dict[str, object] | None = None,
    collector: dict[str, object] | None = None,
    spool_config: dict[str, int] | None = None,
    previous: dict[str, object] | None = None,
) -> dict[str, object]:
    database_value = database or _database()
    return build_operating_snapshot(
        database=database_value,
        collector=collector or _collector(),
        python_api=_api(),
        python_workers=[_worker(captured_at=str(database_value["captured_at"]))],
        spool_config=spool_config or _config(),
        previous=previous,
    )


def _artifact(**changes: object) -> dict[str, object]:
    artifact = {
        "generation_id": 1,
        "generation": 1,
        "artifact": "snapshot",
        "state": "pending",
        "boundary_at": "2026-08-28T05:00:00+00:00",
        "target_at": "2026-08-28T05:10:00+00:00",
        "target_rule": "boundary-delay-v1",
        "member_classifications": {
            "complete": 0,
            "partial": 0,
            "failed": 0,
            "missing": 0,
            "unavailable": 0,
            "inconsistent": 0,
            "malformed": 0,
            "pending": 1,
        },
        "publication_outcome": "not_published",
        "queued_work": 0,
        "valid_leases": 0,
        "due_retries": 0,
        "dependency_transitions": 0,
        "recoverable_expired_leases": 0,
        "unrecoverable_expired_leases": 0,
        "coordinator_transition": False,
        "blocking_failures": 0,
    }
    artifact.update(changes)
    return artifact


@pytest.mark.parametrize(
    ("path", "route"),
    (
        ("/v1/refreshes/refresh-id", "refresh_status"),
        ("/v1/account/saved-tags", "saved_players"),
        ("/v1/account/saved-tags/%232PP", "saved_players"),
        ("/v1/players/%232PP/verifytoken", "verification"),
    ),
)
def test_api_route_uses_shipped_bounded_route_categories(
    path: str, route: str
) -> None:
    assert api_route(path) == route


def test_worker_metrics_are_process_scoped_and_bounded(tmp_path) -> None:
    metrics = WorkerMetrics(
        process_id="00000000-0000-4000-8000-000000000081",
        started_at=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
    )
    metrics.record_outcome("processed")
    metrics.record_outcome("dynamic-outcome-that-must-not-be-a-label")

    snapshot = metrics.snapshot(
        stages={"python_claim": {"count": 2, "average_ms": 1.0}},
        database_pool={"pool_size": 4, "pool_available": 3},
        queue={"pending": 1},
        spool={"ready": True},
    )
    path = tmp_path / "private" / "worker.json"
    write_private_snapshot(path, snapshot)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["process"] == {
        "id": "00000000-0000-4000-8000-000000000081",
        "started_at": "2026-08-28T20:00:00+00:00",
    }
    assert written["outcomes"]["processed"] == 1
    assert written["outcomes"]["other"] == 1
    assert "dynamic-outcome" not in json.dumps(written)
    assert path.stat().st_mode & 0o777 == 0o600


def test_private_worker_snapshot_is_atomically_replaced(tmp_path) -> None:
    path = tmp_path / "worker.json"
    write_private_snapshot(path, {"schema_version": 1, "value": 1})
    write_private_snapshot(path, {"schema_version": 1, "value": 2})

    assert json.loads(path.read_text(encoding="utf-8"))["value"] == 2
    assert list(tmp_path.iterdir()) == [path]


def test_empty_healthy_snapshot_is_bounded_and_optional_stats_can_be_unavailable() -> None:
    result = _snapshot()

    assert result["check"] == {"status": "healthy", "exit_code": 0, "reasons": []}
    assert result["comparison"]["reason"] == "previous_snapshot_missing"
    assert result["comparison"]["deltas"] is None
    assert len(result["snapshot_id"]) == 64
    assert "#" not in json.dumps(result)


def test_worker_snapshot_freshness_is_required_and_bounded() -> None:
    database = _database()
    database_time = datetime.fromisoformat(CAPTURED_AT)
    worker = _worker()
    worker["captured_at"] = (
        database_time - timedelta(seconds=WORKER_SNAPSHOT_MAX_AGE_SECONDS)
    ).isoformat()

    accepted = build_operating_snapshot(
        database=database,
        collector=_collector(),
        python_api=_api(),
        python_workers=[worker],
        spool_config=_config(),
    )
    assert accepted["check"]["exit_code"] == 0

    missing = _worker()
    del missing["captured_at"]
    missing_result = build_operating_snapshot(
        database=database,
        collector=_collector(),
        python_api=_api(),
        python_workers=[missing],
        spool_config=_config(),
    )
    assert missing_result["check"]["exit_code"] == 2
    assert missing_result["check"]["reasons"] == ["required_fact_missing"]

    invalid_workers = []
    stale = _worker()
    stale["captured_at"] = (
        database_time
        - timedelta(seconds=WORKER_SNAPSHOT_MAX_AGE_SECONDS, microseconds=1)
    ).isoformat()
    invalid_workers.append(stale)
    future = _worker()
    future["captured_at"] = (database_time + timedelta(microseconds=1)).isoformat()
    invalid_workers.append(future)

    for invalid in invalid_workers:
        result = build_operating_snapshot(
            database=database,
            collector=_collector(),
            python_api=_api(),
            python_workers=[invalid],
            spool_config=_config(),
        )
        assert result["check"] == {
            "status": "indeterminate",
            "exit_code": 2,
            "reasons": ["required_fact_invalid"],
        }


@pytest.mark.parametrize(
    "mutation",
    ("outcome_count", "latency_count", "response_count", "latency_sum", "response_sum"),
)
def test_contradictory_api_measurements_are_indeterminate(mutation: str) -> None:
    api = _api()
    request = api["requests"]["account"]
    if mutation == "outcome_count":
        request["outcomes"]["success"] = 1
    elif mutation == "latency_count":
        request["latency"]["count"] = 1
        request["latency"]["buckets"][-1] = 1
    elif mutation == "response_count":
        request["response_bytes"]["count"] = 1
    elif mutation == "latency_sum":
        request["latency"]["sum_seconds"] = 1
    else:
        request["response_bytes"]["sum"] = 1

    result = build_operating_snapshot(
        database=_database(),
        collector=_collector(),
        python_api=api,
        python_workers=[_worker()],
        spool_config=_config(),
    )

    assert result["check"] == {
        "status": "indeterminate",
        "exit_code": 2,
        "reasons": ["required_fact_invalid"],
    }


def test_retained_historical_failure_and_old_queue_age_do_not_fail() -> None:
    database = _database()
    database["failures"]["retained_historical"] = _failures(
        4, category="data_quality"
    )
    database["queues"]["python"]["oldest_due_seconds"] = 999_999

    assert _snapshot(database=database)["check"]["exit_code"] == 0


def test_terminal_publication_outcomes_remain_visible_but_inactive() -> None:
    database = _database()
    published = _artifact(state="published", publication_outcome="published")
    superseded = _artifact(
        generation=2,
        state="superseded",
        publication_outcome="superseded",
    )
    database["boundary"] = {
        "active_count": 0,
        "artifacts": [published, superseded],
    }

    result = _snapshot(database=database)

    assert result["check"]["exit_code"] == 0
    assert [item["publication_outcome"] for item in result["database"]["boundary"]["artifacts"]] == [
        "published",
        "superseded",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("queued_work", 1),
        ("valid_leases", 1),
        ("due_retries", 1),
        ("dependency_transitions", 1),
        ("recoverable_expired_leases", 1),
        ("coordinator_transition", True),
    ),
)
def test_each_legal_active_boundary_progress_path_exits_zero(
    field: str, value: object
) -> None:
    database = _database()
    database["boundary"] = {"active_count": 1, "artifacts": [_artifact(**{field: value})]}

    assert _snapshot(database=database)["check"]["exit_code"] == 0


def test_old_target_alone_is_not_stuck_but_no_progress_path_is() -> None:
    database = _database()
    database["boundary"] = {
        "active_count": 1,
        "artifacts": [_artifact(coordinator_transition=True)],
    }
    assert _snapshot(database=database)["check"]["exit_code"] == 0

    database["boundary"]["artifacts"] = [_artifact()]
    result = _snapshot(database=database)
    assert result["check"] == {
        "status": "objective_failure",
        "exit_code": 1,
        "reasons": ["active_boundary_blocked"],
    }


@pytest.mark.parametrize(
    "artifact",
    (
        _artifact(blocking_failures=1, queued_work=1),
        _artifact(unrecoverable_expired_leases=1, queued_work=1),
    ),
)
def test_active_blocking_failure_or_unrecoverable_lease_exits_one(
    artifact: dict[str, object],
) -> None:
    database = _database()
    database["boundary"] = {"active_count": 1, "artifacts": [artifact]}

    result = _snapshot(database=database)
    assert result["check"]["exit_code"] == 1


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("final_bytes", 901, "spool_bytes_capacity_exceeded"),
        ("final_objects", 100, "spool_objects_capacity_exceeded"),
        ("free_bytes", 199, "spool_free_space_below_floor"),
        ("free_inodes", 10, "spool_free_inodes_below_floor"),
    ),
)
def test_configured_hard_spool_violations_exit_one(
    field: str, value: int, reason: str
) -> None:
    collector = _collector()
    collector["spool"][field] = value

    result = _snapshot(collector=collector)
    assert result["check"]["exit_code"] == 1
    assert reason in result["check"]["reasons"]


def test_missing_required_fact_and_dynamic_stage_exit_two_without_source_echo() -> None:
    database = _database()
    del database["storage"]["wal"]
    missing = _snapshot(database=database)
    assert missing["check"]["exit_code"] == 2
    assert missing["database"] is None

    worker = WorkerMetrics().snapshot(
        stages={},
        database_pool=_pool(),
        queue={
            "pending": 0,
            "waiting_retry": 0,
            "waiting_dependency": 0,
            "leased": 0,
            "failed": 0,
            "failed_count_capped": False,
            "oldest_due_seconds": None,
        },
        spool={"ready": True, "component": "spool", "reason": "ready"},
    )
    worker["stages"]["player-#SECRET"] = worker["stages"]["python_claim"]
    result = build_operating_snapshot(
        database=_database(),
        collector=_collector(),
        python_api=_api(),
        python_workers=[worker],
        spool_config=_config(),
    )
    assert result["check"]["exit_code"] == 2
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("invalid_migration", (None, [], "malformed", 1))
def test_non_mapping_migration_fact_is_bounded_indeterminate(
    invalid_migration: object,
) -> None:
    database = _database()
    database["migrations"][0] = invalid_migration

    result = _snapshot(database=database)

    assert result["check"] == {
        "status": "indeterminate",
        "exit_code": 2,
        "reasons": ["required_fact_invalid"],
    }
    assert result["database"] is None


def test_pool_health_keeps_measures_and_zero_fills_only_counters() -> None:
    class Pool:
        @staticmethod
        def get_stats() -> dict[str, int]:
            return {
                "pool_min": 1,
                "pool_max": 4,
                "pool_size": 2,
                "pool_available": 1,
                "requests_waiting": 0,
            }

    assert database_pool_health(Pool()) == {
        "pool_min": 1,
        "pool_max": 4,
        "pool_size": 2,
        "pool_available": 1,
        "requests_waiting": 0,
        "requests_num": 0,
        "requests_queued": 0,
        "requests_wait_ms": 0,
        "usage_ms": 0,
    }


def test_zero_configured_storage_floors_are_valid() -> None:
    config = _config()
    config["free_space_floor"] = 0
    config["free_inode_floor"] = 0

    result = _snapshot(spool_config=config)

    assert result["check"]["exit_code"] == 0


def test_comparable_previous_snapshot_reports_exact_deltas_and_runway() -> None:
    previous_database = _database(captured_at="2026-08-28T19:00:00+00:00")
    previous = _snapshot(database=previous_database)
    database = _database()
    database["storage"]["relations"][0]["table_bytes"] = 130
    database["storage"]["relations"][0]["total_bytes"] = 150
    database["storage"]["wal"]["retained_bytes"] = 25
    collector = _collector()
    collector["spool"]["final_bytes"] = 20

    result = _snapshot(database=database, collector=collector, previous=previous)

    assert result["check"]["exit_code"] == 0
    assert result["comparison"]["previous_snapshot_id"] == previous["snapshot_id"]
    assert result["comparison"]["interval_seconds"] == 3600
    assert result["comparison"]["deltas"]["wal_retained_bytes"] == 15
    assert result["comparison"]["deltas"]["spool_logical_bytes"] == 10
    assert result["comparison"]["deltas"]["relations"][0]["table_bytes"] == 30
    assert result["comparison"]["runway"]["spool_days_to_hard_capacity"] is not None


def test_previous_snapshot_from_another_database_has_no_growth_deltas() -> None:
    previous_database = _database(captured_at="2026-08-28T19:00:00+00:00")
    previous = _snapshot(database=previous_database)
    current_database = _database()
    current_database["identity"]["database_oid"] = 16385

    result = _snapshot(database=current_database, previous=previous)

    assert result["check"]["exit_code"] == 0
    assert result["comparison"]["reason"] == "database_identity_mismatch"
    assert result["comparison"]["deltas"] is None
    assert result["comparison"]["runway"] is None


def test_tampered_explicit_previous_snapshot_is_indeterminate() -> None:
    previous = _snapshot(database=_database(captured_at="2026-08-28T19:00:00+00:00"))
    previous = deepcopy(previous)
    previous["database"]["storage"]["wal"]["retained_bytes"] += 1

    result = _snapshot(previous=previous)

    assert result["check"]["status"] == "indeterminate"
    assert result["check"]["exit_code"] == 2
    assert "invalid_previous_snapshot" in result["check"]["reasons"]


@pytest.mark.parametrize("offset_seconds", [-121, 1])
def test_previous_snapshot_rejects_stale_or_future_worker(
    offset_seconds: int,
) -> None:
    previous = _snapshot(database=_database(captured_at="2026-08-28T19:00:00+00:00"))
    worker = previous["processes"]["python_workers"][0]
    database_time = datetime.fromisoformat(previous["database"]["captured_at"])
    worker["captured_at"] = (
        database_time + timedelta(seconds=offset_seconds)
    ).isoformat()
    unsigned = {key: value for key, value in previous.items() if key != "snapshot_id"}
    previous["snapshot_id"] = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    result = _snapshot(previous=previous)

    assert result["check"]["exit_code"] == 2
    assert "invalid_previous_snapshot" in result["check"]["reasons"]
