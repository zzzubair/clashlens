"""Shared bases: one notebook base shown through an unlisted link (migration
0125), the same kind of link as the OBS overlay's.

Share makes the link, 16 random bytes, or returns the one the base already
has, so sharing twice gives one link. Anyone with it sees the base's name,
its live record over the Legend days it ran, as the Clasher's own notebook
counts it, and, as the Clasher chooses, its in-game link and the names of
their players that ran it. Never its base ID or note. Stop sharing deletes
the link, so it shows nothing from then on; sharing again makes a new one.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from .api import ApiError, StrictBody, _authorize
from .api_db import ApiDatabase, _text
from .api_notebook import get_notebook, parse_code
from .domain import SEASON_DURATION

_LINK = re.compile(r"[A-Za-z0-9_-]{22}")


class ShareWrite(StrictBody):
    action: Literal["share", "settings", "stop"]
    base: str = Field(max_length=12)
    include_link: bool | None = None
    show_names: bool | None = None


def _share(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"link": _text(row[0]), "include_link": bool(row[1]),
            "show_names": bool(row[2])}


def save_share(database: ApiDatabase, account_id: int, body: ShareWrite) -> dict[str, Any]:
    """Share one of the account's bases, change what its page shows, or stop
    sharing it, and say how it is shared now."""
    with database.pool.connection() as connection, connection.transaction():
        row = connection.execute(
            "SELECT id FROM notebook_bases WHERE code = %s AND account_id = %s",
            (parse_code(body.base), account_id),
        ).fetchone()
        if row is None:
            raise ApiError(404, "base_not_found")
        base_id = int(row[0])
        if body.action == "share":
            connection.execute(
                """
                INSERT INTO notebook_shares (base_id, link_id) VALUES (%s, %s)
                ON CONFLICT (base_id) DO NOTHING
                """,
                (base_id, secrets.token_urlsafe(16)),
            )
        elif body.action == "settings":
            connection.execute(
                """
                UPDATE notebook_shares SET
                    include_link = COALESCE(%s, include_link),
                    show_names = COALESCE(%s, show_names)
                WHERE base_id = %s
                """,
                (body.include_link, body.show_names, base_id),
            )
        else:
            connection.execute("DELETE FROM notebook_shares WHERE base_id = %s", (base_id,))
        share = _share(connection.execute(
            "SELECT link_id, include_link, show_names FROM notebook_shares"
            " WHERE base_id = %s", (base_id,),
        ).fetchone())
    if share is None and body.action == "settings":
        raise ApiError(404, "share_not_found")
    return {"kind": "notebook-share", "base": body.base, "share": share}


def read_share(database: ApiDatabase, link_id: str, *, now: datetime) -> dict[str, Any] | None:
    """What the shared page shows, by its link, or None for a link that does
    not exist or was stopped. A base from a finished Season shows its record
    as of that Season's end."""
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT base.account_id, base.code, base.season_id, base.name,
                   share.include_link, share.show_names
            FROM notebook_shares AS share
            JOIN notebook_bases AS base ON base.id = share.base_id
            WHERE share.link_id = %s
            """,
            (link_id,),
        ).fetchone()
    if row is None:
        return None
    account_id, code, season_id, name, include_link, show_names = row
    season_end = datetime.fromtimestamp(int(_text(season_id)), UTC) + SEASON_DURATION
    try:
        notebook = get_notebook(
            database, int(account_id), base=_text(code),
            now=min(now, season_end - timedelta(seconds=1)),
        )
    except ApiError:
        # Deleted, or stopped, since the link was read.
        return None
    base = next(item for item in notebook["bases"] if item["id"] == _text(code))
    detail = notebook["base"]
    names = {
        player["tag"]: player["name"] or player["tag"] for player in notebook["players"]
    }
    return {
        "kind": "shared-base",
        "name": _text(name),
        "link": detail["link"] if include_link else None,
        "record": base["record"],
        "season": {"start": notebook["season"]["start"], "end": notebook["season"]["end"]},
        "as_of_day": notebook["as_of_day"],
        "players": [
            {"name": names[item["tag"]], "record": item["record"]}
            for item in detail["players"]
        ] if show_names else None,
    }


def register_share_routes(
    app: FastAPI,
    *,
    database: ApiDatabase,
    current_time: Callable[[], datetime],
    enabled: bool,
) -> None:
    """Sharing's two routes, both answering 404 ``notebook_disabled`` while
    the dashboard is switched off, before reading anything."""

    def switch() -> None:
        if not enabled:
            raise ApiError(404, "notebook_disabled")

    router = APIRouter(dependencies=[Depends(switch)])

    @router.post("/v1/account/notebook/share")
    def share(body: ShareWrite, request: Request) -> JSONResponse:
        context = _authorize(request, "account.update", database)
        assert context.account is not None
        return JSONResponse(content=save_share(database, context.account.internal_id, body))

    @router.get("/v1/notebook-shares/{link_id}")
    def shared(link_id: str, request: Request) -> JSONResponse:
        _authorize(request, "notebook_share.read", database)
        # Checked before any database read, so a malformed link costs nothing.
        if not _LINK.fullmatch(link_id):
            raise ApiError(404, "share_not_found")
        result = read_share(database, link_id, now=current_time())
        if result is None:
            raise ApiError(404, "share_not_found")
        return JSONResponse(content=result)

    app.include_router(router)
