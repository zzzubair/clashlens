"""The Discord bot, driven through its real command definitions with a fake
Discord interaction and a fake store: no Discord connection, no database."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import psycopg
import pytest

from bot import discord_app
from bot.__main__ import ConfigError, command_payload, read_token
from bot.commands import Commands
from bot.discord_app import DiscordApp, RateLimiter
from bot.replies import Site
from clashlens.api_db import AccountContext, _screen_daily_log_with_events
from clashlens.domain import ranked_day_for

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
TODAY = ranked_day_for(NOW)
SITE = Site("https://clashlens.test")
ME = 100000000000000001
OTHER = 100000000000000002


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
    day = _screen_daily_log_with_events(
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
        "trophies": 5842,
        "season_reset_pending": False,
        "age_seconds": 120,
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
        self.pages: dict[str, dict[str, Any]] = {}
        self.fail: Exception | None = None
        self.delay = 0.0
        self.reads: list[str] = []

    def connect(self, discord_id: int, cards: list[dict[str, Any]], username="drift") -> None:
        account_id = len(self.accounts) + 1
        self.accounts[str(discord_id)] = AccountContext(account_id, "public", username, username)
        self.cards[account_id] = cards

    def _read(self, name: str) -> None:
        self.reads.append(name)
        if self.fail is not None:
            raise self.fail
        time.sleep(self.delay)

    def account(self, discord_id):
        self._read("account")
        return self.accounts.get(discord_id)

    def players(self, account):
        self._read("players")
        return self.cards[account.internal_id]

    def main_tag(self, account):
        self._read("main_tag")
        return self.mains.get(account.internal_id)

    def set_main(self, account, tag):
        self._read("set_main")
        if tag not in {item["tag"] for item in self.cards[account.internal_id]}:
            return False
        self.mains[account.internal_id] = tag
        return True

    def player_page(self, tag):
        self._read("player_page")
        return self.pages.get(tag)

    def live_rank(self, tag):
        self._read("live_rank")
        return 1234


class FakeResponse:
    def __init__(self, interaction: FakeInteraction) -> None:
        self.interaction = interaction
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, *, ephemeral: bool = False, thinking: bool = False) -> None:
        self.done = True
        self.interaction.deferred = ephemeral

    async def send_message(self, **message: Any) -> None:
        self.done = True
        self.interaction.record(message)


class FakeFollowup:
    def __init__(self, interaction: FakeInteraction) -> None:
        self.interaction = interaction

    async def send(self, **message: Any) -> None:
        self.interaction.record(message)


class FakeInteraction:
    def __init__(self, user: int = ME, *, dm: bool = False, name: str = "drift") -> None:
        self.user = SimpleNamespace(id=user, name=name)
        self.context = SimpleNamespace(dm_channel=dm)
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)
        self.deferred: bool | None = None
        self.messages: list[dict[str, Any]] = []

    def record(self, message: dict[str, Any]) -> None:
        embed = message["embed"]
        self.messages.append(
            {
                "private": bool(message.get("ephemeral")),
                "title": embed.title,
                "text": embed.description,
                "view": message.get("view"),
            }
        )

    async def edit_original_response(self, **message: Any) -> None:
        self.record(message)

    @property
    def last(self) -> dict[str, Any]:
        return self.messages[-1]


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


def run_command(store: FakeStore, name: str, interaction: FakeInteraction, **options: Any):
    async def go() -> None:
        app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
        client = discord.Client(intents=discord.Intents.none())
        tree = discord.app_commands.CommandTree(client)
        app.register(tree)
        await tree.get_command(name).callback(interaction, **options)

    asyncio.run(go())
    return interaction.last


def links(message: dict[str, Any]) -> dict[str, str]:
    return {item.label: item.url for item in message["view"].children if getattr(item, "url", None)}


def test_commands_install_for_users_and_servers_and_only_me_can_be_shared() -> None:
    commands = {command["name"]: command for command in command_payload()}
    assert set(commands) == {"help", "link", "me", "main"}
    for command in commands.values():
        # Guild, bot DM and group DM; installable to a server and to a user.
        assert command["contexts"] == [0, 1, 2]
        assert command["integration_types"] == [0, 1]
    shareable = {
        name for name, command in commands.items()
        if any(option["name"] == "share" for option in command["options"])
    }
    assert shareable == {"me"}


@pytest.mark.parametrize(
    ("dm", "share", "private"), [(False, False, True), (False, True, False), (True, False, False)]
)
def test_server_replies_are_private_unless_shared_and_dm_replies_are_normal(
    store, dm, share, private
) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    message = run_command(store, "me", FakeInteraction(dm=dm), account=None, share=share)
    assert message["title"].startswith("Drift #2PP")
    assert message["private"] is private


def test_unconnected_person_gets_only_a_private_connect_message_even_when_sharing(store) -> None:
    message = run_command(store, "me", FakeInteraction(name="drifter"), account=None, share=True)
    assert message["private"] is True
    assert "(@drifter)" in message["text"]
    assert set(links(message).values()) == {
        "https://clashlens.test/account/providers",
        "https://clashlens.test/auth/discord?returnPath=%2Faccount",
    }
    assert store.reads == ["account"]


def test_help_and_link_work_without_a_connection(store) -> None:
    help_message = run_command(store, "help", FakeInteraction())
    assert "not connected yet, use /link" in help_message["text"]
    assert help_message["private"] is True
    link_message = run_command(store, "link", FakeInteraction())
    assert "doesn't know this Discord account" in link_message["text"]

    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.mains[1] = "#8QQ"
    assert "connected as @drift, 2 verified players" in run_command(
        store, "help", FakeInteraction()
    )["text"]
    linked = run_command(store, "link", FakeInteraction())["text"]
    assert "Players: Drift #2PP, Lens #8QQ." in linked
    assert "Main: Lens." in linked


def test_connected_account_without_players_is_sent_to_verify_one(store) -> None:
    store.connect(ME, [])
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    assert "No player linked yet" in message["text"]
    assert links(message) == {"Link a player": "https://clashlens.test/account/verify-player"}


def test_database_outage_says_unavailable_never_unconnected(store) -> None:
    store.fail = psycopg.OperationalError("connection refused")
    message = run_command(store, "me", FakeInteraction(), account=None, share=True)
    assert message["text"] == "Clash Lens is unavailable right now, try again in a minute."
    assert message["private"] is True


def test_slow_read_asks_to_try_again(store, monkeypatch) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    monkeypatch.setattr(discord_app, "READ_SECONDS", 0.05)
    store.delay = 0.2
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    assert message["text"] == "Clash Lens is taking longer than usual. Try again."


def test_too_many_commands_are_refused_without_reading() -> None:
    clock = [0.0]
    limiter = RateLimiter(limit=5, window=15, clock=lambda: clock[0])
    assert [limiter.retry_after(ME) for _ in range(5)] == [0.0] * 5
    assert limiter.retry_after(ME) == 15
    assert limiter.retry_after(OTHER) == 0.0
    clock[0] = 15.0
    assert limiter.retry_after(ME) == 0.0


def test_me_lists_main_first_then_most_trophies_with_a_dropdown(store) -> None:
    store.connect(
        ME,
        [
            card("#2PP", "Drift", 5842),
            card("#8QQ", "Lens", 5100, today={"net": None, "attacks": 5, "defenses": 6}),
            card("#9RR", "Alt", None, season_reset_pending=True),
            card("#0UU", "Climber", 6001),
        ],
    )
    store.mains[1] = "#8QQ"
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    lines = [line for line in message["text"].splitlines() if line.startswith("**")]
    assert lines == [
        "**Lens** #8QQ (main) · 5,100 🏆 · #1,200 · ⚔ 5/8 · 🛡 6/8 · net pending",
        "**Climber** #0UU · 6,001 🏆 · #1,200 · ⚔ 3/8 · 🛡 2/8 · net +40 so far",
        "**Drift** #2PP · 5,842 🏆 · #1,200 · ⚔ 3/8 · 🛡 2/8 · net +40 so far",
        "**Alt** #9RR · Waiting for Season reset",
    ]
    (select,) = [item for item in message["view"].children if isinstance(item, discord.ui.Select)]
    assert [option.value for option in select.options] == ["#8QQ", "#0UU", "#2PP", "#9RR"]


def test_me_with_many_players_shows_four_then_all_on_request(store) -> None:
    store.connect(ME, [card(f"#{tag}", f"P{tag}", 5000 + index) for index, tag in enumerate("2PQRUV")])
    interaction = FakeInteraction()
    message = run_command(store, "me", interaction, account=None, share=False)
    assert message["text"].count("\n**") == 4
    (button,) = [
        item for item in message["view"].children
        if isinstance(item, discord.ui.Button) and not item.url
    ]
    assert button.label == "Show all (6)"
    asyncio.run(button.callback(interaction))
    assert interaction.last["text"].count("\n**") == 6


def test_one_player_gets_the_full_day_in_time_order(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page(
        "#2PP",
        "Drift",
        [
            battle("offense", 30, 2, 20, "Second"),
            battle("offense", 10, 3, 40, "First"),
            battle("defense", 20, 3, -40, "Raider"),
        ],
    )
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    text = message["text"]
    assert message["title"] == "Drift #2PP · Lens Clan"
    assert text.startswith(
        f"5,842 🏆 · #1,234 among tracked · Legend day {TODAY.day_number} of 28 · started today at 5,796"
    )
    assert text.index("vs First") < text.index("vs Second")
    assert "**Attacks** 2/8 · +60 · 1 three-stars" in text
    assert "**Defenses** 1/8 · −40 · 1 three-stars given up" in text
    assert "Net so far: +20" in text
    assert "automatic defense loss at Reset" in text
    assert f"<t:{int(TODAY.end.timestamp())}:R>" in text
    assert links(message) == {"Open on Clash Lens": "https://clashlens.test/players/%232PP"}


def test_full_day_holds_back_net_until_every_battle_is_recorded(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page(
        "#2PP", "Drift", [battle("offense", 10, 3, 40, "First")],
        reasons=["battle_log_overlap_gap"],
    )
    text = run_command(store, "me", FakeInteraction(), account=None, share=False)["text"]
    assert "Net pending: Clash Lens has not recorded every battle yet." in text
    assert "Net so far" not in text


def test_me_refuses_a_player_on_another_account(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.connect(OTHER, [card("#9RR", "Theirs", 6000)])
    store.pages["#9RR"] = page("#9RR", "Theirs", [])
    message = run_command(store, "me", FakeInteraction(), account="#9rr", share=False)
    assert message["text"] == "That player isn't linked to your Clash Lens account."
    assert "player_page" not in store.reads


def test_player_names_cannot_ping_or_add_formatting(store) -> None:
    store.connect(ME, [card("#2PP", "@everyone **loud**", 5842), card("#8QQ", "<@123456789012345678>", 5100)])
    text = run_command(store, "me", FakeInteraction(), account=None, share=True)["text"]
    assert "@everyone" not in text and "<@123456789012345678>" not in text
    assert r"\*\*loud\*\*" in text
    built, _tree = discord_app.build_client(Commands(store, SITE))
    mentions = built.allowed_mentions
    assert not (mentions.everyone or mentions.users or mentions.roles)


def test_main_saves_only_the_persons_own_player(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.connect(OTHER, [card("#9RR", "Theirs", 6000)])
    refused = run_command(store, "main", FakeInteraction(), account="#9RR")
    assert refused["text"] == "That player isn't linked to your Clash Lens account."
    assert store.mains == {}
    saved = run_command(store, "main", FakeInteraction(dm=True), account="lens")
    assert saved["text"].startswith("Main is now Lens #8QQ.")
    assert store.mains == {1: "#8QQ"}


def test_only_the_person_who_ran_the_command_can_use_its_dropdown(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    message = run_command(store, "me", FakeInteraction(), account=None, share=True)
    view = message["view"]
    stranger = FakeInteraction(OTHER)
    allowed = asyncio.run(view.interaction_check(stranger))
    assert allowed is False
    assert stranger.last == {
        "private": True,
        "title": None,
        "text": "Only the person who ran this command can use these buttons.",
        "view": None,
    }
    assert asyncio.run(view.interaction_check(FakeInteraction(ME))) is True


def test_autocomplete_offers_only_the_persons_own_players(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.connect(OTHER, [card("#9RR", "Drifter", 6000)])
    app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
    choices = asyncio.run(app.own_player_choices(FakeInteraction(), "dri"))
    assert [(choice.name, choice.value) for choice in choices] == [("Drift #2PP · 5,842", "#2PP")]
    assert asyncio.run(app.own_player_choices(FakeInteraction(12345), "")) == []


def test_token_file_problems_never_show_the_token(tmp_path) -> None:
    secret = tmp_path / "token"
    secret.write_text("abc def-not-a-token\n")
    with pytest.raises(ConfigError) as error:
        read_token(str(secret))
    assert "abc" not in str(error.value)
    secret.write_text("MTIz.abc.def\n")
    assert read_token(str(secret)) == "MTIz.abc.def"
