from __future__ import annotations

import json
from pathlib import Path

import pytest

from clashlens_prototype.hmac_proof import (
    InvalidProof,
    SigningInput,
    build_signing_bytes,
    sign,
    verify_proof,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "testdata" / "private-api-hmac-v1.json"
)


def test_golden_vectors_match_signing_contract() -> None:
    vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]

    for vector in vectors:
        signing_input = SigningInput(
            proof_version=vector["proof_version"],
            caller_b64url=vector["caller_b64url"],
            key_id_b64url=vector["key_id_b64url"],
            audience=vector["audience"],
            method=vector["method"],
            target_b64url=vector["target_b64url"],
            body_sha256=vector["body_sha256"],
            issued_at=vector["issued_at"],
            expires_at=vector["expires_at"],
            request_id=vector["request_id"],
            provider_b64url=vector["provider_b64url"],
            provider_subject_b64url=vector["provider_subject_b64url"],
        )

        assert build_signing_bytes(signing_input).hex() == vector["signing_bytes_hex"]
        assert (
            sign(bytes.fromhex(vector["key_hex"]), signing_input)
            == vector["signature_b64url"]
        )


def test_verifier_accepts_golden_vectors() -> None:
    vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]

    for vector in vectors:
        headers = [
            (b"x-clashlens-proof-version", vector["proof_version"].encode("ascii")),
            (b"x-clashlens-caller", vector["caller_b64url"].encode("ascii")),
            (b"x-clashlens-key-id", vector["key_id_b64url"].encode("ascii")),
            (b"x-clashlens-issued-at", vector["issued_at"].encode("ascii")),
            (b"x-clashlens-expires-at", vector["expires_at"].encode("ascii")),
            (b"x-clashlens-request-id", vector["request_id"].encode("ascii")),
            (b"x-clashlens-provider", vector["provider_b64url"].encode("ascii")),
            (
                b"x-clashlens-provider-subject",
                vector["provider_subject_b64url"].encode("ascii"),
            ),
            (b"x-clashlens-signature", vector["signature_b64url"].encode("ascii")),
        ]

        verified = verify_proof(
            headers=headers,
            method=vector["method"],
            raw_target=vector["target"].encode("ascii"),
            body=bytes.fromhex(vector["body_hex"]),
            keys={
                (vector["caller"], vector["key_id"]): bytes.fromhex(vector["key_hex"])
            },
            now=vector["verification_time"],
        )

        assert verified.caller == vector["caller"]
        assert verified.key_id == vector["key_id"]
        assert verified.provider == vector["provider"]
        assert verified.provider_subject == vector["provider_subject"]
        assert verified.request_id == vector["request_id"]


def test_verifier_rejects_unknown_proof_version_with_a_valid_signature() -> None:
    vector = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"][0]
    changed = SigningInput(
        proof_version="clashlens-hmac-v2",
        caller_b64url=vector["caller_b64url"],
        key_id_b64url=vector["key_id_b64url"],
        audience=vector["audience"],
        method=vector["method"],
        target_b64url=vector["target_b64url"],
        body_sha256=vector["body_sha256"],
        issued_at=vector["issued_at"],
        expires_at=vector["expires_at"],
        request_id=vector["request_id"],
        provider_b64url=vector["provider_b64url"],
        provider_subject_b64url=vector["provider_subject_b64url"],
    )
    signature = sign(bytes.fromhex(vector["key_hex"]), changed)
    headers = [
        (b"x-clashlens-proof-version", b"clashlens-hmac-v2"),
        (b"x-clashlens-caller", vector["caller_b64url"].encode("ascii")),
        (b"x-clashlens-key-id", vector["key_id_b64url"].encode("ascii")),
        (b"x-clashlens-issued-at", vector["issued_at"].encode("ascii")),
        (b"x-clashlens-expires-at", vector["expires_at"].encode("ascii")),
        (b"x-clashlens-request-id", vector["request_id"].encode("ascii")),
        (b"x-clashlens-provider", b""),
        (b"x-clashlens-provider-subject", b""),
        (b"x-clashlens-signature", signature.encode("ascii")),
    ]

    with pytest.raises(InvalidProof):
        verify_proof(
            headers=headers,
            method=vector["method"],
            raw_target=vector["target"].encode("ascii"),
            body=b"",
            keys={
                (vector["caller"], vector["key_id"]): bytes.fromhex(vector["key_hex"])
            },
            now=vector["verification_time"],
        )
