"""Attack alerts without a database: what Discord's refusals and outages
become, and the message itself."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import discord
import pytest
from aiohttp import web

from bot.alerts import AlertLoop, AlertStore, Refused, Unreachable
from bot.commands import Commands
from bot.discord_app import DiscordMessenger, build_client
from bot.replies import PLAYER_COLOURS, Reply, Site, attack_alert, say
from clashlens.domain import ranked_day_for

SITE = Site("https://clashlens.test")


class FakeClient:
    """Opens a DM channel whose send fails as Discord answered, or fails to
    open it when `opening` fails."""

    def __init__(self, error: Exception | None, opening: Exception | None = None) -> None:
        self.error = error
        self.opening = opening
        self.sent: list[dict] = []
        self.http = SimpleNamespace(max_ratelimit_timeout=None)

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
        asyncio.run(messenger.send_dm("100000000000000001", say("hi"), 1, 60.0))
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
            asyncio.run(
                messenger.send_dm("100000000000000001", say("hi"), 1, 60.0)
            )
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
            "battle_id": 41,
            "ranked_day_start": datetime(2026, 10, 12, 5, tzinfo=UTC),
        },
        "Unavailable",
        PLAYER_COLOURS[1],
    )
    asyncio.run(DiscordMessenger(client).send_dm("1", reply, 1, 60.0))  # type: ignore[arg-type]
    items = list(client.sent[0]["view"].walk_children())
    text = "\n".join(item.content for item in items if isinstance(item, discord.ui.TextDisplay))
    assert text.splitlines() == [
        "### Wanderer was attacked",
        f"-# #2PP · <t:{int(datetime(2026, 10, 12, 6, tzinfo=UTC).timestamp())}:t>",
        "**@\u200beveryone \\*Drift\\*** #8QQ",
        "★★★  ·  100%  ·  **0** trophies",
        "-# @\u200beveryone \\*Drift\\*'s hit rate in this attack's Season: Unavailable",
        "-# [Alert settings](https://clashlens.test/account/discord-alerts)",
    ]
    # The defender's colour, and a button to the battle highlighted on its Legend day.
    assert [
        item.accent_colour for item in items if isinstance(item, discord.ui.Container)
    ] == [PLAYER_COLOURS[1]]
    assert [item.url for item in items if isinstance(item, discord.ui.Button)] == [
        "https://clashlens.test/players/%232PP?day=2026-10-12#battle-41"
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


def test_a_rate_limit_longer_than_a_dms_time_left_fails_it_at_once(monkeypatch) -> None:
    """100 seconds into a DM's 2 minutes, Discord rate limits it for 60
    seconds. The DM's one request carried its nonce, which Discord is asked
    to check for repeats, and it fails at once, asking for that wait, instead
    of waiting out the 20 seconds left."""

    async def scenario() -> tuple[list[dict], Unreachable]:
        async with stand_in_discord(monkeypatch, lambda _n: _rate_limited(60.0)) as (
            client,
            sends,
        ):
            with pytest.raises(Unreachable) as outage:
                await asyncio.wait_for(
                    DiscordMessenger(client).send_dm(
                        "100000000000000001", say("hi"), 7, 20.0
                    ),
                    5.0,
                )
        return sends, outage.value

    sends, outage = asyncio.run(scenario())
    assert [(body["nonce"], body["enforce_nonce"]) for body in sends] == [("7", True)]
    assert outage.retry_after == 60.0


def test_a_dm_cut_off_in_a_wait_for_all_of_discord_holds_up_no_later_one(
    monkeypatch,
) -> None:
    """Discord answers the first DM by asking the bot to wait 5 seconds
    before any request. That DM runs out of time first, as one does when its
    2 minutes end during the wait, and the next DM still goes out at once."""

    def answer(n: int) -> web.Response:
        return _rate_limited(5.0, everything=True) if n == 1 else delivered(n)

    async def scenario() -> list[dict]:
        async with stand_in_discord(monkeypatch, answer) as (client, sends):
            messenger = DiscordMessenger(client)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(
                    messenger.send_dm("100000000000000001", say("hi"), 7, 10.0),
                    0.3,
                )
            await asyncio.wait_for(
                messenger.send_dm("100000000000000001", say("hi"), 8, 10.0), 2.0
            )
        return sends

    assert [body["nonce"] for body in asyncio.run(scenario())] == ["7", "8"]


class SeasonPages:
    """Player pages as Clash Lens reads them at a moment: every Legend day of
    that moment's Season so far, 3 three-stars in 4 attacks a day in the
    Season before `season_start`, 1 in 1 from it."""

    def __init__(self, season_start: datetime) -> None:
        self.season_start = season_start

    def player_page(self, _tag: str, now: datetime) -> dict:
        day = ranked_day_for(now)
        stars, attacks = (1, 1) if day.season_start == self.season_start else (3, 4)
        starts = [
            (day.season_start + timedelta(days=offset)).isoformat()
            for offset in range(day.day_number)
        ]
        return {
            "screen_ready": {
                "season": {
                    "start": day.season_start.isoformat(),
                    "current_day_number": day.day_number,
                },
                "season_day_starts": starts,
                "days": [
                    {"ranked_day_start": start, "attack_count": attacks,
                     "attack_three_star_count": stars}
                    for start in starts
                ],
            }
        }


def test_an_alert_rates_the_attacker_over_the_attacks_season() -> None:
    """The attacker's hit rate is the one for the Season of the attack,
    whether the bot reads it after the Reset that ended that Season or in a
    round that began before the Reset that started it."""
    season = ranked_day_for(datetime(2026, 10, 13, 12, tzinfo=UTC)).season_start
    alerts = AlertStore(None, SeasonPages(season))  # type: ignore[arg-type]
    last_day = season - timedelta(days=1)
    assert alerts.hit_rate("#8QQ", last_day, season + timedelta(minutes=2)) == "75%"
    assert alerts.hit_rate("#8QQ", season, season - timedelta(seconds=1)) == "100%"
    assert alerts.hit_rate("#8QQ", season, season + timedelta(hours=1)) == "100%"


class ResetRound(AlertStore):
    """The alert reads with no database: a confirmation, then an attack on
    the Legend day starting at `reset`, the Season's second, are due. The
    attacker made 3 three-stars in 4 attacks the day before and none in 1
    attack that day."""

    def __init__(self, reset: datetime) -> None:
        super().__init__(None, self)  # type: ignore[arg-type]
        self.reset = reset

    def prune(self) -> None:
        pass

    def confirmations_due(self) -> list[dict]:
        return [{"account_id": 1, "discord_user_id": "1", "player_id": 2, "tag": "#2PP",
                 "name": "Wanderer", "confirm_requested_at": self.reset,
                 "colour": PLAYER_COLOURS[0]}]

    def due(self) -> list[dict]:
        return [{"account_id": 1, "discord_user_id": "1", "battle_id": 3,
                 "defender_player_id": 2, "defender_tag": "#2PP", "defender_name": "Wanderer",
                 "attacker_tag": "#8QQ", "attacker_name": "Drift", "stars": 2,
                 "destruction_percentage": 87, "trophies_lost": 24, "saved_at": self.reset,
                 "ranked_day_start": self.reset, "colour": PLAYER_COLOURS[0]}]

    def claim(self, delivery: object) -> int:
        return 3 if delivery.battle_id else 2  # type: ignore[attr-defined]

    def finish(self, *_outcome: object) -> None:
        pass

    def player_page(self, _tag: str, now: datetime) -> dict:
        day = ranked_day_for(now)
        results = {self.reset - timedelta(days=1): (3, 4), self.reset: (0, 1)}
        starts = [day.season_start + timedelta(days=offset) for offset in range(day.day_number)]
        return {
            "screen_ready": {
                "season": {
                    "start": day.season_start.isoformat(),
                    "current_day_number": day.day_number,
                },
                "season_day_starts": [start.isoformat() for start in starts],
                "days": [
                    {"ranked_day_start": start.isoformat(),
                     "attack_three_star_count": results[start][0],
                     "attack_count": results[start][1]}
                    for start in starts
                ],
            }
        }


def test_an_alert_rates_the_attacker_as_of_when_it_is_written() -> None:
    """A round begins 30 seconds before a Reset, and the confirmation before
    the alert takes 90 seconds to send. The alert written after it counts
    the attacker's Legend day that Reset began: 3 three-stars in 5 attacks,
    not 3 in 4 as at the start of the round."""
    reset = ranked_day_for(datetime(2026, 10, 13, 12, tzinfo=UTC)).season_start + timedelta(
        days=1)
    moment = [reset - timedelta(seconds=30)]
    sent: list[Reply] = []

    class SlowDiscord:
        async def send_dm(self, _user: str, reply: Reply, _nonce: int, _left: float) -> None:
            sent.append(reply)
            moment[0] += timedelta(seconds=90)

    loop = AlertLoop(ResetRound(reset), SlowDiscord(), SITE, now=lambda: moment[0])
    assert asyncio.run(loop.tick()) == 2
    assert "-# Drift's hit rate in this attack's Season: 60%" in sent[1].text.splitlines()


def test_only_an_attacker_with_no_saved_days_is_not_tracked_yet() -> None:
    """An attacker Clash Lens never read, or read with no days saved, is not
    tracked yet. One with days saved whose rate cannot be worked out now, as
    while today's result awaits publishing with no earlier Season on record,
    is unavailable."""
    day = datetime(2026, 10, 13, 5, tzinfo=UTC)

    def rate(page: dict | None) -> str:
        store = SimpleNamespace(player_page=lambda _tag, _now: page)
        return AlertStore(None, store).hit_rate("#8QQ", day, day)  # type: ignore[arg-type]

    saved = {"ranked_day_start": day.isoformat(), "attack_count": 4,
             "attack_three_star_count": 3}
    assert rate(None) == "not tracked yet"
    assert rate({"tag": "#8QQ", "state": "known"}) == "not tracked yet"
    assert rate({"screen_ready": {"season": None, "season_day_starts": [], "days": []}}) == (
        "not tracked yet")
    assert rate({"screen_ready": {"season": None, "season_day_starts": [day.isoformat()],
                                  "days": [saved]}}) == "Unavailable"
