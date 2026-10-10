"""Private API routes for crews. Every route answers 404 ``crews_disabled``
while crews are switched off, before checking the request or reading
anything."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from . import api_crews
from .accounts import normalize_crew_name, normalize_username
from .api import (
    ApiError,
    StrictBody,
    _authorize,
    _binding,
    _json_safe,
    _operation_response,
    _safe_tag,
    _safe_uuid,
)
from .api_db import ApiDatabase

_INVITE_CODE = re.compile(r"[A-Za-z0-9_-]{22}")


class CrewCreateBody(StrictBody):
    name: str = Field(min_length=1, max_length=80)
    size: int = api_crews.DEFAULT_CREW_SIZE
    tags: list[str] = Field(min_length=1, max_length=api_crews.MAX_CREW_SIZE)


class CrewUpdateBody(StrictBody):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    size: int | None = None


class CrewTagsBody(StrictBody):
    tags: list[str] = Field(min_length=1, max_length=api_crews.MAX_CREW_SIZE)


class CrewRoleBody(StrictBody):
    role: Literal["admin", "member"]


class CrewOwnerBody(StrictBody):
    username: str = Field(min_length=1, max_length=80)


class CrewInviteBody(StrictBody):
    new: bool = False


def register_crew_routes(
    app: FastAPI,
    *,
    database: ApiDatabase,
    current_time: Callable[[], datetime],
    enabled: bool,
) -> None:
    def switch() -> None:
        if not enabled:
            raise ApiError(404, "crews_disabled")

    router = APIRouter(dependencies=[Depends(switch)])

    def account_id(request: Request) -> int:
        context = _authorize(request, "crews.read", database)
        assert context.account is not None
        return context.account.internal_id

    def writer(request: Request) -> Any:
        return _authorize(request, "crews.write", database)

    def write(
        context: Any,
        request: Request,
        operation: str,
        identity: dict[str, Any],
        run: Callable[..., Any],
        **values: Any,
    ) -> JSONResponse:
        binding = _binding(request, context, f"crews.{operation}", identity)
        return _operation_response(run(database, binding, **values))

    @router.get("/v1/account/crews")
    def crews(request: Request) -> JSONResponse:
        return JSONResponse(content=api_crews.list_crews(database, account_id(request)))

    @router.post("/v1/account/crews")
    def create_crew(body: CrewCreateBody, request: Request) -> JSONResponse:
        context = writer(request)
        name = _crew_name(body.name)
        size = _crew_size(body.size)
        tags = _tags(body.tags)
        return write(
            context,
            request,
            "create",
            {"name": name, "size": size, "tags": tags},
            api_crews.create_crew,
            name=name,
            size=size,
            tags=tags,
        )

    @router.get("/v1/account/crews/{crew_id}")
    def crew(crew_id: str, request: Request) -> JSONResponse:
        result = api_crews.get_crew(
            database, account_id(request), _safe_uuid(crew_id), now=current_time()
        )
        if result is None:
            raise ApiError(404, "crew_not_found")
        return JSONResponse(content=_json_safe(result))

    @router.patch("/v1/account/crews/{crew_id}")
    def update_crew(crew_id: str, body: CrewUpdateBody, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        if (body.name is None) == (body.size is None):
            raise ApiError(422, "invalid_request")
        name = None if body.name is None else _crew_name(body.name)
        size = None if body.size is None else _crew_size(body.size)
        return write(
            context,
            request,
            "update",
            {"crew_id": crew_id, "name": name, "size": size},
            api_crews.update_crew,
            crew_id=crew_id,
            name=name,
            size=size,
        )

    @router.delete("/v1/account/crews/{crew_id}")
    def delete_crew(crew_id: str, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        return write(
            context, request, "delete", {"crew_id": crew_id}, api_crews.delete_crew,
            crew_id=crew_id,
        )

    @router.post("/v1/account/crews/{crew_id}/players")
    def add_players(crew_id: str, body: CrewTagsBody, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        tags = _tags(body.tags)
        return write(
            context,
            request,
            "add_players",
            {"crew_id": crew_id, "tags": tags},
            api_crews.add_players,
            crew_id=crew_id,
            tags=tags,
        )

    @router.delete("/v1/account/crews/{crew_id}/players/{tag}")
    def remove_player(crew_id: str, tag: str, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        tag = _safe_tag(tag)
        return write(
            context,
            request,
            "remove_player",
            {"crew_id": crew_id, "tag": tag},
            api_crews.remove_player,
            crew_id=crew_id,
            tag=tag,
        )

    @router.patch("/v1/account/crews/{crew_id}/members/{username}")
    def set_member_role(
        crew_id: str, username: str, body: CrewRoleBody, request: Request
    ) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        username = _username(username)
        return write(
            context,
            request,
            "set_role",
            {"crew_id": crew_id, "username": username, "role": body.role},
            api_crews.set_member_role,
            crew_id=crew_id,
            username=username,
            role=body.role,
        )

    @router.post("/v1/account/crews/{crew_id}/owner")
    def hand_over(crew_id: str, body: CrewOwnerBody, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        username = _username(body.username)
        return write(
            context,
            request,
            "hand_over",
            {"crew_id": crew_id, "username": username},
            api_crews.hand_over,
            crew_id=crew_id,
            username=username,
        )

    @router.delete("/v1/account/crews/{crew_id}/members/me")
    def leave_crew(crew_id: str, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        return write(
            context, request, "leave", {"crew_id": crew_id}, api_crews.leave_crew,
            crew_id=crew_id,
        )

    @router.post("/v1/account/crews/{crew_id}/invites")
    def make_invite(crew_id: str, body: CrewInviteBody, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        return write(
            context,
            request,
            "make_invite",
            {"crew_id": crew_id, "new": body.new},
            api_crews.make_invite,
            crew_id=crew_id,
            new=body.new,
            now=current_time(),
        )

    @router.delete("/v1/account/crews/{crew_id}/invites/{invite_id}")
    def revoke_invite(crew_id: str, invite_id: str, request: Request) -> JSONResponse:
        context = writer(request)
        crew_id = _safe_uuid(crew_id)
        invite_id = _safe_uuid(invite_id)
        return write(
            context,
            request,
            "revoke_invite",
            {"crew_id": crew_id, "invite_id": invite_id},
            api_crews.revoke_invite,
            crew_id=crew_id,
            invite_id=invite_id,
            now=current_time(),
        )

    @router.get("/v1/account/crew-invites/{code}")
    def invite(code: str, request: Request) -> JSONResponse:
        caller = account_id(request)
        result = api_crews.get_invite(
            database, caller, _invite_code(code), now=current_time()
        )
        return JSONResponse(content=_json_safe(result))

    @router.post("/v1/account/crew-invites/{code}/accept")
    def accept_invite(code: str, body: CrewTagsBody, request: Request) -> JSONResponse:
        context = writer(request)
        code = _invite_code(code)
        tags = _tags(body.tags)
        return write(
            context,
            request,
            "accept_invite",
            {"code": code, "tags": tags},
            api_crews.accept_invite,
            code=code,
            tags=tags,
            now=current_time(),
        )

    app.include_router(router)


def _crew_name(value: str) -> str:
    try:
        return normalize_crew_name(value)
    except ValueError as error:
        raise ApiError(422, "invalid_crew_name") from error


def _crew_size(value: int) -> int:
    if not api_crews.MIN_CREW_SIZE <= value <= api_crews.MAX_CREW_SIZE:
        raise ApiError(422, "invalid_crew_size")
    return value


def _tags(values: list[str]) -> list[str]:
    return sorted({_safe_tag(value) for value in values})


def _username(value: str) -> str:
    try:
        return normalize_username(value)
    except ValueError as error:
        raise ApiError(404, "member_not_found") from error


def _invite_code(value: str) -> str:
    # Checked before any database read, so a malformed code costs nothing.
    if not _INVITE_CODE.fullmatch(value):
        raise ApiError(422, "invalid_request")
    return value
