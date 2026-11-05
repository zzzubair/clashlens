"""View as: the site owner opens the dashboard as any player, read only.

Only an account listed in site_owner_accounts (migration 0138) is the owner,
decided here from the signed request's own account; nothing the browser sends
can make anyone the owner, and no request can add one. The answer is the same
player card a linked player gets, read only: this module writes nothing, and
the viewed player never becomes the acting account for anything.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import api_players
from .api import _authorize
from .api_db import ApiDatabase
from .profile import normalize_player_tag


def view_as(
    database: ApiDatabase, account_id: int, tag: str | None, *, now: datetime
) -> dict[str, Any]:
    """Whether the account is the owner and, for the owner only, the card of
    the player `tag` names; a malformed or unknown tag gives no card. Anyone
    else gets the same answer whatever the tag, before the tag is read."""
    with database.pool.connection() as connection:
        owner = connection.execute(
            "SELECT 1 FROM site_owner_accounts WHERE account_id = %s", (account_id,)
        ).fetchone()
        if owner is None:
            return {"kind": "view-as", "owner": False, "player": None}
        try:
            normalized = None if tag is None else normalize_player_tag(tag)
        except ValueError:
            normalized = None
        row = None if normalized is None else connection.execute(
            f"""
            SELECT {api_players.PLAYER_ROW_COLUMNS}
            FROM players AS player
            {api_players.NEWEST_PROFILE_JOIN}
            WHERE player.normalized_tag = %s
            """,
            (normalized,),
        ).fetchone()
        cards = (
            [] if row is None
            else api_players.player_cards(connection, [api_players.player_row(row)], now=now)
        )
    if cards:
        print(json.dumps({
            "event": "owner_view_as", "account_id": account_id,
            "tag": cards[0]["tag"], "at": now.isoformat(),
        }), flush=True)
    return {"kind": "view-as", "owner": True, "player": cards[0] if cards else None}


def register_view_as_routes(
    app: FastAPI, *, database: ApiDatabase | None, current_time: Callable[[], datetime]
) -> None:
    @app.get("/v1/account/view-as")
    def read(request: Request, tag: str | None = None) -> JSONResponse:
        context = _authorize(request, "account.read", database)
        assert database is not None and context.account is not None
        return JSONResponse(content=view_as(
            database, context.account.internal_id, tag, now=current_time()
        ))
