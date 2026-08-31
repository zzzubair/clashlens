from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clashlens.api import create_app
from clashlens.hmac_proof import SigningInput, sign
from clashlens.operating import ApiMetrics

KEY = bytes.fromhex("11" * 32)
NOW = 1_800_000_000


@dataclass
class FakeDatabase:
    ready: bool = True
    closed: bool = False

    def is_ready(self, expected_contract_version: int) -> bool:
        return self.ready and expected_contract_version == 2

    def close(self) -> None:
        self.closed = True

    def pool_health(self) -> dict[str, int]:
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

    def get_player(self, _tag: str) -> dict[str, Any] | None:
        return None


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).rstrip(b"=").decode("ascii")


def _signed_headers(
    target: str,
    *,
    provider: str = "",
    provider_subject: str = "",
    caller: str = "typescript-website",
) -> dict[str, str]:
    value = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=_b64(caller),
        key_id_b64url=_b64("current"),
        audience="clashlens-python-private-api",
        method="GET",
        target_b64url=_b64(target),
        body_sha256=hashlib.sha256(b"").hexdigest(),
        issued_at=str(NOW),
        expires_at=str(NOW + 10),
        request_id="00000000-0000-4000-8000-000000000029",
        provider_b64url=_b64(provider),
        provider_subject_b64url=_b64(provider_subject),
    )
    return {
        "X-ClashLens-Proof-Version": value.proof_version,
        "X-ClashLens-Caller": value.caller_b64url,
        "X-ClashLens-Key-Id": value.key_id_b64url,
        "X-ClashLens-Issued-At": value.issued_at,
        "X-ClashLens-Expires-At": value.expires_at,
        "X-ClashLens-Request-Id": value.request_id,
        "X-ClashLens-Provider": value.provider_b64url,
        "X-ClashLens-Provider-Subject": value.provider_subject_b64url,
        "X-ClashLens-Signature": sign(KEY, value),
    }


def _app(
    database: FakeDatabase,
    *,
    max_body_bytes: int = 1_024,
    max_response_bytes: int = 1024 * 1024,
    api_metrics: ApiMetrics | None = None,
):
    return create_app(
        database=database,
        keys={("typescript-website", "current"): KEY},
        clock=lambda: NOW,
        max_body_bytes=max_body_bytes,
        max_response_bytes=max_response_bytes,
        api_metrics=api_metrics,
    )


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


def test_operator_metrics_are_private_bounded_and_preserve_response_paths() -> None:
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
