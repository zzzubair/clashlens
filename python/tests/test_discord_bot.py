"""The Discord bot, driven through its real command definitions with a fake
Discord interaction and a fake store: no Discord connection, no database."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import discord
import psycopg
import psycopg_pool
import pytest

from bot import discord_app
from bot.__main__ import ConfigError, command_payload, main, read_token
from bot.commands import Commands
from bot.discord_app import DiscordApp
from bot.replies import Site
from clashlens.api_db import AccountContext, _screen_daily_log_with_events
from clashlens.domain import ranked_day_for

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
TODAY = ranked_day_for(NOW)
RESET = f"Reset <t:{int(TODAY.end.timestamp())}:t> (<t:{int(TODAY.end.timestamp())}:R>)"
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
        embed = message.get("embed", discord.Embed())
        self.messages.append(
            {
                "private": bool(message.get("ephemeral")),
                "edited": edited,
                "title": embed.title,
                "text": embed.description,
                "footer": embed.footer.text,
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
    return {item.label: item.url for item in message["view"].children if getattr(item, "url", None)}


def test_commands_install_for_users_and_servers_with_no_share_option() -> None:
    commands = {command["name"]: command for command in command_payload()}
    assert set(commands) == {"help", "link", "stats"}
    for command in commands.values():
        # Guild, bot DM and group DM; installable to a server and to a user.
        assert command["contexts"] == [0, 1, 2]
        assert command["integration_types"] == [0, 1]
        assert all(option["name"] != "share" for option in command["options"])


@pytest.mark.parametrize("dm", [False, True])
def test_replies_are_normal_messages_anyone_can_forward(store, dm) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    message = run_command(store, "stats", FakeInteraction(dm=dm), player=None)
    assert message["title"].startswith("Drift #2PP")
    assert message["private"] is False


def test_unconnected_person_gets_only_a_private_connect_pointer(store) -> None:
    interaction = FakeInteraction(name="drifter")
    message = run_command(store, "stats", interaction, player=None)
    assert len(interaction.messages) == 1
    assert message["private"] is True
    assert "(@drifter)" in message["text"]
    assert set(links(message).values()) == {
        "https://clashlens.test/account/discord-alerts",
        "https://clashlens.test/auth/discord?returnPath=%2Faccount",
    }
    assert store.reads == ["account"]


def test_a_private_reply_is_dropped_when_the_public_placeholder_cannot_be_removed(store) -> None:
    interaction = FakeInteraction()
    interaction.delete_fails = True
    assert run_command(store, "stats", interaction, player=None) is None


def test_help_and_link_work_without_a_connection(store) -> None:
    help_message = run_command(store, "help", FakeInteraction())
    assert "not connected yet, use /link" in help_message["text"]
    assert help_message["private"] is False
    link_message = run_command(store, "link", FakeInteraction())
    assert "on the website to use the bot" in link_message["text"]
    assert link_message["private"] is True
    # Connecting happens on the website: the bot only points there.
    assert links(link_message)["Connect Discord on Clash Lens"] == (
        "https://clashlens.test/account/discord-alerts"
    )

    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.mains[1] = "#8QQ"
    assert "connected as @drift, 2 verified players" in run_command(
        store, "help", FakeInteraction()
    )["text"]
    # Connected: still only a pointer to the website, not the players.
    linked = run_command(store, "link", FakeInteraction())
    assert linked["text"].startswith("Connected as @drift.")
    assert "#2PP" not in linked["text"] and "#8QQ" not in linked["text"]
    assert linked["private"] is False
    assert links(linked) == {
        "Discord alerts on Clash Lens": "https://clashlens.test/account/discord-alerts"
    }


def test_connected_account_without_players_is_pointed_to_the_website(store) -> None:
    store.connect(ME, [])
    message = run_command(store, "stats", FakeInteraction(), player=None)
    assert "Link a Clash of Clans player on Clash Lens" in message["text"]
    assert "API token" not in message["text"]
    assert links(message) == {
        "Link a player on Clash Lens": "https://clashlens.test/account/verify-player"
    }


@pytest.mark.parametrize(("dm", "private"), [(False, True), (True, False)])
def test_database_outage_says_unavailable_never_unconnected(store, dm, private) -> None:
    # The pool's error when no connection comes within its wait.
    store.fail = psycopg_pool.PoolTimeout("couldn't get a connection after 5.00 sec")
    interaction = FakeInteraction(dm=dm, events=store.reads)
    message = run_command(store, "stats", interaction, player=None)
    assert store.reads == ["acknowledged", "account"]
    assert message["text"] == "Clash Lens is unavailable right now, try again in a minute."
    assert message["private"] is private


def test_slow_read_asks_to_try_again(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.fail = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    message = run_command(store, "stats", FakeInteraction(), player=None)
    assert message["text"] == "Clash Lens is taking longer than usual. Try again."


def test_every_reply_ends_with_how_old_its_data_is(store) -> None:
    store.connect(
        ME,
        [
            card("#2PP", "Drift", 5842, observed_at=NOW - timedelta(hours=3)),
            card("#8QQ", "Lens", 5100, observed_at=None),
        ],
    )
    # The player menu, as when the default read finds no player.
    store.no_default.add(1)
    footer = run_command(store, "stats", FakeInteraction(), player=None)["footer"]
    assert footer.endswith("Updated 180 min ago")
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    footer = run_command(store, "stats", FakeInteraction(), player="#8QQ")["footer"]
    assert footer.endswith("Updated 2 min ago")
    assert run_command(store, "help", FakeInteraction())["footer"] == "Updated 0 min ago"
    store.fail = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    footer = run_command(store, "stats", FakeInteraction(), player=None)["footer"]
    assert footer == "Updated: Unavailable"


@pytest.mark.parametrize(
    ("state", "reason", "pending", "word"),
    [
        ("not_in_legend", None, False, "Not in Legend"),
        ("tracking", "pending", False, "Being checked"),
        # Left Legend I with last Season's profile: still Not in Legend.
        ("not_in_legend", None, True, "Not in Legend"),
        ("uncertain", None, True, "Being checked"),
    ],
)
def test_a_player_whose_numbers_do_not_apply_gets_its_status_not_old_numbers(
    store, state, reason, pending, word
) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = {
        **page("#2PP", "Drift", []),
        "state": state,
        "reason": reason,
        "season_reset_pending": pending,
    }
    message = run_command(store, "stats", FakeInteraction(), player=None)
    assert message["title"] == "Drift #2PP · Lens Clan"
    assert message["text"].splitlines() == [word, "", RESET]


def test_player_names_cannot_ping_or_add_formatting(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = {**page("#2PP", "@everyone **loud**", []), "clan": "<@123456789012345678>"}
    title = run_command(store, "stats", FakeInteraction(), player=None)["title"]
    assert "@everyone" not in title and "<@123456789012345678>" not in title
    assert r"\*\*loud\*\*" in title
    built, _tree = discord_app.build_client(Commands(store, SITE))
    mentions = built.allowed_mentions
    assert not (mentions.everyone or mentions.users or mentions.roles)


def test_a_default_player_no_longer_verified_gives_way_to_the_first(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.mains[1] = "#8QQ"
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    first = run_command(store, "stats", FakeInteraction(), player=None)
    assert first["title"].startswith("Lens #8QQ")
    store.cards[1].pop()
    store.cards[1].append(card("#9RR", "New", 5000))
    after = run_command(store, "stats", FakeInteraction(), player=None)
    assert after["title"].startswith("Drift #2PP")


def test_only_the_person_who_ran_the_command_can_use_its_dropdown(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.no_default.add(1)
    message = run_command(store, "stats", FakeInteraction(), player=None)
    view = message["view"]
    stranger = FakeInteraction(OTHER)
    allowed = asyncio.run(view.interaction_check(stranger))
    assert allowed is False
    assert stranger.last["private"] is True
    assert stranger.last["text"] == "Only the person who ran this command can use these buttons."
    assert asyncio.run(view.interaction_check(FakeInteraction(ME))) is True


def pick(message: dict[str, Any], interaction: FakeInteraction, value: str) -> None:
    (select,) = [item for item in message["view"].children if isinstance(item, discord.ui.Select)]
    select._values = [value]
    asyncio.run(select.callback(interaction))


def test_a_public_reply_never_turns_into_a_connect_message(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.no_default.add(1)
    message = run_command(store, "stats", FakeInteraction(), player=None)
    assert message["private"] is False
    del store.accounts[str(ME)]
    click = FakeInteraction()
    pick(message, click, "#8QQ")
    assert [item["edited"] for item in click.messages] == [False]
    assert click.last["private"] is True
    assert click.last["title"] == "Connect Discord to Clash Lens"


def test_autocomplete_that_ran_out_of_time_waiting_never_reads(store, monkeypatch) -> None:
    monkeypatch.setattr(discord_app, "AUTOCOMPLETE_SECONDS", 0.05)
    store.connect(ME, [card("#2PP", "Drift", 5842)])

    async def go() -> None:
        app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
        # Every read place busy with other work.
        for _ in range(discord_app.READ_SLOTS):
            await app._slots.acquire()
        assert await app.any_player_choices(FakeInteraction(), "") == []
        for _ in range(discord_app.READ_SLOTS):
            app._slots.release()
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert store.reads == []


def test_token_file_problems_never_show_the_token(tmp_path) -> None:
    secret = tmp_path / "token"
    secret.write_text("abc def-not-a-token\n")
    with pytest.raises(ConfigError) as error:
        read_token(str(secret))
    assert "abc" not in str(error.value)
    secret.write_text("MTIz.abc.def\n")
    assert read_token(str(secret)) == "MTIz.abc.def"


def test_run_stops_at_startup_without_a_website_address(tmp_path, monkeypatch, capsys) -> None:
    # The service passes the setting through even when app.env leaves it empty.
    monkeypatch.setenv("CLASHLENS_PUBLIC_ORIGIN", "")
    monkeypatch.setenv("CLASHLENS_DISCORD_BOT_TOKEN_FILE", str(tmp_path / "token"))
    monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(tmp_path / "database-url"))
    assert main(["run"]) == 2
    assert capsys.readouterr().err == "bot: missing settings: CLASHLENS_PUBLIC_ORIGIN\n"
