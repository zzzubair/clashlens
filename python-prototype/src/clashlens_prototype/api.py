from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .db import Database
from .hmac_proof import InvalidProof, verify_proof
from .profile import normalize_player_tag

PLAYER_PAGE_CALLERS = frozenset({"typescript-website", "discord-bot"})


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


def create_app(
    database_url: str | None = None,
    *,
    keys: Mapping[tuple[str, str], bytes],
    database: Database | None = None,
    clock: Callable[[], int | float] = time.time,
    freshness_seconds: int = 900,
) -> FastAPI:
    if database is None and database_url is None:
        raise ValueError("database_url or database is required")
    if not keys:
        raise ValueError("at least one private API HMAC key is required")
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
        yield
        if owns_store:
            store.close()

    app = FastAPI(title="Clash Lens Python prototype", lifespan=lifespan)
    app.state.database = store

    @app.middleware("http")
    async def verify_private_proof(request: Request, call_next):
        body = await request.body()
        raw_path = request.scope.get("raw_path")
        if not isinstance(raw_path, bytes):
            raw_path = str(request.scope.get("path", "")).encode("ascii")
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
        except InvalidProof as error:
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_proof", "detail": str(error)},
            )
        request.state.verified_proof = proof
        return await call_next(request)

    @app.get("/v1/players/{player_tag}", response_model=PlayerPageResponse)
    async def get_player(request: Request, player_tag: str) -> PlayerPageResponse | JSONResponse:
        if request.state.verified_proof.caller not in PLAYER_PAGE_CALLERS:
            return JSONResponse(
                status_code=403,
                content={"error": "caller_not_authorized"},
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
