from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .db import PROTOTYPE_CONTRACT_VERSION, Database
from .hmac_proof import InvalidProof, VerifiedProof, verify_proof
from .profile import normalize_player_tag

DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
PLAYER_READ_OPERATION = "player.read"
CALLER_OPERATIONS = {
    "typescript-website": frozenset({PLAYER_READ_OPERATION}),
    "discord-bot": frozenset({PLAYER_READ_OPERATION}),
}
HEALTH_PATHS = frozenset({"/livez", "/readyz"})


class PlayerPageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str
    name: str
    trophies: int
    eligibility: str
    active: bool
    freshness: str
    coverage: str
    observed_at: datetime
    source_http_status: int
    endpoint_version: str
    schema_version: str
    parser_version: str


class RequestBodyTooLarge(ValueError):
    pass


def create_app(
    database_url: str | None = None,
    *,
    keys: Mapping[tuple[str, str], bytes],
    database: Database | None = None,
    clock: Callable[[], int | float] = time.time,
    freshness_seconds: int = 900,
    max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
) -> FastAPI:
    if database is None and database_url is None:
        raise ValueError("database_url or database is required")
    if not keys:
        raise ValueError("at least one private API HMAC key is required")
    if freshness_seconds < 0:
        raise ValueError("freshness window must not be negative")
    if max_body_bytes <= 0:
        raise ValueError("private API request body limit must be positive")
    if max_body_bytes > MAX_REQUEST_BODY_BYTES:
        raise ValueError("private API request body limit exceeds the supported maximum")
    caller_counts: dict[str, int] = {}
    for (caller, key_id), key in keys.items():
        if not caller or not key_id:
            raise ValueError("HMAC caller and key ID must not be empty")
        if len(key) != 32:
            raise ValueError("each private API HMAC key must contain exactly 32 bytes")
        caller_counts[caller] = caller_counts.get(caller, 0) + 1
        if caller_counts[caller] > 2:
            raise ValueError("a caller may have only current and previous HMAC keys")
    store = database or Database(database_url or "")
    owns_store = database is None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _app.state.shutting_down = False
        try:
            yield
        finally:
            _app.state.shutting_down = True
            if owns_store:
                store.close()

    app = FastAPI(title="Clash Lens Python prototype", lifespan=lifespan)
    app.state.database = store
    app.state.max_body_bytes = max_body_bytes
    app.state.shutting_down = False

    @app.exception_handler(Exception)
    async def safe_internal_error(_request: Request, _error: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    @app.middleware("http")
    async def verify_private_proof(request: Request, call_next):
        try:
            body = await _read_limited_body(request, max_body_bytes)
        except RequestBodyTooLarge:
            return JSONResponse(
                status_code=413, content={"error": "request_body_too_large"}
            )
        if request.scope.get("path") in HEALTH_PATHS:
            return await call_next(request)
        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            raw_path = str(request.scope.get("path", "")).encode(
                "ascii", errors="replace"
            )
        raw_query = request.scope.get("query_string", b"")
        if not isinstance(raw_query, bytes):
            raw_query = bytes(raw_query)
        raw_target = raw_path + (b"?" + raw_query if raw_query else b"")
        try:
            proof = verify_proof(
                headers=request.scope.get("headers", []),
                method=str(request.scope.get("method", "")),
                raw_target=raw_target,
                body=body,
                keys=keys,
                now=int(clock()),
            )
        except InvalidProof:
            return JSONResponse(status_code=401, content={"error": "invalid_proof"})
        request.state.verified_proof = proof
        return await call_next(request)

    @app.get("/livez")
    async def liveness() -> dict[str, bool]:
        return {"live": True}

    @app.get("/readyz")
    async def readiness() -> JSONResponse:
        if app.state.shutting_down:
            return JSONResponse(status_code=503, content={"ready": False})
        try:
            ready = bool(
                store.is_ready(expected_contract_version=PROTOTYPE_CONTRACT_VERSION)
            )
        except Exception:  # noqa: BLE001 - health output must not expose backend details
            ready = False
        return JSONResponse(status_code=200 if ready else 503, content={"ready": ready})

    @app.get("/v1/players/{player_tag}", response_model=PlayerPageResponse)
    async def get_player(
        request: Request, player_tag: str
    ) -> PlayerPageResponse | JSONResponse:
        proof: VerifiedProof = request.state.verified_proof
        if not _is_authorized(proof, PLAYER_READ_OPERATION):
            return JSONResponse(
                status_code=403,
                content={"error": "caller_operation_not_authorized"},
            )
        try:
            normalized_tag = normalize_player_tag(player_tag)
        except ValueError:
            return JSONResponse(status_code=404, content={"error": "player_not_found"})
        row = store.get_player(normalized_tag)
        if row is None:
            return JSONResponse(status_code=404, content={"error": "player_not_found"})
        now = datetime.fromtimestamp(int(clock()), tz=UTC)
        observed_at = row["observed_at"].astimezone(UTC)
        age = max(0.0, (now - observed_at).total_seconds())
        freshness = "fresh" if age <= freshness_seconds else "stale"
        return PlayerPageResponse(
            tag=row["normalized_tag"],
            name=row["name"],
            trophies=row["trophies"],
            eligibility=row["eligibility_state"],
            active=bool(row["active"]),
            freshness=freshness,
            coverage="profile",
            observed_at=observed_at,
            source_http_status=row["source_http_status"],
            endpoint_version=row["endpoint_version"],
            schema_version=row["schema_version"],
            parser_version=row["parser_version"],
        )

    return app


async def _read_limited_body(request: Request, limit: int) -> bytes:
    content_lengths = [
        value
        for name, value in request.scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if content_lengths:
        try:
            declared_lengths = [int(value) for value in content_lengths]
        except (TypeError, ValueError) as error:
            raise RequestBodyTooLarge from error
        if len(set(declared_lengths)) != 1:
            raise RequestBodyTooLarge
        declared = declared_lengths[0]
        if declared < 0 or declared > limit:
            raise RequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestBodyTooLarge
        body.extend(chunk)
    request._body = bytes(body)  # type: ignore[attr-defined]
    return bytes(body)


def _is_authorized(proof: VerifiedProof, operation: str) -> bool:
    allowed = CALLER_OPERATIONS.get(proof.caller, frozenset())
    if operation not in allowed:
        return False
    # The profile prototype has no account resolver. Do not treat provider headers
    # as authorization until a Python-owned account mapping exists.
    return not proof.provider and not proof.provider_subject
