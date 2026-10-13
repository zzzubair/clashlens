"""Attack alerts without a database: what Discord's refusals and outages
become, and the message itself."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import aiohttp
import discord
import pytest
from aiohttp import web

from bot.alerts import Refused, Unreachable
from bot.commands import Commands
from bot.discord_app import DiscordMessenger, build_client
from bot.replies import Reply, Site, attack_alert

SITE = Site("https://clashlens.test")


class FakeClient:
    """Opens a DM channel whose send fails as Discord answered, or fails to
    open it when `opening` fails."""

    def __init__(self, error: Exception | None, opening: Exception | None = None) -> None:
        self.error = error
        self.opening = opening
        self.sent: list[dict] = []

    async def create_dm(self, user: discord.abc.Snowflake) -> SimpleNamespace:
        if self.opening is not None:
            raise self.opening

        async def send(**message) -> None:
            if self.error is not None:
                raise self.error
            self.sent.append(message)

        return SimpleNamespace(send=send)


def _answer(status: int, code: int, headers: dict[str, str] | None = None) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="refused", headers=headers or {})
    kind = {403: discord.Forbidden, 404: discord.NotFound}.get(
        status, discord.DiscordServerError if status >= 500 else discord.HTTPException
    )
    return kind(response, {"code": code, "message": "refused"})


@pytest.mark.parametrize(
    ("status", "code", "reason"),
    [
        (403, 50278, "no_shared_server"),
        (403, 50007, "dms_closed"),
        (404, 10013, "unknown_user"),
        (400, 50033, "unknown_user"),
        (400, 50035, "other"),
    ],
)
def test_discord_refusals_are_recorded_with_their_reason(status, code, reason) -> None:
    messenger = DiscordMessenger(FakeClient(_answer(status, code)))  # type: ignore[arg-type]
    with pytest.raises(Refused) as refused:
        asyncio.run(messenger.send_dm("100000000000000001", Reply("hi", title="t"), 1))
    assert refused.value.reason == reason


def test_discord_outages_and_rate_limits_are_tried_again_not_refused() -> None:
    """Any failure but a refusal is tried again, whatever the cause: an
    outage, a rate limit, a timeout, an answer cut off, or a DM channel that
    could not be opened."""
    for client, wait in (
        (FakeClient(_answer(503, 0)), 0.0),
        (FakeClient(_answer(429, 0, {"Retry-After": "7.5"})), 7.5),
        (FakeClient(None, opening=TimeoutError()), 0.0),
        (FakeClient(TimeoutError()), 0.0),
        (FakeClient(aiohttp.ServerDisconnectedError()), 0.0),
    ):
        messenger = DiscordMessenger(client)  # type: ignore[arg-type]
        with pytest.raises(Unreachable) as outage:
            asyncio.run(messenger.send_dm("100000000000000001", Reply("hi", title="t"), 1))
        assert outage.value.retry_after == wait


def test_an_alert_names_the_defender_and_cannot_ping_anyone() -> None:
    client = FakeClient(None)
    reply = attack_alert(
        SITE,
        {
            "defender_tag": "#2PP",
            "defender_name": "Wanderer",
            "attacker_tag": "#8QQ",
            "attacker_name": "@everyone *Drift*",
            "stars": 3,
            "destruction_percentage": 100,
            "trophies_lost": 0,
            "saved_at": datetime(2026, 10, 12, 6, tzinfo=UTC),
        },
        "Unavailable",
    )
    asyncio.run(DiscordMessenger(client).send_dm("1", reply, 1))  # type: ignore[arg-type]
    embed = client.sent[0]["embed"]
    assert embed.title == "Wanderer #2PP was attacked"
    assert embed.description.splitlines() == [
        "@\u200beveryone \\*Drift\\* #8QQ: 3★ · 100% · 0 trophies",
        "@\u200beveryone \\*Drift\\*'s hit rate this Season: Unavailable",
    ]
    assert [item.url for item in client.sent[0]["view"].children] == [
        "https://clashlens.test/players/%232PP",
        "https://clashlens.test/account/discord-alerts",
    ]


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


def _unavailable(_n: int) -> web.Response:
    return _json({"message": "Service unavailable", "code": 0}, 503)


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


def test_a_dm_asks_discord_to_check_its_nonce(monkeypatch) -> None:
    """Against a stand-in for Discord that cannot take messages just then,
    the DM's request carries its nonce, which Discord is asked to check for
    repeats, and the failure is one to try again."""

    async def scenario() -> list[dict]:
        async with stand_in_discord(monkeypatch, _unavailable) as (client, sends):
            with pytest.raises(Unreachable):
                await DiscordMessenger(client).send_dm(
                    "100000000000000001", Reply("hi", title="t"), 7
                )
        return sends

    sends = asyncio.run(scenario())
    assert [(body["nonce"], body["enforce_nonce"]) for body in sends] == [("7", True)]


def test_a_dm_cut_off_in_a_wait_for_all_of_discord_holds_up_no_later_one(
    monkeypatch,
) -> None:
    """Discord answers the first DM by asking the bot to wait 5 seconds
    before any request. That DM is cut off first, as the alert loop cuts
    one off at the end of its 2 minutes, and the next DM still goes out at
    once."""

    def answer(n: int) -> web.Response:
        return _rate_limited(5.0, everything=True) if n == 1 else _unavailable(n)

    async def scenario() -> list[dict]:
        async with stand_in_discord(monkeypatch, answer) as (client, sends):
            messenger = DiscordMessenger(client)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    messenger.send_dm("100000000000000001", Reply("hi", title="t"), 7), 0.3
                )
            with pytest.raises(Unreachable):
                await asyncio.wait_for(
                    messenger.send_dm("100000000000000001", Reply("hi", title="t"), 8), 2.0
                )
        return sends

    assert [body["nonce"] for body in asyncio.run(scenario())] == ["7", "8"]
