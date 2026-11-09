from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from api_security_support import (
    KEY,
    NOW,
    FakeDatabase,
    _app,
    _signed_headers,
)
from fastapi.testclient import TestClient

from clashlens.api import create_app
from clashlens.operating import ApiMetrics


def test_liveness_and_readiness_are_local_health_operations_without_private_proof() -> (
    None
):
    database = FakeDatabase()

    with TestClient(_app(database)) as client:
        live = client.get("/livez")
        ready = client.get("/readyz")

    assert live.status_code == 200
    assert live.json() == {"live": True}
    assert ready.status_code == 200
    assert ready.json() == {"ready": True}


def test_readiness_returns_service_unavailable_when_database_contract_is_not_ready() -> (
    None
):
    database = FakeDatabase(ready=False)

    with TestClient(_app(database)) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"ready": False}


def test_private_api_rejects_a_body_over_the_explicit_limit_before_proof_verification() -> (
    None
):
    database = FakeDatabase()

    with TestClient(_app(database, max_body_bytes=8)) as client:
        response = client.post("/v1/players/%232PP", content=b"012345678")

    assert response.status_code == 413
    assert response.json() == {"error": "request_body_too_large"}


def test_health_operations_also_reject_a_body_over_the_explicit_limit() -> None:
    database = FakeDatabase()

    with TestClient(_app(database, max_body_bytes=8)) as client:
        response = client.request("GET", "/readyz", content=b"012345678")

    assert response.status_code == 413
    assert response.json() == {"error": "request_body_too_large"}


def test_api_rejects_an_unbounded_request_body_setting() -> None:
    with pytest.raises(ValueError, match="supported maximum"):
        _app(FakeDatabase(), max_body_bytes=16 * 1024 * 1024 + 1)


def test_invalid_private_proof_has_a_safe_error_envelope() -> None:
    database = FakeDatabase()

    with TestClient(_app(database)) as client:
        response = client.get("/v1/players/%232PP")

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_proof"}


def test_operator_metrics_are_private_bounded_and_preserve_response_paths(monkeypatch) -> None:
    monkeypatch.setattr(
        "clashlens.api_leaderboard.live_freshness_metrics",
        lambda database, now: {"entries": 0, "older_than_10_minutes": 0},
    )
    database = FakeDatabase(ready=False)
    metrics = ApiMetrics(
        process_id="00000000-0000-4000-8000-000000000080",
        started_at=datetime(2026, 8, 28, 20, 0, tzinfo=UTC),
    )

    with TestClient(_app(database, api_metrics=metrics)) as client:
        live = client.get("/livez")
        missing_proof = client.get("/v1/players/%23SECRET")
        unavailable = client.get("/readyz")
        unsigned_operator = client.get("/operatorz")
        operator = client.get(
            "/operatorz", headers=_signed_headers("/operatorz")
        )

    assert live.json() == {"live": True}
    assert missing_proof.json() == {"error": "invalid_proof"}
    assert unavailable.json() == {"ready": False}
    assert unsigned_operator.status_code == 401
    assert unsigned_operator.json() == {"error": "invalid_proof"}
    assert operator.status_code == 200
    snapshot = operator.json()
    assert snapshot["process"] == {
        "id": "00000000-0000-4000-8000-000000000080",
        "started_at": "2026-08-28T20:00:00+00:00",
    }
    assert snapshot["database_pool"]["pool_max"] == 4
    assert snapshot["requests"]["livez"]["outcomes"]["success"] == 1
    assert snapshot["requests"]["player_read"]["outcomes"]["expected_4xx"] == 1
    assert snapshot["requests"]["readyz"]["outcomes"]["safe_5xx"] == 1
    assert snapshot["requests"]["livez"]["latency"]["count"] == 1
    assert snapshot["requests"]["livez"]["response_bytes"]["sum"] > 0
    assert "SECRET" not in json.dumps(snapshot)


def test_operator_metrics_reject_end_user_identity() -> None:
    with TestClient(_app(FakeDatabase())) as client:
        response = client.get(
            "/operatorz",
            headers=_signed_headers(
                "/operatorz", provider="discord", provider_subject="123"
            ),
        )

    assert response.status_code == 403
    assert response.json() == {"error": "caller_operation_not_authorized"}


def test_response_size_limit_has_its_own_bounded_outcome() -> None:
    metrics = ApiMetrics()

    with TestClient(
        _app(FakeDatabase(), max_response_bytes=5, api_metrics=metrics)
    ) as client:
        response = client.get("/livez")

    assert response.status_code == 503
    assert response.json() == {"error": "response_too_large"}
    snapshot = metrics.snapshot({})
    assert snapshot["requests"]["livez"]["outcomes"] == {
        "success": 0,
        "expected_4xx": 0,
        "safe_5xx": 0,
        "response_size_limit": 1,
    }
    assert snapshot["requests"]["livez"]["response_bytes"]["max"] > 5


def test_public_player_read_denies_a_signed_end_user_identity() -> (
    None
):
    """Only the two allowed login providers pass proof verification; anything
    else is rejected before any authorization decision."""
    database = FakeDatabase()
    target = "/v1/players/%232PP"

    with TestClient(_app(database)) as client:
        response = client.get(
            target,
            headers=_signed_headers(target, provider="email", provider_subject="123"),
        )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_proof"}


def test_operator_freshness_cache_expires_and_never_hides_refresh_failure(monkeypatch):
    from time import perf_counter
    from types import SimpleNamespace

    instant = [100.0]
    calls = []
    failed = [False]
    monkeypatch.setattr("clashlens.api.time", SimpleNamespace(monotonic=lambda: instant[0], perf_counter=perf_counter))

    def measure(database, *, now):
        calls.append(now)
        if failed[0]:
            raise RuntimeError("database unavailable")
        return {"age_max_seconds": len(calls)}

    monkeypatch.setattr("clashlens.api_leaderboard.live_freshness_metrics", measure)
    with TestClient(_app(FakeDatabase()), raise_server_exceptions=False) as client:
        def scrape():
            return client.get("/operatorz", headers=_signed_headers("/operatorz"))

        assert scrape().json()["live_leaderboard"]["age_max_seconds"] == 1
        instant[0] = 129.99
        assert scrape().json()["live_leaderboard"]["age_max_seconds"] == 1
        instant[0] = 130
        failed[0] = True
        assert scrape().status_code >= 500
        failed[0] = False
        assert scrape().json()["live_leaderboard"]["age_max_seconds"] == 3
    assert len(calls) == 3


def test_player_read_passes_a_totals_only_request_to_the_page(monkeypatch) -> None:
    asked = []

    def page(database, tag, *, now, freshness_seconds, battles, goal_rank, dashboard):
        asked.append((tag, battles, goal_rank, dashboard))
        return {"tag": tag}

    monkeypatch.setattr("clashlens.api_players.read_player_page", page)
    with TestClient(_app(FakeDatabase())) as client:
        for target in ("/v1/players/%232PP", "/v1/players/%232PP?battles=false",
                       "/v1/players/%232PP?goal_rank=200", "/v1/players/%232PP?dashboard=true"):
            response = client.get(target, headers=_signed_headers(target))
            assert response.status_code == 200, response.text
        for rank in ("0", "10001", "unknown"):
            target = f"/v1/players/%232PP?goal_rank={rank}"
            assert client.get(target, headers=_signed_headers(target)).status_code == 422

    assert asked == [("#2PP", True, None, False), ("#2PP", False, None, False),
                     ("#2PP", True, 200, False), ("#2PP", True, None, True)]


def test_player_freshness_counts_from_arrival_not_from_a_free_connection(
    monkeypatch,
) -> None:
    instant = [NOW]
    asked = []
    database = FakeDatabase()

    @contextmanager
    def busy_pool():
        instant[0] += 2
        yield object()

    def page(connection, tag, *, now, freshness_seconds, battles, goal_rank, dashboard):
        asked.append(now)
        return {"tag": tag}

    database.pool.connection = busy_pool
    monkeypatch.setattr("clashlens.api_players.read_player_page", page)
    app = create_app(
        database=database,
        keys={("typescript-website", "current"): KEY},
        clock=lambda: instant[0],
    )
    target = "/v1/players/%232PP"
    with TestClient(app) as client:
        response = client.get(target, headers=_signed_headers(target))

    assert response.status_code == 200, response.text
    assert asked == [datetime.fromtimestamp(NOW, tz=UTC)]
