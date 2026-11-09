"""OBS overlay: one per account, set up on the website's Stream overlay page and
shown in OBS through one private link (migration 0113).

The overlay shows one of the account's verified players; a player that leaves
the account stops being shown. Saving bumps the version, which an open
overlay reads every few seconds to apply the change. Making a new link
replaces the old one in place, so the old one shows nothing from then on.
Each read through the link marks the overlay open, at most once a minute,
and the collector then checks its player first (collector_overlays.py).

Where a link stands is `LinkState`; whether OBS is watching is
`ViewerPresence`.
"""

from __future__ import annotations

import re
import secrets
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import Field

from .api_accounts import _linked_players
from .api_db import ApiDatabase, _text
from .api_http import ApiError, StrictBody, authorize, safe_tag
from .collector_overlays import PRESENCE_SQL, ViewerPresence

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


class LinkState(StrEnum):
    """Where one link id stands against an account's overlay.

    ABSENT: the account has no overlay yet, so no link; the first
    `get_overlay` makes it with its first link, CURRENT. CURRENT: the
    overlay's one link, which `read_link` answers. RETIRED: any other id,
    replaced, never made or malformed; it answers nothing for good, since
    links are random and never reused.

    `new_link` is the only move: given the CURRENT link it makes a new one
    CURRENT and the given one RETIRED, bumping the version and clearing the
    viewer presence; given a RETIRED one, such as a repeat of a request that
    already replaced it, it changes nothing and answers the overlay as it is;
    with ABSENT it answers overlay_not_found. Each move is one statement on
    the overlay row, so a failure leaves it as it was and a retry or restart
    reads the row again; nothing is held in memory.
    """

    ABSENT = "absent"
    CURRENT = "current"
    RETIRED = "retired"


def link_state(row: Any, link_id: str) -> LinkState:
    """Where `link_id` stands against the overlay `row` it was looked up
    with: ABSENT with no row, else CURRENT when it is the row's link and
    RETIRED when not. Looked up by the id itself, no row reads like a
    RETIRED one: nothing."""
    if row is None:
        return LinkState.ABSENT
    return LinkState.CURRENT if _text(row[0]) == link_id else LinkState.RETIRED


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
    """The account's overlay, made on first ask (ABSENT to CURRENT,
    `LinkState`; an overlay already made stays as it is), and its verified
    players."""
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
    """Replace the account's link, when `replaces` is CURRENT (`LinkState`),
    and make the overlay CLOSED (`ViewerPresence`) until OBS reads the new
    one; a RETIRED `replaces` changes nothing."""
    with database.pool.connection() as connection:
        moved = connection.execute(
            """
            UPDATE obs_overlays SET link_id = %s, version = version + 1,
                updated_at = clock_timestamp(), last_seen_at = NULL
            WHERE account_id = %s AND link_id = %s
            """,
            (secrets.token_urlsafe(16), account_id, replaces),
        ).rowcount
        row = connection.execute(_OVERLAY_SQL + " WHERE overlay.account_id = %s",
                                 (account_id,)).fetchone()
    was = LinkState.CURRENT if moved else link_state(row, replaces)
    if was is LinkState.ABSENT:
        raise ApiError(404, "overlay_not_found")
    return {"kind": "obs-overlay", **_overlay(row)}


def read_link(database: ApiDatabase, link_id: str) -> dict[str, Any] | None:
    """What an overlay in OBS shows, by its private link, or None for a link
    that does not exist or was replaced. Marks the overlay open."""
    with database.pool.connection() as connection:
        row = connection.execute(_OVERLAY_SQL + " WHERE overlay.link_id = %s",
                                 (link_id,)).fetchone()
        if link_state(row, link_id) is not LinkState.CURRENT:
            return None
        # Seen now, while a player is chosen: one write a minute per link at
        # most, as a JUST_SEEN overlay writes nothing.
        connection.execute(
            f"""
            UPDATE obs_overlays AS overlay SET last_seen_at = clock_timestamp()
            WHERE overlay.link_id = %s AND overlay.player_id IS NOT NULL
              AND {PRESENCE_SQL} <> '{ViewerPresence.JUST_SEEN}'
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
        context = authorize(request, operation, database)
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
        tag = None if body.tag is None else safe_tag(body.tag)
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
        authorize(request, "overlay.read", database)
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
