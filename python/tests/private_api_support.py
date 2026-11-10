"""Shared setup for private api tests."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from clashlens.hmac_proof import SigningInput, sign

TS_CURRENT = bytes.fromhex("21" * 32)


TS_PREVIOUS = bytes.fromhex("22" * 32)


DISCORD_CURRENT = bytes.fromhex("31" * 32)


NOW_SECONDS = 1_807_000_000


NOW = datetime.fromtimestamp(NOW_SECONDS, tz=UTC)


def b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def signed_headers(
    target: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    caller: str = "typescript-website",
    key_id: str = "current",
    provider: str = "",
    subject: str = "",
    request_id: str | None = None,
) -> dict[str, str]:
    keys = {
        ("typescript-website", "current"): TS_CURRENT,
        ("typescript-website", "previous"): TS_PREVIOUS,
        ("discord-bot", "current"): DISCORD_CURRENT,
    }
    signing_input = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=b64(caller),
        key_id_b64url=b64(key_id),
        audience="clashlens-python-private-api",
        method=method,
        target_b64url=b64(target),
        body_sha256=hashlib.sha256(body).hexdigest(),
        issued_at=str(NOW_SECONDS),
        expires_at=str(NOW_SECONDS + 10),
        request_id=request_id or str(uuid4()),
        provider_b64url=b64(provider),
        provider_subject_b64url=b64(subject),
    )
    return {
        "X-ClashLens-Proof-Version": signing_input.proof_version,
        "X-ClashLens-Caller": signing_input.caller_b64url,
        "X-ClashLens-Key-Id": signing_input.key_id_b64url,
        "X-ClashLens-Issued-At": signing_input.issued_at,
        "X-ClashLens-Expires-At": signing_input.expires_at,
        "X-ClashLens-Request-Id": signing_input.request_id,
        "X-ClashLens-Provider": signing_input.provider_b64url,
        "X-ClashLens-Provider-Subject": signing_input.provider_subject_b64url,
        "X-ClashLens-Signature": sign(keys[(caller, key_id)], signing_input),
        "Content-Type": "application/json",
    }


def json_body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()
