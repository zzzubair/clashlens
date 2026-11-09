"""Shared setup for api security tests."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

from clashlens.api import create_app
from clashlens.hmac_proof import SigningInput, sign
from clashlens.operating import ApiMetrics

KEY = bytes.fromhex("11" * 32)


NOW = 1_800_000_000


@dataclass
class FakeDatabase:
    pool: Any = field(default_factory=lambda: MagicMock(timeout=5.0, max_size=8))
    ready: bool = True
    closed: bool = False

    def is_ready(self, expected_contract_version: int) -> bool:
        return self.ready and expected_contract_version == 5

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
