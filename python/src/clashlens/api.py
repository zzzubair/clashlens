from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .accounts import normalize_display_name, normalize_group_name, normalize_username
from .api_db import (
    API_CONTRACT_VERSION,
    AccountContext,
    ApiDatabase,
    OperationResult,
    RequestBinding,
)
from .army_analytics import (
    ArmyAnalyticsSelection,
    ArmyAnalyticsUnavailable,
    CurrentSeasonEmpty,
)
from .hmac_proof import InvalidProof, VerifiedProof, verify_proof
from .operating import ApiMetrics, elapsed
from .profile import normalize_player_tag
from .verification import (
    OfficialVerificationResponse,
    VerificationOutcome,
    VerificationTransportError,
    classify_official_response,
    classify_transport_ambiguity,
)

_MAX_SUPPORTED_BODY_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_DEFAULT_FRESHNESS_SECONDS = 900

_PUBLIC_OPERATIONS = frozenset(
    {
        "analytics.read",
        "leaderboards.read",
        "player.read",
        "refresh.status",
        "refresh.submit",
        "user.read",
    }
)
_ALLOWED_PROVIDERS = frozenset({"google", "discord"})
_TYPESCRIPT_ACCOUNT_OPERATIONS = frozenset(
    {
        "account.create",
        "account.read",
        "account.update",
        "groups.read",
        "groups.write",
        "player_links.verify",
        "providers.link",
        "providers.unlink",
        "saved_tags.read",
        "saved_tags.write",
        "summary.read",
    }
)


class OfficialVerifier(Protocol):
    def verify(
        self, normalized_tag: str, player_token: str
    ) -> OfficialVerificationResponse: ...


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AccountCreateBody(StrictBody):
    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(min_length=1, max_length=80)


class AccountUpdateBody(AccountCreateBody):
    preferences: dict[str, Any] = Field(default_factory=dict)


class SavedTagBody(StrictBody):
    tag: str = Field(min_length=4, max_length=16)


class GroupBody(StrictBody):
    name: str = Field(min_length=1, max_length=80)
    tags: list[str] = Field(max_length=100)


class ProviderLinkBody(StrictBody):
    provider_subject: str = Field(min_length=1, max_length=255)


class ExportBody(StrictBody):
    format: Literal["google_sheets_scaffold", "csv_scaffold"]


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        *,
        detail: str | None = None,
        affected_days: list[int] | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.affected_days = affected_days


class RequestBodyTooLarge(ValueError):
    pass


def create_app(
    database: ApiDatabase,
    *,
    keys: Mapping[tuple[str, str], bytes],
    clock: Callable[[], int | float] = time.time,
    now: Callable[[], datetime] | None = None,
    max_body_bytes: int = 64 * 1024,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    verification_client: OfficialVerifier | None = None,
    official_credential_fingerprint: str | None = None,
    verification_cooldown_seconds: int = 5,
    api_metrics: ApiMetrics | None = None,
) -> FastAPI:
    if not 1 <= max_body_bytes <= _MAX_SUPPORTED_BODY_BYTES:
        raise ValueError("max_body_bytes exceeds the supported maximum")
    if not 1 <= max_response_bytes <= _MAX_SUPPORTED_BODY_BYTES:
        raise ValueError("max_response_bytes exceeds the supported maximum")
    if not 1 <= verification_cooldown_seconds <= 300:
        raise ValueError("verification cooldown is outside the supported range")
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

    production_database = database
    current_time = now or (lambda: datetime.fromtimestamp(int(clock()), tz=UTC))
    operating_metrics = api_metrics or ApiMetrics()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        database.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, error: ApiError) -> JSONResponse:
        content: dict[str, Any] = {"error": error.code}
        if error.detail:
            content["detail"] = error.detail
        if error.affected_days is not None:
            content["affected_days"] = error.affected_days
        return JSONResponse(status_code=error.status_code, content=content)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "invalid_request"})

    @app.exception_handler(Exception)
    async def safe_error_handler(_request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, ApiError):
            content: dict[str, Any] = {"error": error.code}
            if error.detail:
                content["detail"] = error.detail
            if error.affected_days is not None:
                content["affected_days"] = error.affected_days
            return JSONResponse(status_code=error.status_code, content=content)
        return JSONResponse(status_code=503, content={"error": "service_unavailable"})

    @app.middleware("http")
    async def proof_middleware(request: Request, call_next):
        request_started = time.perf_counter()

        def observed(
            response: Response,
            response_bytes: int,
            *,
            response_size_limited: bool = False,
        ) -> Response:
            operating_metrics.record(
                request.url.path,
                response.status_code,
                elapsed(request_started),
                response_bytes,
                response_size_limited=response_size_limited,
            )
            return response

        try:
            body = await _read_limited_body(request, max_body_bytes)
        except RequestBodyTooLarge:
            response = JSONResponse(
                status_code=413,
                content={"error": "request_body_too_large"},
            )
            return observed(response, len(response.body))
        if request.url.path not in {"/livez", "/readyz"}:
            try:
                proof = verify_proof(
                    headers=request.scope.get("headers", []),
                    method=request.method,
                    raw_target=_request_target(request).encode("ascii"),
                    body=body,
                    keys=keys,
                    now=int(clock()),
                )
            except InvalidProof:
                response = JSONResponse(
                    status_code=401, content={"error": "invalid_proof"}
                )
                return observed(response, len(response.body))
            request.state.proof = proof
        try:
            response = await call_next(request)
        except Exception:
            operating_metrics.record(
                request.url.path,
                503,
                elapsed(request_started),
                0,
            )
            raise
        chunks: list[bytes] = []
        response_size = 0
        async for chunk in response.body_iterator:
            encoded = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            response_size += len(encoded)
            if response_size > max_response_bytes:
                limited = JSONResponse(
                    status_code=503,
                    content={"error": "response_too_large"},
                )
                return observed(
                    limited,
                    response_size,
                    response_size_limited=True,
                )
            chunks.append(encoded)
        headers = dict(response.headers)
        headers.pop("content-length", None)
        bounded = Response(
            content=b"".join(chunks),
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,
        )
        return observed(bounded, response_size)

    @app.get("/livez")
    def live() -> dict[str, bool]:
        return {"live": True}

    @app.get("/readyz")
    def ready() -> JSONResponse:
        try:
            is_ready = database.is_ready(
                expected_contract_version=API_CONTRACT_VERSION
            )
        except Exception:  # noqa: BLE001 - readiness fails closed without disclosure.
            is_ready = False
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"ready": is_ready},
        )

    @app.get("/operatorz")
    def operator(request: Request) -> dict[str, Any]:
        proof: VerifiedProof = request.state.proof
        if (
            proof.caller != "typescript-website"
            or proof.provider
            or proof.provider_subject
        ):
            raise ApiError(403, "caller_operation_not_authorized")
        pool_health = getattr(database, "pool_health", dict)()
        return operating_metrics.snapshot(pool_health)

    @app.get("/v1/players/search")
    def search_players(
        request: Request,
        q: str = Query(min_length=1, max_length=80),
        limit: int = Query(default=50, ge=1, le=50),
    ) -> JSONResponse:
        _authorize(request, "player.read", production_database)
        return JSONResponse(
            status_code=200,
            content={
                "query": q,
                "known_only": True,
                "results": production_database.search_known_players(
                    q,
                    now=current_time(),
                    freshness_seconds=_DEFAULT_FRESHNESS_SECONDS,
                    limit=limit,
                ),
            },
        )

    @app.get("/v1/players/{tag}")
    def player(tag: str, request: Request) -> JSONResponse:
        _authorize(request, "player.read", production_database)
        normalized_tag = _safe_tag(tag)
        result = production_database.get_player_page(
            normalized_tag,
            now=current_time(),
            freshness_seconds=_DEFAULT_FRESHNESS_SECONDS,
        )
        if result is None:
            raise ApiError(404, "player_not_found")
        return JSONResponse(status_code=200, content=_json_safe(result))

    @app.post("/v1/players/{tag}/refresh")
    async def refresh(tag: str, request: Request) -> JSONResponse:
        context = _authorize(request, "refresh.submit", production_database)
        if await request.body():
            raise ApiError(422, "invalid_request")
        normalized_tag = _safe_tag(tag)
        binding = _binding(
            request,
            context,
            "refresh.submit",
            {"tag": normalized_tag},
        )
        result = production_database.submit_refresh(
            binding,
            normalized_tag=normalized_tag,
            cooldown_seconds=300,
        )
        return _operation_response(result)

    @app.get("/v1/refreshes/{refresh_id}")
    def refresh_status(refresh_id: str, request: Request) -> JSONResponse:
        _authorize(request, "refresh.status", production_database)
        _safe_uuid(refresh_id)
        result = production_database.get_refresh_status(refresh_id)
        if result is None:
            raise ApiError(404, "refresh_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/leaderboards/{kind}")
    def leaderboard(
        kind: Literal["live", "frozen"],
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        official_season_id: str | None = None,
        season_day_number: int | None = Query(default=None, ge=1, le=28),
    ) -> JSONResponse:
        _authorize(request, "leaderboards.read", production_database)
        if offset % limit or (official_season_id is None) != (season_day_number is None):
            raise ApiError(422, "invalid_request")
        if kind == "live":
            if official_season_id is not None:
                raise ApiError(422, "invalid_request")
            result = production_database.get_live_leaderboard(
                limit=limit, offset=offset, now=current_time(),
                freshness_seconds=_DEFAULT_FRESHNESS_SECONDS,
            )
        else:
            result = production_database.get_frozen_leaderboard(
                limit=limit, offset=offset, official_season_id=official_season_id,
                season_day_number=season_day_number, now=current_time(),
                freshness_seconds=_DEFAULT_FRESHNESS_SECONDS,
            )
        if result is None:
            raise ApiError(404, "leaderboard_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.get("/v1/analytics/armies")
    def army_analytics(
        request: Request,
        season: str = Query(min_length=1, max_length=80),
        lens: str = Query(default="offense", max_length=16),
        start_day: int = Query(default=1, ge=1, le=28),
        end_day: int = Query(default=28, ge=1, le=28),
        population: str = Query(default="top-100", max_length=40),
        category: str = Query(default="troops", max_length=40),
        sort: str = Query(default="usage-rate", max_length=40),
    ) -> JSONResponse:
        _authorize(request, "analytics.read", production_database)
        try:
            selection = ArmyAnalyticsSelection.parse(
                lens=lens, season=season, start_day=start_day, end_day=end_day,
                population=population, category=category, sort=sort,
            )
        except ValueError as error:
            raise ApiError(422, "invalid_army_analytics_selection") from error
        try:
            result = production_database.get_army_analytics(
                selection, now=current_time()
            )
        except CurrentSeasonEmpty as empty:
            content: dict[str, Any] = {"error": "no_completed_legend_days"}
            if empty.previous_season_id:
                content["previous_season_id"] = empty.previous_season_id
            return JSONResponse(status_code=404, content=content)
        except ArmyAnalyticsUnavailable as unavailable:
            raise ApiError(
                404,
                "army_analytics_unavailable",
                detail="unavailable legend days: "
                + ",".join(str(day) for day in unavailable.affected_days),
                affected_days=unavailable.affected_days,
            ) from unavailable
        if result is None:
            raise ApiError(404, "army_analytics_unavailable")
        return JSONResponse(status_code=200, content=_json_safe(result))

    @app.get("/v1/battles/{battle_id}/army")
    def battle_army(
        battle_id: int,
        request: Request,
        perspective: Literal["attacker", "defender"],
    ) -> JSONResponse:
        _authorize(request, "player.read", production_database)
        if battle_id < 1:
            raise ApiError(422, "invalid_request")
        result = production_database.get_battle_army(battle_id, perspective)
        if result is None:
            raise ApiError(404, "battle_army_not_found")
        return JSONResponse(status_code=200, content=_json_safe(result))

    @app.get("/v1/analytics/basic")
    def analytics(request: Request) -> JSONResponse:
        _authorize(request, "analytics.read", production_database)
        return JSONResponse(
            status_code=200,
            content=production_database.get_basic_analytics(
                now=current_time(),
                freshness_seconds=_DEFAULT_FRESHNESS_SECONDS,
            ),
        )

    @app.get("/v1/users/{username}")
    def public_user(username: str, request: Request) -> JSONResponse:
        _authorize(request, "user.read", production_database)
        try:
            normalized_username = normalize_username(username)
        except ValueError as error:
            raise ApiError(404, "user_not_found") from error
        result = production_database.get_public_user(normalized_username)
        if result is None:
            raise ApiError(404, "user_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.post("/v1/account")
    def create_account(body: AccountCreateBody, request: Request) -> JSONResponse:
        context = _authorize(
            request, "account.create", production_database, allow_unresolved_identity=True
        )
        assert production_database is not None
        if context.account is not None:
            raise ApiError(409, "account_exists")
        try:
            username = normalize_username(body.username)
            display_name = normalize_display_name(body.display_name)
        except ValueError as error:
            raise ApiError(422, "invalid_request") from error
        binding = _binding(
            request,
            context,
            "account.create",
            {"username": username, "display_name": display_name},
        )
        result = production_database.create_account(
            binding,
            username=username,
            normalized_username=username,
            display_name=display_name,
        )
        return _operation_response(result)

    @app.get("/v1/account")
    def get_account(request: Request) -> JSONResponse:
        context = _authorize(request, "account.read", production_database)
        assert production_database is not None and context.account is not None
        result = production_database.get_account(context.account.internal_id)
        if result is None:
            raise ApiError(404, "account_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.patch("/v1/account")
    def update_account(body: AccountUpdateBody, request: Request) -> JSONResponse:
        context = _authorize(request, "account.update", production_database)
        assert production_database is not None and context.account is not None
        try:
            username = normalize_username(body.username)
            display_name = normalize_display_name(body.display_name)
        except ValueError as error:
            raise ApiError(422, "invalid_request") from error
        if len(json.dumps(body.preferences, separators=(",", ":")).encode()) > 4096:
            raise ApiError(422, "invalid_request")
        result = production_database.update_account(
            _binding(
                request,
                context,
                "account.update",
                {
                    "username": username,
                    "display_name": display_name,
                    "preferences": body.preferences,
                },
            ),
            username=username,
            normalized_username=username,
            display_name=display_name,
            preferences=body.preferences,
        )
        return _operation_response(result)

    @app.get("/v1/account/saved-tags")
    def saved_tags(request: Request) -> JSONResponse:
        context = _authorize(request, "saved_tags.read", production_database)
        assert production_database is not None and context.account is not None
        return JSONResponse(
            status_code=200,
            content={
                "players": production_database.list_saved_players(
                    context.account.internal_id
                )
            },
        )

    @app.post("/v1/account/saved-tags")
    def add_saved_tag(body: SavedTagBody, request: Request) -> JSONResponse:
        context = _authorize(request, "saved_tags.write", production_database)
        assert production_database is not None
        tag = _safe_tag(body.tag)
        result = production_database.add_saved_player(
            _binding(request, context, "saved_tags.add", {"tag": tag}),
            normalized_tag=tag,
        )
        return _operation_response(result)

    @app.delete("/v1/account/saved-tags/{tag}")
    def remove_saved_tag(tag: str, request: Request) -> JSONResponse:
        context = _authorize(request, "saved_tags.write", production_database)
        assert production_database is not None
        normalized_tag = _safe_tag(tag)
        result = production_database.remove_saved_player(
            _binding(request, context, "saved_tags.remove", {"tag": normalized_tag}),
            normalized_tag=normalized_tag,
        )
        return _operation_response(result)

    @app.get("/v1/account/groups")
    def groups(request: Request) -> JSONResponse:
        context = _authorize(request, "groups.read", production_database)
        assert production_database is not None and context.account is not None
        return JSONResponse(
            status_code=200,
            content={
                "groups": production_database.list_groups(context.account.internal_id)
            },
        )

    @app.post("/v1/account/groups")
    def create_group(body: GroupBody, request: Request) -> JSONResponse:
        context = _authorize(request, "groups.write", production_database)
        assert production_database is not None
        name, normalized_name, tags = _group_values(body)
        result = production_database.create_group(
            _binding(
                request,
                context,
                "groups.create",
                {"name": name, "tags": tags},
            ),
            name=name,
            normalized_name=normalized_name,
            normalized_tags=tags,
        )
        return _operation_response(result)

    @app.patch("/v1/account/groups/{group_id}")
    def update_group(group_id: str, body: GroupBody, request: Request) -> JSONResponse:
        context = _authorize(request, "groups.write", production_database)
        assert production_database is not None
        _safe_uuid(group_id)
        name, normalized_name, tags = _group_values(body)
        result = production_database.update_group(
            _binding(
                request,
                context,
                "groups.update",
                {"group_id": group_id, "name": name, "tags": tags},
            ),
            group_id=group_id,
            name=name,
            normalized_name=normalized_name,
            normalized_tags=tags,
        )
        return _operation_response(result)

    @app.delete("/v1/account/groups/{group_id}")
    def delete_group(group_id: str, request: Request) -> JSONResponse:
        context = _authorize(request, "groups.write", production_database)
        assert production_database is not None
        _safe_uuid(group_id)
        result = production_database.delete_group(
            _binding(request, context, "groups.delete", {"group_id": group_id}),
            group_id=group_id,
        )
        return _operation_response(result)

    @app.get("/v1/account/summary")
    def summary(request: Request) -> JSONResponse:
        context = _authorize(request, "summary.read", production_database)
        assert production_database is not None and context.account is not None
        result = production_database.get_multi_account_summary(
            context.account.internal_id
        )
        if result is None:
            raise ApiError(404, "account_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.post("/v1/account/exports")
    def submit_export(body: ExportBody, request: Request) -> JSONResponse:
        context = _authorize(request, "exports.submit", production_database)
        assert production_database is not None
        result = production_database.submit_export(
            _binding(request, context, "exports.submit", {"format": body.format}),
            export_format=body.format,
        )
        return _operation_response(result)

    @app.get("/v1/account/exports/{export_id}")
    def export_status(export_id: str, request: Request) -> JSONResponse:
        context = _authorize(request, "exports.read", production_database)
        assert production_database is not None and context.account is not None
        _safe_uuid(export_id)
        result = production_database.get_export_status(
            context.account.internal_id, export_id
        )
        if result is None:
            raise ApiError(404, "export_not_found")
        return JSONResponse(status_code=200, content=result)

    @app.post("/v1/account/providers/{provider}")
    def link_provider(provider: str, body: ProviderLinkBody, request: Request) -> JSONResponse:
        context = _authorize(request, "providers.link", production_database)
        assert production_database is not None and context.account is not None
        if provider not in _ALLOWED_PROVIDERS:
            raise ApiError(404, "provider_not_found")
        result = production_database.link_provider(
            _binding(
                request,
                context,
                "providers.link",
                # The fresh subject joins the idempotency binding: a reused
                # request ID with another subject conflicts instead of
                # replaying the first outcome.
                {"provider": provider, "provider_subject": body.provider_subject},
            ),
            account_id=context.account.internal_id,
            provider=provider,
            provider_subject=body.provider_subject,
        )
        return _operation_response(result)

    @app.delete("/v1/account/providers/{provider}")
    def unlink_provider(provider: str, body: ProviderLinkBody, request: Request) -> JSONResponse:
        context = _authorize(request, "providers.unlink", production_database)
        assert production_database is not None and context.account is not None
        if provider not in _ALLOWED_PROVIDERS:
            raise ApiError(404, "provider_not_found")
        result = production_database.unlink_provider(
            _binding(
                request,
                context,
                "providers.unlink",
                {"provider": provider, "provider_subject": body.provider_subject},
            ),
            account_id=context.account.internal_id,
            provider=provider,
            provider_subject=body.provider_subject,
        )
        return _operation_response(result)

    @app.post("/v1/players/{tag}/verifytoken")
    async def verify_player_token(tag: str, request: Request) -> JSONResponse:
        context = _authorize(request, "player_links.verify", production_database)
        assert production_database is not None and context.account is not None
        normalized_tag = _safe_tag(tag)
        binding = _binding(
            request,
            context,
            "player_links.verify",
            {"tag": normalized_tag},
        )
        reservation = production_database.reserve_verification(
            binding, normalized_tag=normalized_tag
        )
        if not reservation.fresh:
            assert reservation.result is not None
            return _operation_response(reservation.result)
        try:
            token = _strict_verification_token(await request.body(), request.headers)
        except ApiError:
            result = production_database.complete_invalid_verification_request(
                binding,
                completed_at=current_time(),
            )
            return _operation_response(result)
        if verification_client is None or official_credential_fingerprint is None:
            result = production_database.complete_verification(
                binding,
                normalized_tag=normalized_tag,
                outcome=VerificationOutcome.UNAVAILABLE,
                account_id=context.account.internal_id,
                completed_at=current_time(),
            )
            return _operation_response(result)
        permit = production_database.acquire_official_permit(
            official_credential_fingerprint,
            caller="python",
            request_id=binding.request_id,
        )
        if not permit.granted:
            result = production_database.complete_verification(
                binding,
                normalized_tag=normalized_tag,
                outcome=VerificationOutcome.UNAVAILABLE,
                account_id=context.account.internal_id,
                completed_at=current_time(),
            )
            return _operation_response(result)
        try:
            official_response = verification_client.verify(normalized_tag, token)
            classification = classify_official_response(
                official_response.http_status, official_response.body
            )
        except VerificationTransportError:
            classification = classify_transport_ambiguity()
        except Exception:  # noqa: BLE001 - never repeat an ambiguous source call.
            classification = classify_transport_ambiguity()
        try:
            production_database.apply_official_key_action(
                official_credential_fingerprint,
                classification.key_action,
                cooldown_seconds=verification_cooldown_seconds,
            )
        except Exception:  # noqa: BLE001 - close the durable reservation safely.
            result = production_database.complete_verification(
                binding,
                normalized_tag=normalized_tag,
                outcome=VerificationOutcome.UNAVAILABLE,
                account_id=context.account.internal_id,
                completed_at=current_time(),
            )
            return _operation_response(result)
        result = production_database.complete_verification(
            binding,
            normalized_tag=normalized_tag,
            outcome=classification.outcome,
            account_id=context.account.internal_id,
            completed_at=current_time(),
        )
        return _operation_response(result)

    return app


class _AuthorizationContext:
    __slots__ = ("account", "proof")

    def __init__(self, proof: VerifiedProof, account: AccountContext | None) -> None:
        self.proof = proof
        self.account = account


def _authorize(
    request: Request,
    operation: str,
    database: ApiDatabase | None,
    *,
    allow_unresolved_identity: bool = False,
) -> _AuthorizationContext:
    proof: VerifiedProof = request.state.proof
    if operation in _PUBLIC_OPERATIONS:
        if proof.caller != "typescript-website":
            raise ApiError(403, "caller_operation_not_authorized")
        if (proof.provider or proof.provider_subject) and proof.provider not in _ALLOWED_PROVIDERS:
            raise ApiError(403, "caller_operation_not_authorized")
        account = (
            database.resolve_account(proof.provider, proof.provider_subject)
            if database is not None and proof.provider in _ALLOWED_PROVIDERS
            else None
        )
        return _AuthorizationContext(proof, account)
    if (
        proof.caller != "typescript-website"
        or operation not in _TYPESCRIPT_ACCOUNT_OPERATIONS
    ):
        raise ApiError(403, "caller_operation_not_authorized")
    if (
        database is None
        or proof.provider not in _ALLOWED_PROVIDERS
        or not proof.provider_subject
    ):
        raise ApiError(403, "caller_operation_not_authorized")
    account = database.resolve_account(proof.provider, proof.provider_subject)
    if account is None and not allow_unresolved_identity:
        raise ApiError(403, "account_not_found")
    return _AuthorizationContext(proof, account)


def _binding(
    request: Request,
    context: _AuthorizationContext,
    operation: str,
    identity: dict[str, Any],
) -> RequestBinding:
    return RequestBinding(
        request_id=context.proof.request_id,
        caller=context.proof.caller,
        provider=context.proof.provider,
        provider_subject=context.proof.provider_subject,
        account_id=None if context.account is None else context.account.internal_id,
        operation=operation,
        method=request.method,
        request_target=_request_target(request),
        identity=identity,
    )


def _operation_response(result: OperationResult) -> JSONResponse:
    return JSONResponse(
        status_code=result.status_code, content=_json_safe(result.payload)
    )


def _group_values(body: GroupBody) -> tuple[str, str, list[str]]:
    try:
        name = normalize_group_name(body.name)
        tags = sorted({_safe_tag(tag) for tag in body.tags})
    except ValueError as error:
        raise ApiError(422, "invalid_request") from error
    return name, name.casefold(), tags


def _safe_tag(tag: str) -> str:
    try:
        return normalize_player_tag(tag)
    except ValueError as error:
        raise ApiError(422, "invalid_tag") from error


def _safe_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ApiError(422, "invalid_request") from error
    if str(parsed) != value.lower():
        raise ApiError(422, "invalid_request")
    return str(parsed)


def _strict_verification_token(body: bytes, headers: Mapping[str, str]) -> str:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ApiError(422, "invalid_request")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ApiError(422, "invalid_request") from error
    if not isinstance(decoded, dict) or set(decoded) != {"token"}:
        raise ApiError(422, "invalid_request")
    token = decoded["token"]
    if (
        not isinstance(token, str)
        or not 1 <= len(token) <= 512
        or not token.isascii()
        or any(
            character.isspace() or not character.isprintable() for character in token
        )
    ):
        raise ApiError(422, "invalid_request")
    return token


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
        if declared_lengths[0] < 0 or declared_lengths[0] > limit:
            raise RequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise RequestBodyTooLarge
        body.extend(chunk)
    request._body = bytes(body)
    return bytes(body)


def _request_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
    query = request.scope.get("query_string", b"")
    target = raw_path
    if query:
        target += b"?" + query
    return target.decode("ascii")


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
