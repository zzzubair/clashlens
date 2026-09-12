from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from clashlens.operating import (
    WorkerMetrics,
    api_route,
    database_pool_health,
    write_private_snapshot,
)


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
