from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlsplit


class VerificationOutcome(StrEnum):
    VERIFIED = "verified"
    INVALID_TOKEN = "invalid_token"
    UNAVAILABLE = "verification_unavailable"


class KeyAction(StrEnum):
    NONE = "none"
    COOLDOWN = "cooldown"
    QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class VerificationClassification:
    outcome: VerificationOutcome
    key_action: KeyAction


@dataclass(frozen=True, slots=True)
class OfficialVerificationResponse:
    http_status: int
    body: bytes


@dataclass(frozen=True, slots=True)
class OfficialVerificationRequest:
    method: str
    url: str
    proxy_url: str
    authorization: str
    body: bytes
    timeout_seconds: float


class VerificationTransportError(RuntimeError):
    pass


class OfficialVerificationClient:
    def __init__(
        self,
        *,
        api_key: bytes,
        proxy_url: str,
        timeout_seconds: float = 10.0,
        transport: Callable[[OfficialVerificationRequest], OfficialVerificationResponse]
        | None = None,
    ) -> None:
        parsed_proxy = urlsplit(proxy_url)
        if (
            parsed_proxy.scheme not in {"http", "https"}
            or not parsed_proxy.hostname
            or parsed_proxy.username is not None
            or parsed_proxy.password is not None
        ):
            raise ValueError("a non-credentialed fixed-egress proxy URL is required")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("official verification timeout is outside the supported range")
        try:
            authorization = "Bearer " + api_key.decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("official API key must contain ASCII") from error
        self._proxy_url = proxy_url
        self._authorization = authorization
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _urllib_transport

    def verify(self, normalized_tag: str, player_token: str) -> OfficialVerificationResponse:
        request = OfficialVerificationRequest(
            method="POST",
            url=(
                "https://api.clashofclans.com/v1/players/"
                f"{quote(normalized_tag, safe='')}/verifytoken"
            ),
            proxy_url=self._proxy_url,
            authorization=self._authorization,
            body=json.dumps(
                {"token": player_token}, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8"),
            timeout_seconds=self._timeout_seconds,
        )
        return self._transport(request)


_VALID_BODY: Final = {"status": "ok"}
_INVALID_BODY: Final = {"status": "invalid"}
_AUTH_FAILURE_BODIES: Final = (
    {"reason": "accessDenied", "message": "Invalid authorization"},
    {"reason": "accessDenied.invalidIp", "message": "Invalid IP address"},
)


def classify_official_response(
    http_status: int,
    body: bytes,
) -> VerificationClassification:
    try:
        decoded = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = None

    if http_status == 200 and decoded == _VALID_BODY:
        return VerificationClassification(VerificationOutcome.VERIFIED, KeyAction.NONE)
    if http_status == 200 and decoded == _INVALID_BODY:
        return VerificationClassification(VerificationOutcome.INVALID_TOKEN, KeyAction.NONE)
    if http_status in (401, 403) and decoded in _AUTH_FAILURE_BODIES:
        return VerificationClassification(VerificationOutcome.UNAVAILABLE, KeyAction.QUARANTINE)
    if http_status == 429:
        return VerificationClassification(VerificationOutcome.UNAVAILABLE, KeyAction.COOLDOWN)
    return VerificationClassification(VerificationOutcome.UNAVAILABLE, KeyAction.NONE)


def classify_transport_ambiguity() -> VerificationClassification:
    return VerificationClassification(VerificationOutcome.UNAVAILABLE, KeyAction.NONE)


def load_official_api_key_file(path: str | Path) -> bytes:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError("official API key file could not be read") from error
    if raw.endswith(b"\r\n"):
        raw = raw[:-2]
    elif raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        decoded = raw.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("official API key file must contain ASCII") from error
    if (
        not decoded
        or len(decoded) > 4096
        or decoded != decoded.strip()
        or any(character.isspace() for character in decoded)
    ):
        raise ValueError("official API key file contains invalid bytes")
    return raw


def _urllib_transport(request: OfficialVerificationRequest) -> OfficialVerificationResponse:
    proxy = urllib.request.ProxyHandler(
        {"http": request.proxy_url, "https": request.proxy_url}
    )
    opener = urllib.request.build_opener(proxy)
    http_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers={
            "Authorization": request.authorization,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=request.method,
    )
    try:
        with opener.open(http_request, timeout=request.timeout_seconds) as response:
            body = response.read(8193)
            if len(body) > 8192:
                raise VerificationTransportError("official verification response is too large")
            return OfficialVerificationResponse(int(response.status), body)
    except urllib.error.HTTPError as error:
        body = error.read(8193)
        if len(body) > 8192:
            raise VerificationTransportError("official verification response is too large") from None
        return OfficialVerificationResponse(int(error.code), body)
    except (OSError, urllib.error.URLError) as error:
        raise VerificationTransportError("official verification transport is unavailable") from error
