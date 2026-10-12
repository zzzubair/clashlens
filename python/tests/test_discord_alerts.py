"""Attack alerts without a database: what Discord's refusals become, the
switch that turns alerts on, and the message itself."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from bot.__main__ import ConfigError, load_config
from bot.alerts import Refused, Unreachable
from bot.discord_app import DiscordMessenger
from bot.replies import Reply, Site, attack_alert

SITE = Site("https://clashlens.test")
SETTINGS = {
    "CLASHLENS_DISCORD_BOT_TOKEN_FILE": "/run/token",
    "CLASHLENS_DATABASE_URL_FILE": "/run/database",
    "CLASHLENS_PUBLIC_ORIGIN": "https://clashlens.test",
}


class FakeClient:
    """Opens a DM channel whose send fails as Discord answered."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.sent: list[dict] = []

    async def create_dm(self, user: discord.abc.Snowflake) -> SimpleNamespace:
        async def send(**message) -> None:
            if self.error is not None:
                raise self.error
            self.sent.append(message)

        return SimpleNamespace(send=send)


def _answer(status: int, code: int) -> discord.HTTPException:
    response = SimpleNamespace(status=status, reason="refused")
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
        (429, 40003, "other"),
    ],
)
def test_discord_refusals_are_recorded_with_their_reason(status, code, reason) -> None:
    messenger = DiscordMessenger(FakeClient(_answer(status, code)))  # type: ignore[arg-type]
    with pytest.raises(Refused) as refused:
        asyncio.run(messenger.send_dm("100000000000000001", Reply("hi", title="t")))
    assert refused.value.reason == reason


def test_discord_outages_count_as_undelivered_not_refused() -> None:
    for error in (_answer(503, 0), TimeoutError()):
        messenger = DiscordMessenger(FakeClient(error))  # type: ignore[arg-type]
        with pytest.raises(Unreachable):
            asyncio.run(messenger.send_dm("100000000000000001", Reply("hi", title="t")))


def test_alerts_are_off_unless_turned_on_and_a_typo_stops_the_bot() -> None:
    assert load_config(SETTINGS).alerts is False
    assert load_config({**SETTINGS, "CLASHLENS_DISCORD_ALERTS": "off"}).alerts is False
    assert load_config({**SETTINGS, "CLASHLENS_DISCORD_ALERTS": "on"}).alerts is True
    with pytest.raises(ConfigError):
        load_config({**SETTINGS, "CLASHLENS_DISCORD_ALERTS": "yes"})


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
    asyncio.run(DiscordMessenger(client).send_dm("1", reply))  # type: ignore[arg-type]
    embed = client.sent[0]["embed"]
    assert embed.title == "Wanderer #2PP was attacked"
    assert embed.description.splitlines() == [
        "@\u200beveryone \\*Drift\\* #8QQ: 3★ · 100% · 0 trophies",
        "@\u200beveryone \\*Drift\\*'s hit rate this Season: Unavailable",
    ]
    assert [item.url for item in client.sent[0]["view"].children] == [
        "https://clashlens.test/players/%232PP",
        "https://clashlens.test/account/providers",
    ]
