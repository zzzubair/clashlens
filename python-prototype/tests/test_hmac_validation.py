from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path

import pytest

from clashlens_prototype.hmac_proof import (
    InvalidProof,
    SigningInput,
    load_secret_file,
    sign,
    verify_proof,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "testdata" / "private-api-hmac-v1.json"
)


def anonymous_vector() -> dict[str, object]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"][0]


def signing_input(vector: dict[str, object]) -> SigningInput:
    return SigningInput(
        proof_version=str(vector["proof_version"]),
        caller_b64url=str(vector["caller_b64url"]),
        key_id_b64url=str(vector["key_id_b64url"]),
        audience=str(vector["audience"]),
        method=str(vector["method"]),
        target_b64url=str(vector["target_b64url"]),
        body_sha256=str(vector["body_sha256"]),
        issued_at=str(vector["issued_at"]),
        expires_at=str(vector["expires_at"]),
        request_id=str(vector["request_id"]),
        provider_b64url=str(vector["provider_b64url"]),
        provider_subject_b64url=str(vector["provider_subject_b64url"]),
    )


def raw_headers(value: SigningInput, signature: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"x-clashlens-proof-version", value.proof_version.encode("ascii")),
        (b"x-clashlens-caller", value.caller_b64url.encode("ascii")),
        (b"x-clashlens-key-id", value.key_id_b64url.encode("ascii")),
        (b"x-clashlens-issued-at", value.issued_at.encode("ascii")),
        (b"x-clashlens-expires-at", value.expires_at.encode("ascii")),
        (b"x-clashlens-request-id", value.request_id.encode("ascii")),
        (b"x-clashlens-provider", value.provider_b64url.encode("ascii")),
        (
            b"x-clashlens-provider-subject",
            value.provider_subject_b64url.encode("ascii"),
        ),
        (b"x-clashlens-signature", signature.encode("ascii")),
    ]


def verify(vector: dict[str, object], value: SigningInput) -> None:
    key = bytes.fromhex(str(vector["key_hex"]))
    verify_proof(
        headers=raw_headers(value, sign(key, value)),
        method=str(vector["method"]),
        raw_target=str(vector["target"]).encode("ascii"),
        body=bytes.fromhex(str(vector["body_hex"])),
        keys={(str(vector["caller"]), str(vector["key_id"])): key},
        now=int(str(vector["verification_time"])),
    )


def test_verifier_rejects_proof_lifetime_over_30_seconds() -> None:
    vector = anonymous_vector()
    value = replace(signing_input(vector), expires_at="1800000031")

    with pytest.raises(InvalidProof):
        verify(vector, value)


def test_verifier_rejects_provider_without_provider_subject() -> None:
    vector = anonymous_vector()
    value = replace(signing_input(vector), provider_b64url="ZGlzY29yZA")

    with pytest.raises(InvalidProof):
        verify(vector, value)


@pytest.mark.parametrize("suffix", [b"", b"\n"])
def test_secret_file_accepts_one_unpadded_base64url_key(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    key = bytes(range(32))
    encoded = base64.urlsafe_b64encode(key).rstrip(b"=")
    secret_file = tmp_path / "caller.key"
    secret_file.write_bytes(encoded + suffix)

    assert load_secret_file(secret_file) == key


@pytest.mark.parametrize(
    "content",
    [
        base64.urlsafe_b64encode(bytes(range(32))),
        base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=") + b"\r\n",
        b" " + base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"="),
        base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=") + b" ",
        base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=") + b"\n\n",
        base64.urlsafe_b64encode(bytes(range(31))).rstrip(b"="),
        b"\xff",
    ],
)
def test_secret_file_rejects_noncanonical_or_wrong_length_key(
    tmp_path: Path,
    content: bytes,
) -> None:
    secret_file = tmp_path / "caller.key"
    secret_file.write_bytes(content)

    with pytest.raises(ValueError, match="secret file"):
        load_secret_file(secret_file)
