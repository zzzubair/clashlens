"""Shared setup for discord alerts tests."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import discord
from aiohttp import web
from PIL import Image

from bot import picture
from bot.commands import Commands
from bot.discord_app import build_client
from bot.replies import Site

SITE = Site("https://clashlens.test")


# The website's light and dark page backgrounds.
LIGHT, DARK = (0xF4, 0xF4, 0xF5), (0x05, 0x05, 0x06)


def a_day(defenses: int = 1) -> dict:
    """A Legend day as the alert's picture draws it, with 2 attacks and
    `defenses` defenses, the last new; its start rank not known yet."""
    battle = {"name": "Drift", "stars": 3, "destruction": 100, "trophies": 40, "new": False}
    return {"name": "Wanderer", "day_number": 12, "date": datetime(2026, 10, 12, 5, tzinfo=UTC),
            "complete": True, "start_trophies": 5312, "trophies": 80 - 40 * defenses + 5312, "start_rank": None,
            "rank": 1187, "attacks": [battle, battle],
            "defenses": [{**battle, "trophies": -40, "new": index == defenses - 1}
                         for index in range(defenses)]}


def background(png: bytes) -> tuple[int, ...]:
    """The colour of a picture's bottom left corner."""
    with Image.open(io.BytesIO(png)) as drawn:
        assert (drawn.format, drawn.size) == ("PNG", (picture.WIDTH, picture.HEIGHT))
        return drawn.convert("RGB").getpixel((4, picture.HEIGHT - 4))


def _json(payload: dict, status: int = 200, **headers: str) -> web.Response:
    return web.Response(
        body=json.dumps(payload).encode(),
        status=status,
        headers={"Content-Type": "application/json", **headers},
    )


def _rate_limited(seconds: float, *, everything: bool = False) -> web.Response:
    """Discord's 429, for one route or, when `everything`, for the whole bot."""
    return _json(
        {"message": "You are being rate limited.", "retry_after": seconds, "global": everything},
        429,
        Via="1.1 google",
    )


def delivered(n: int) -> web.Response:
    """Discord's answer to the nth message it took."""
    return _json({
        "id": str(100 + n), "channel_id": "10", "type": 0, "content": "",
        "author": {"id": "1", "username": "bot", "discriminator": "0", "avatar": None},
        "timestamp": "2026-10-13T05:00:00+00:00", "edited_timestamp": None, "tts": False,
        "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [],
        "embeds": [], "pinned": False,
    })


@asynccontextmanager
async def stand_in_discord(
    monkeypatch, answer: Callable[[int], web.Response]
) -> AsyncIterator[tuple[discord.Client, list[dict]]]:
    """The bot's real client, logged in to a local stand-in for Discord that
    answers the nth message with `answer(n)`; yields it and each message
    sent."""
    sends: list[dict] = []

    async def me(_request: web.Request) -> web.Response:
        return _json({"id": "1", "username": "bot", "discriminator": "0", "avatar": None})

    async def open_dm(_request: web.Request) -> web.Response:
        user = {"id": "100000000000000001", "username": "u", "discriminator": "0", "avatar": None}
        return _json({"id": "10", "type": 1, "recipients": [user]})

    async def send(request: web.Request) -> web.Response:
        sends.append(await request.json())
        return answer(len(sends))

    app = web.Application()
    app.router.add_get("/api/v10/users/@me", me)
    app.router.add_post("/api/v10/users/@me/channels", open_dm)
    app.router.add_post("/api/v10/channels/10/messages", send)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    monkeypatch.setattr(
        discord.http.Route, "BASE", f"http://127.0.0.1:{runner.addresses[0][1]}/api/v10"
    )
    client, _tree = build_client(Commands(store=None, site=SITE))  # type: ignore[arg-type]
    await client.http.static_login("token")
    try:
        yield client, sends
    finally:
        await client.http.close()
        await runner.cleanup()
