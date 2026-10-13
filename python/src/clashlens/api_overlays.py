"""OBS overlay: one per account, set up on the website's OBS overlay page and
shown in OBS through one private link (migration 0113).

The overlay shows one of the account's verified players; a player that leaves
the account stops being shown. Saving bumps the version, which an open
overlay reads every few seconds to apply the change. Making a new link
replaces the old one in place, so the old one shows nothing from then on.
Each read through the link marks the overlay open, at most once a minute,
and the collector then checks its player first (collector_overlays.py).
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import Field

from .api import ApiError, StrictBody, _authorize, _safe_tag
from .api_accounts import _linked_players
from .api_db import ApiDatabase, _text

_LINK = re.compile(r"[A-Za-z0-9_-]{22}")
# Each setting and the values it may take; numbers are stream pixels.
_CHOICES = {
    "text": {"light", "dark"},
    "bg": {"card", "tint", "none"},
    "fit": {"screen", "overlay"},
    "font": {"clashlens", "clean", "esports", "tech", "arcade"},
    "snap": {"tl", "tc", "tr", "bl", "bc", "br"},
    "popup": {"0"},
    "demo": {"1"},
}
_NUMBERS = {"w", "h", "x", "y", "cw", "ch"}
_PARTS = {"trophies", "rank", "attacks", "defenses", "net", "day", "updated"}
DEFAULTS = {"w": "366", "h": "222", "x": "40", "y": "40", "fit": "screen",
            "cw": "1920", "ch": "1080", "text": "light", "bg": "card", "snap": "tl"}


class OverlayBody(StrictBody):
    tag: str | None = Field(default=None, min_length=1, max_length=16)
    settings: dict[str, str] = Field(max_length=20)


class NewLinkBody(StrictBody):
    # The link being replaced: asking twice makes one new link, not two.
    replaces: str = Field(min_length=22, max_length=22)


def clean_settings(raw: dict[str, str]) -> dict[str, str]:
    """Only the known settings, each with an allowed value."""
    out = {}
    for key, value in raw.items():
        if value in _CHOICES.get(key, ()):
            out[key] = value
        elif key in _NUMBERS and value.isdigit() and int(value) <= 4000:
            out[key] = str(int(value))
        elif key == "show" and set(filter(None, value.split(","))) <= _PARTS:
            out[key] = value
    return out


def get_overlay(database: ApiDatabase, account_id: int) -> dict[str, Any]:
    """The account's overlay, made on first ask, and its verified players."""
    with database.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO obs_overlays (account_id, link_id, settings)
            VALUES (%s, %s, %s) ON CONFLICT (account_id) DO NOTHING
            """,
            (account_id, secrets.token_urlsafe(16), Jsonb(DEFAULTS)),
        )
        row = connection.execute(_OVERLAY_SQL + " WHERE overlay.account_id = %s",
                                 (account_id,)).fetchone()
        players = _linked_players(connection, account_id, None)
    return {
        "kind": "obs-overlay",
        **_overlay(row),
        "players": [
            {"tag": tag, "name": name, "league": league}
            for _id, tag, name, _clan, league in players
        ],
    }


def save_overlay(
    database: ApiDatabase, account_id: int, *, tag: str | None, settings: dict[str, str]
) -> dict[str, Any]:
    """Save the overlay's player and settings. Saving the same again is
    harmless, so a repeated request needs no record of its own."""
    with database.pool.connection() as connection:
        with connection.transaction():
            player_id = None
            if tag is not None:
                link = connection.execute(
                    """
                    SELECT link.player_id FROM verified_player_links AS link
                    JOIN players AS player ON player.id = link.player_id
                    WHERE player.normalized_tag = %s AND link.account_id = %s
                    """,
                    (tag, account_id),
                ).fetchone()
                if link is None:
                    raise ApiError(404, "player_not_verified")
                player_id = int(link[0])
            row = connection.execute(
                """
                UPDATE obs_overlays
                SET player_id = %s, settings = %s, version = version + 1,
                    updated_at = clock_timestamp()
                WHERE account_id = %s
                RETURNING version
                """,
                (player_id, Jsonb(clean_settings(settings)), account_id),
            ).fetchone()
    if row is None:
        raise ApiError(404, "overlay_not_found")
    return {"kind": "obs-overlay-saved", "version": int(row[0])}


def new_link(database: ApiDatabase, account_id: int, *, replaces: str) -> dict[str, Any]:
    """Replace the account's link, when it is still `replaces`."""
    with database.pool.connection() as connection:
        connection.execute(
            """
            UPDATE obs_overlays SET link_id = %s, version = version + 1,
                updated_at = clock_timestamp(), last_seen_at = NULL
            WHERE account_id = %s AND link_id = %s
            """,
            (secrets.token_urlsafe(16), account_id, replaces),
        )
        row = connection.execute(_OVERLAY_SQL + " WHERE overlay.account_id = %s",
                                 (account_id,)).fetchone()
    if row is None:
        raise ApiError(404, "overlay_not_found")
    return {"kind": "obs-overlay", **_overlay(row)}


def read_link(database: ApiDatabase, link_id: str) -> dict[str, Any] | None:
    """What an overlay in OBS shows, by its private link, or None for a link
    that does not exist or was replaced. Marks the overlay open."""
    with database.pool.connection() as connection:
        row = connection.execute(_OVERLAY_SQL + " WHERE overlay.link_id = %s",
                                 (link_id,)).fetchone()
        if row is None:
            return None
        # At most one write a minute per link; a no-op otherwise.
        connection.execute(
            """
            UPDATE obs_overlays SET last_seen_at = clock_timestamp()
            WHERE link_id = %s AND player_id IS NOT NULL
              AND (last_seen_at IS NULL OR last_seen_at < clock_timestamp() - interval '1 minute')
            """,
            (link_id,),
        )
    result = _overlay(row)
    del result["link"]
    return {"kind": "obs-overlay-link", **result}


# The player is shown only while it is still one of the account's verified players.
_OVERLAY_SQL = """
    SELECT overlay.link_id, overlay.settings, overlay.version,
           CASE WHEN link.player_id IS NOT NULL THEN player.normalized_tag END
    FROM obs_overlays AS overlay
    LEFT JOIN verified_player_links AS link
        ON link.player_id = overlay.player_id AND link.account_id = overlay.account_id
    LEFT JOIN players AS player ON player.id = overlay.player_id
"""


def _overlay(row: Any) -> dict[str, Any]:
    return {
        "link": _text(row[0]),
        "settings": dict(row[1]),
        "version": int(row[2]),
        "tag": None if row[3] is None else _text(row[3]),
    }


def register_overlay_routes(app: FastAPI, *, database: ApiDatabase | None) -> None:
    def account(request: Request, operation: str) -> int:
        context = _authorize(request, operation, database)
        assert database is not None and context.account is not None
        return context.account.internal_id

    @app.get("/v1/account/obs-overlay")
    def overlay(request: Request) -> JSONResponse:
        account_id = account(request, "account.read")
        assert database is not None
        return JSONResponse(content=get_overlay(database, account_id))

    @app.post("/v1/account/obs-overlay")
    def save(body: OverlayBody, request: Request) -> JSONResponse:
        account_id = account(request, "account.update")
        assert database is not None
        tag = None if body.tag is None else _safe_tag(body.tag)
        return JSONResponse(content=save_overlay(
            database, account_id, tag=tag, settings=body.settings
        ))

    @app.post("/v1/account/obs-overlay/link")
    def replace(body: NewLinkBody, request: Request) -> JSONResponse:
        account_id = account(request, "account.update")
        assert database is not None
        return JSONResponse(content=new_link(
            database, account_id, replaces=_link(body.replaces)
        ))

    @app.get("/v1/overlays/{link_id}")
    def by_link(link_id: str, request: Request) -> JSONResponse:
        _authorize(request, "overlay.read", database)
        if database is None:
            raise ApiError(404, "overlay_not_found")
        result = read_link(database, _link(link_id))
        if result is None:
            raise ApiError(404, "overlay_not_found")
        return JSONResponse(content=result)


def _link(value: str) -> str:
    # Checked before any database read, so a malformed link costs nothing.
    if not _LINK.fullmatch(value):
        raise ApiError(404, "overlay_not_found")
    return value
