"""View as: the site owner opens the dashboard as any player, read only.

Only an account listed in site_owner_accounts (migration 0138) is the owner,
decided here from the signed request's own account; nothing the browser sends
can make anyone the owner, and no request can add one. The answer is the same
player card a linked player gets, read only, and the viewed player never
becomes the acting account for anything.

The owner may also pin one player, so a plain load of their dashboard opens
that player's View as, and follow any player's Discord attack alerts and
Legend day summary on their own Discord (migration 0139). Both write only the
owner's own rows, keyed by the owner's account: site_owner_pins, and the same
discord_attack_alerts and discord_day_summary_on rows a linked player's
switches are. Every other account gets one answer whatever the tag, before
the tag is read, and nothing is written for it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from . import api_db, api_discord_alerts, api_players
from .api import StrictBody, _authorize, _binding, _operation_response
from .api_db import ApiDatabase, OperationResult, RequestBinding, _text
from .api_discord_alerts import Switch, SwitchState
from .profile import normalize_player_tag

# What any account but the owner gets from a pin or follow, whatever it sent.
NOT_OWNER = OperationResult(404, {"error": "not_found"})
# The owner's switch for each alert, as the Discord alerts page names them.
FOLLOW_TABLES = {"attack": Switch.ATTACK, "day_summary": Switch.DAY_SUMMARY}


def _is_owner(connection: Any, account_id: int | None) -> bool:
    return account_id is not None and connection.execute(
        "SELECT 1 FROM site_owner_accounts WHERE account_id = %s", (account_id,)
    ).fetchone() is not None


def _player_id(connection: Any, tag: str | None) -> tuple[int, str] | None:
    """The saved player a typed tag names, with its tag; None when it is
    malformed or no player has it."""
    try:
        normalized = None if tag is None else normalize_player_tag(tag)
    except ValueError:
        return None
    row = None if normalized is None else connection.execute(
        "SELECT id FROM players WHERE normalized_tag = %s", (normalized,)
    ).fetchone()
    return None if row is None or normalized is None else (int(row[0]), normalized)


def view_as(
    database: ApiDatabase, account_id: int, tag: str | None, *, now: datetime
) -> dict[str, Any]:
    """Whether the account is the owner and, for the owner only, the card of
    the player `tag` names (none for a malformed or unknown tag), the owner's
    pinned player and the owner's own alert switches for the viewed player.
    Anyone else gets the same answer whatever the tag, before it is read."""
    with database.pool.connection() as connection:
        if not _is_owner(connection, account_id):
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
        pinned = connection.execute(
            """
            SELECT player.normalized_tag FROM site_owner_pins AS pin
            JOIN players AS player ON player.id = pin.player_id
            WHERE pin.account_id = %s
            """,
            (account_id,),
        ).fetchone()
        viewed = cards[0]["tag"] if cards else None
        follow = {
            alert: viewed is not None and connection.execute(
                f"""
                SELECT 1 FROM {table} AS switch
                JOIN players AS player ON player.id = switch.player_id
                WHERE switch.account_id = %s AND player.normalized_tag = %s
                """,
                (account_id, viewed),
            ).fetchone() is not None
            for alert, table in FOLLOW_TABLES.items()
        }
    if cards:
        print(json.dumps({
            "event": "owner_view_as", "account_id": account_id,
            "tag": cards[0]["tag"], "at": now.isoformat(),
        }), flush=True)
    return {
        "kind": "view-as",
        "owner": True,
        "player": cards[0] if cards else None,
        "pinned": None if pinned is None else _text(pinned[0]),
        "follow": follow,
    }


def set_pin(database: ApiDatabase, binding: RequestBinding, *, tag: str | None) -> OperationResult:
    """Pin the player `tag` names on the owner's dashboard, or unpin with
    None. Anyone but the owner gets NOT_OWNER whatever the tag."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            if not _is_owner(connection, binding.account_id):
                result = NOT_OWNER
            elif tag is None:
                connection.execute(
                    "DELETE FROM site_owner_pins WHERE account_id = %s", (binding.account_id,)
                )
                result = OperationResult(200, {"pinned": None})
            elif (player := _player_id(connection, tag)) is None:
                result = OperationResult(404, {"error": "player_not_found"})
            else:
                connection.execute(
                    """
                    INSERT INTO site_owner_pins (account_id, player_id) VALUES (%s, %s)
                    ON CONFLICT (account_id) DO UPDATE
                    SET player_id = EXCLUDED.player_id, pinned_at = clock_timestamp()
                    """,
                    (binding.account_id, player[0]),
                )
                result = OperationResult(200, {"pinned": player[1]})
            api_db._complete_request(connection, binding.request_id, result)
            return result


def set_follow(
    database: ApiDatabase,
    binding: RequestBinding,
    *,
    tag: str,
    alert: Literal["attack", "day_summary"],
    on: bool,
) -> OperationResult:
    """Turn the owner's own attack alerts or Legend day summary for any
    player on or off. Only the owner's row changes, never the linked
    account's. Anyone but the owner gets NOT_OWNER whatever the tag."""
    with database.pool.connection() as connection:
        with connection.transaction():
            existing = api_db._reserve_request(connection, binding)
            if existing is not None:
                return existing
            if not _is_owner(connection, binding.account_id):
                result = NOT_OWNER
            elif (player := _player_id(connection, tag)) is None:
                result = OperationResult(404, {"error": "player_not_found"})
            else:
                assert binding.account_id is not None
                api_discord_alerts.flip(
                    connection, FOLLOW_TABLES[alert], binding.account_id, player[0],
                    SwitchState.of(on),
                )
                result = OperationResult(200, {"tag": player[1], "alert": alert, "on": on})
            api_db._complete_request(connection, binding.request_id, result)
            return result


class PinBody(StrictBody):
    # None unpins. Any text up to this long is read as a tag, so every
    # account but the owner gets one answer for every tag.
    tag: str | None = Field(default=None, max_length=64)


class FollowBody(StrictBody):
    tag: str = Field(max_length=64)
    alert: Literal["attack", "day_summary"]
    on: bool


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

    @app.post("/v1/account/view-as/pin")
    def pin(body: PinBody, request: Request) -> JSONResponse:
        context = _authorize(request, "account.update", database)
        assert database is not None and context.account is not None
        return _operation_response(set_pin(
            database, _binding(request, context, "account.update", {"owner_pin": body.tag}),
            tag=body.tag,
        ))

    @app.post("/v1/account/view-as/follow")
    def follow(body: FollowBody, request: Request) -> JSONResponse:
        context = _authorize(request, "account.update", database)
        assert database is not None and context.account is not None
        return _operation_response(set_follow(
            database,
            _binding(request, context, "account.update",
                     {"owner_follow": body.tag, "alert": body.alert, "on": body.on}),
            tag=body.tag, alert=body.alert, on=body.on,
        ))
