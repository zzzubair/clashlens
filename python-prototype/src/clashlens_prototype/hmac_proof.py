from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

PROOF_VERSION = "clashlens-hmac-v1"
AUDIENCE = "clashlens-python-private-api"
ALLOWED_PROVIDERS = frozenset({"discord", "google"})
_METHOD_RE = re.compile(r"^[A-Z]+$")

PROOF_HEADERS = {
    "proof_version": b"x-clashlens-proof-version",
    "caller": b"x-clashlens-caller",
    "key_id": b"x-clashlens-key-id",
    "issued_at": b"x-clashlens-issued-at",
    "expires_at": b"x-clashlens-expires-at",
    "request_id": b"x-clashlens-request-id",
    "provider": b"x-clashlens-provider",
    "provider_subject": b"x-clashlens-provider-subject",
    "signature": b"x-clashlens-signature",
}


@dataclass(frozen=True, slots=True)
class SigningInput:
    proof_version: str
    caller_b64url: str
    key_id_b64url: str
    audience: str
    method: str
    target_b64url: str
    body_sha256: str
    issued_at: str
    expires_at: str
    request_id: str
    provider_b64url: str
    provider_subject_b64url: str


@dataclass(frozen=True, slots=True)
class VerifiedProof:
    caller: str
    key_id: str
    provider: str
    provider_subject: str
    request_id: str


class InvalidProof(ValueError):
    pass


def load_secret_file(path: str | Path) -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError("HMAC secret file could not be read") from error
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        encoded = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("HMAC secret file must contain ASCII base64url") from error
    if not encoded or not _is_canonical_base64url(encoded):
        raise ValueError("HMAC secret file must contain one unpadded base64url value")
    decoded = base64.b64decode(
        encoded + "=" * (-len(encoded) % 4),
        altchars=b"-_",
        validate=True,
    )
    if len(decoded) != 32:
        raise ValueError("HMAC secret file value must decode to exactly 32 bytes")
    return decoded


def build_signing_bytes(value: SigningInput) -> bytes:
    return "\n".join(
        (
            value.proof_version,
            f"caller:{value.caller_b64url}",
            f"key-id:{value.key_id_b64url}",
            f"audience:{value.audience}",
            f"method:{value.method}",
            f"target:{value.target_b64url}",
            f"body-sha256:{value.body_sha256}",
            f"issued-at:{value.issued_at}",
            f"expires-at:{value.expires_at}",
            f"request-id:{value.request_id}",
            f"provider:{value.provider_b64url}",
            f"provider-subject:{value.provider_subject_b64url}",
        )
    ).encode("ascii")


def sign(key: bytes, value: SigningInput) -> str:
    digest = hmac.new(key, build_signing_bytes(value), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_proof(
    *,
    headers: Sequence[tuple[bytes, bytes]],
    method: str,
    raw_target: bytes,
    body: bytes,
    keys: Mapping[tuple[str, str], bytes],
    now: int,
) -> VerifiedProof:
    values: dict[str, str] = {}
    for field, header_name in PROOF_HEADERS.items():
        matching = [value for name, value in headers if name.lower() == header_name]
        if len(matching) != 1:
            raise InvalidProof(
                f"expected exactly one {header_name.decode('ascii')} header"
            )
        try:
            values[field] = matching[0].decode("ascii")
        except UnicodeDecodeError as error:
            raise InvalidProof("proof headers must contain ASCII") from error

    if values["proof_version"] != PROOF_VERSION:
        raise InvalidProof("unknown proof version")

    if not _METHOD_RE.fullmatch(method):
        raise InvalidProof("method must contain uppercase ASCII letters only")
    try:
        raw_target.decode("ascii")
    except UnicodeDecodeError as error:
        raise InvalidProof("request target must be ASCII") from error

    issued_at = _parse_canonical_unix_time(values["issued_at"])
    expires_at = _parse_canonical_unix_time(values["expires_at"])
    if not 1 <= expires_at - issued_at <= 30:
        raise InvalidProof("proof lifetime is outside the allowed range")
    if not issued_at - 5 <= now <= expires_at + 5:
        raise InvalidProof("proof is outside its allowed time window")

    caller = _decode_text(values["caller"])
    key_id = _decode_text(values["key_id"])
    provider = _decode_text(values["provider"])
    provider_subject = _decode_text(values["provider_subject"])
    if not caller or not key_id:
        raise InvalidProof("caller and key ID must not be empty")
    if bool(provider) != bool(provider_subject):
        raise InvalidProof(
            "provider and provider subject must both be empty or non-empty"
        )
    if provider and provider not in ALLOWED_PROVIDERS:
        raise InvalidProof("provider is not allowed")
    try:
        if str(UUID(values["request_id"])) != values["request_id"]:
            raise InvalidProof("request ID must be a canonical lowercase UUID")
    except ValueError as error:
        raise InvalidProof("request ID must be a canonical lowercase UUID") from error
    if not _is_canonical_base64url(values["signature"]):
        raise InvalidProof("signature must be canonical unpadded base64url")
    signing_input = SigningInput(
        proof_version=values["proof_version"],
        caller_b64url=values["caller"],
        key_id_b64url=values["key_id"],
        audience=AUDIENCE,
        method=method,
        target_b64url=_encode_base64url(raw_target),
        body_sha256=hashlib.sha256(body).hexdigest(),
        issued_at=values["issued_at"],
        expires_at=values["expires_at"],
        request_id=values["request_id"],
        provider_b64url=values["provider"],
        provider_subject_b64url=values["provider_subject"],
    )
    key = keys.get((caller, key_id))
    if key is None:
        raise InvalidProof("unknown caller or key ID")
    if not hmac.compare_digest(sign(key, signing_input), values["signature"]):
        raise InvalidProof("invalid signature")
    return VerifiedProof(
        caller=caller,
        key_id=key_id,
        provider=provider,
        provider_subject=provider_subject,
        request_id=values["request_id"],
    )


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_text(value: str) -> str:
    try:
        if not _is_canonical_base64url(value):
            raise ValueError("noncanonical base64url")
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
        text = decoded.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise InvalidProof("invalid base64url text field") from error
    if _encode_base64url(decoded) != value:
        raise InvalidProof("noncanonical base64url text field")
    return text


def _is_canonical_base64url(value: str) -> bool:
    if any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        return False
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (binascii.Error, ValueError):
        return False
    return _encode_base64url(decoded) == value


def _parse_canonical_unix_time(value: str) -> int:
    if not value or (value != "0" and (value.startswith("0") or not value.isdecimal())):
        raise InvalidProof("Unix time must be canonical non-negative decimal")
    if value == "0":
        return 0
    return int(value)
