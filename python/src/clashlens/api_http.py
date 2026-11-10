"""Shared checks and request values for the private HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from . import api_accounts
from .api_db import AccountContext, ApiDatabase, OperationResult, RequestBinding
from .hmac_proof import VerifiedProof
from .profile import normalize_player_tag

_PUBLIC_OPERATIONS = frozenset(
    {
        "analytics.read",
        "crews.view",
        "leaderboards.read",
        "overlay.read",
        "player.read",
        "refresh.status",
        "refresh.submit",
        "status.read",
        "user.read",
    }
)
ALLOWED_PROVIDERS = frozenset({"google", "discord"})
_TYPESCRIPT_ACCOUNT_OPERATIONS = frozenset(
    {
        "account.create",
        "account.read",
        "account.update",
        "crews.read",
        "crews.write",
        "groups.read",
        "groups.write",
        "player_links.verify",
        "providers.link",
        "providers.unlink",
        "session.check",
        "session.revoke",
        "summary.read",
    }
)


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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


class AuthorizationContext:
    __slots__ = ("account", "proof")

    def __init__(self, proof: VerifiedProof, account: AccountContext | None) -> None:
        self.proof = proof
        self.account = account


def authorize(
    request: Request,
    operation: str,
    database: ApiDatabase | None,
    *,
    allow_unresolved_identity: bool = False,
) -> AuthorizationContext:
    proof: VerifiedProof = request.state.proof
    if operation in _PUBLIC_OPERATIONS:
        if proof.caller != "typescript-website":
            raise ApiError(403, "caller_operation_not_authorized")
        if (
            proof.provider or proof.provider_subject
        ) and proof.provider not in ALLOWED_PROVIDERS:
            raise ApiError(403, "caller_operation_not_authorized")
        account = (
            api_accounts.resolve_account(database, proof.provider, proof.provider_subject)
            if database is not None and proof.provider in ALLOWED_PROVIDERS
            else None
        )
        return AuthorizationContext(proof, account)
    if (
        proof.caller != "typescript-website"
        or operation not in _TYPESCRIPT_ACCOUNT_OPERATIONS
    ):
        raise ApiError(403, "caller_operation_not_authorized")
    if (
        database is None
        or proof.provider not in ALLOWED_PROVIDERS
        or not proof.provider_subject
    ):
        raise ApiError(403, "caller_operation_not_authorized")
    account = api_accounts.resolve_account(database, proof.provider, proof.provider_subject)
    if account is None and not allow_unresolved_identity:
        raise ApiError(403, "account_not_found")
    return AuthorizationContext(proof, account)


def request_binding(
    request: Request,
    context: AuthorizationContext,
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
        request_target=request_target(request),
        identity=identity,
    )


def operation_response(result: OperationResult) -> JSONResponse:
    return JSONResponse(
        status_code=result.status_code, content=json_safe(result.payload)
    )


def safe_tag(tag: str) -> str:
    try:
        return normalize_player_tag(tag)
    except ValueError as error:
        raise ApiError(422, "invalid_tag") from error


def safe_uuid(value: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ApiError(422, "invalid_request") from error
    if str(parsed) != value.lower():
        raise ApiError(422, "invalid_request")
    return str(parsed)


def request_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
    query = request.scope.get("query_string", b"")
    target = raw_path
    if query:
        target += b"?" + query
    return target.decode("ascii")


def json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value
