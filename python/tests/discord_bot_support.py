"""Shared setup for discord bot tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot import replies
from bot.commands import Commands
from bot.discord_app import DiscordApp
from bot.replies import Site
from clashlens.api_player_presentation import screen_daily_log_with_events
from clashlens.api_requests import AccountContext
from clashlens.domain import ranked_day_for

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


TODAY = ranked_day_for(NOW)


RESET = f"Reset <t:{int(TODAY.end.timestamp())}:t> (<t:{int(TODAY.end.timestamp())}:R>)"


SITE = Site("https://clashlens.test")


ME = 100000000000000001


def card(tag: str, name: str, trophies: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "tag": tag,
        "name": name,
        "clan": None,
        "state": "tracking",
        "reason": None,
        "trophies": trophies,
        "season_reset_pending": False,
        "rank": 1200,
        "today": {"net": 40, "attacks": 3, "defenses": 2},
        "observed_at": NOW - timedelta(minutes=2),
        **extra,
    }


def battle(lens: str, minute: int, stars: int, trophies: int, name: str) -> dict[str, Any]:
    return {
        "lens": lens,
        "battle_id": f"{lens}-{minute}",
        "battle_timestamp": (TODAY.start + timedelta(minutes=minute)).isoformat(),
        "opponent": {"tag": "#2PP", "name": name},
        "stars": stars,
        "destruction_percentage": 100 if stars == 3 else 71,
        "trophy_change": trophies,
    }


def page(tag: str, name: str, battles: list[dict[str, Any]], reasons=()) -> dict[str, Any]:
    attacks = [item for item in battles if item["lens"] == "offense"]
    defenses = [item for item in battles if item["lens"] == "defense"]
    day = screen_daily_log_with_events(
        {
            "ranked_day_start": TODAY.start.isoformat(),
            "ranked_day_end": TODAY.end.isoformat(),
            "official_season_id": TODAY.official_season_id,
            "season_day_number": TODAY.day_number,
            "version": 1,
            "state": "Live",
            "coverage": "partial",
            "confidence": "partial",
            "attack_count": len(attacks),
            "attack_three_star_count": sum(item["stars"] == 3 for item in attacks),
            "attack_gain": sum(item["trophy_change"] for item in attacks),
            "defense_count": len(defenses),
            "defense_three_star_count": sum(item["stars"] == 3 for item in defenses),
            "defense_loss": -sum(item["trophy_change"] for item in defenses),
            "net_trophy_change": None,
            "adjustments": [],
            "battles": battles,
            "partial_reasons": list(reasons),
            "start_trophies": 5796,
            "start_trophies_source": None,
        },
        "high",
        NOW,
    )
    return {
        "tag": tag,
        "name": name,
        "clan": "Lens Clan",
        "state": "tracking",
        "reason": None,
        "rank": 1234,
        "trophies": 5842,
        "season_reset_pending": False,
        "observed_at": NOW - timedelta(minutes=2),
        "screen_ready": {
            "days": [day],
            "current_day_start": day["ranked_day_start"],
            "season": None,
            "data_quality": [],
        },
    }


class FakeStore:
    def __init__(self) -> None:
        self.accounts: dict[str, AccountContext] = {}
        self.cards: dict[int, list[dict[str, Any]]] = {}
        self.mains: dict[int, str] = {}
        # Accounts whose default read finds no player, as when their links
        # are removed between the player and default reads.
        self.no_default: set[int] = set()
        # Accounts that chose the Legend day picture dark.
        self.dark: set[int] = set()
        self.pages: dict[str, dict[str, Any]] = {}
        self.fail: Exception | None = None
        self.reads: list[str] = []
        self.moments: list[datetime] = []

    def connect(self, discord_id: int, cards: list[dict[str, Any]], username="drift") -> None:
        account_id = len(self.accounts) + 1
        self.accounts[str(discord_id)] = AccountContext(account_id, "public", username, username)
        self.cards[account_id] = cards

    def _read(self, name: str) -> None:
        self.reads.append(name)
        if self.fail is not None:
            raise self.fail

    def account(self, discord_id):
        self._read("account")
        return self.accounts.get(discord_id)

    def players(self, account, now):
        self._read("players")
        self.moments.append(now)
        return self.cards[account.internal_id]

    def colours(self, account):
        self._read("colours")
        return replies.colours([item["tag"] for item in self.cards[account.internal_id]])

    def alert_switches(self, account):
        self._read("alert_switches")
        return [
            {"tag": item["tag"], "name": item["name"], "on": index == 0, "summary_on": False}
            for index, item in enumerate(self.cards[account.internal_id])
        ]

    def dark_picture(self, account):
        self._read("dark_picture")
        return account.internal_id in self.dark

    def default_tag(self, account):
        self._read("default_tag")
        # As the database does: the chosen default while still verified,
        # otherwise the first verified player.
        own = [item["tag"] for item in self.cards[account.internal_id]]
        if account.internal_id in self.no_default or not own:
            return None
        tag = self.mains.get(account.internal_id)
        return tag if tag in own else own[0]

    def player_page(self, tag, now):
        self._read("player_page")
        self.moments.append(now)
        return self.pages.get(tag)


class FakeResponse:
    def __init__(self, interaction: FakeInteraction) -> None:
        self.interaction = interaction
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.done = True
        self.interaction.events.append("acknowledged")
        if thinking:
            self.interaction.thinking = ephemeral

    async def send_message(self, **message: Any) -> None:
        self.done = True
        self.interaction.record(message)


class FakeFollowup:
    def __init__(self, interaction: FakeInteraction) -> None:
        self.interaction = interaction
        self.sent = 0

    async def send(self, **message: Any) -> None:
        interaction = self.interaction
        # An app installed only to the person gets 5 follow-ups per interaction.
        assert self.sent < 5, "Discord refuses a sixth follow-up message"
        self.sent += 1
        if interaction.thinking is not None:
            # As Discord does: the first message after "thinking" replaces it
            # and keeps its visibility, whatever this message asks for.
            message = {**message, "ephemeral": interaction.thinking}
            interaction.thinking = None
        interaction.record(message)


class FakeInteraction:
    def __init__(
        self, user: int = ME, *, dm: bool = False, name: str = "drift", events=None
    ) -> None:
        self.user = SimpleNamespace(id=user, name=name)
        self.context = SimpleNamespace(dm_channel=dm)
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.events: list[str] = [] if events is None else events
        # The visibility of a "thinking" placeholder still showing.
        self.thinking: bool | None = None
        self.delete_fails = False
        self.messages: list[dict[str, Any]] = []

    def record(self, message: dict[str, Any], *, edited: bool = False) -> None:
        items = list(message["view"].walk_children()) if message.get("view") else []
        self.messages.append(
            {
                "private": bool(message.get("ephemeral")),
                "edited": edited,
                "text": "\n\n".join(
                    item.content for item in items if isinstance(item, discord.ui.TextDisplay)
                ),
                "colours": [
                    item.accent_colour for item in items if isinstance(item, discord.ui.Container)
                ],
                "shown": [
                    "picture" if isinstance(item, discord.ui.MediaGallery)
                    else "line" if isinstance(item, discord.ui.Separator)
                    else item.content
                    for item in items
                    if isinstance(
                        item, (discord.ui.TextDisplay, discord.ui.MediaGallery, discord.ui.Separator)
                    )
                ],
                "pictures": [
                    entry.media.url
                    for item in items
                    if isinstance(item, discord.ui.MediaGallery)
                    for entry in item.items
                ],
                # The files uploaded with it; an edit's replace the old ones.
                "files": {
                    file.filename: file.fp.read()
                    for file in message.get("files", message.get("attachments", []))
                },
                # An edit without a view keeps the old buttons, as on Discord.
                "view": message.get("view", "old buttons kept"),
            }
        )

    async def edit_original_response(self, **message: Any) -> None:
        self.record(message, edited=True)

    async def delete_original_response(self) -> None:
        if self.delete_fails:
            raise discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "")
        self.thinking = None

    @property
    def last(self) -> dict[str, Any]:
        return self.messages[-1]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def run_command(
    store: FakeStore, name: str, interaction: FakeInteraction, *, now=lambda: NOW, **options: Any
):
    async def go() -> None:
        app = DiscordApp(Commands(store, SITE, now=now))
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        app.register(tree)
        await tree.get_command(name).callback(interaction, **options)

    asyncio.run(go())
    return interaction.last if interaction.messages else None


def links(message: dict[str, Any]) -> dict[str, str]:
    return {
        item.label: item.url
        for item in message["view"].walk_children()
        if getattr(item, "url", None)
    }
