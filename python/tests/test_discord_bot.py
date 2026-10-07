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
from bot.__main__ import ConfigError, command_payload, read_token
from bot.commands import Commands
from bot.discord_app import DiscordApp
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
        "age_seconds": 120,
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

    def main_tag(self, account):
        self._read("main_tag")
        return self.mains.get(account.internal_id)

    def set_main(self, account, tag):
        self._read("set_main")
        if tag not in {item["tag"] for item in self.cards[account.internal_id]}:
            return False
        self.mains[account.internal_id] = tag
        return True

    def player_page(self, tag, now):
        self._read("player_page")
        self.moments.append(now)
        return self.pages.get(tag)

    def live_rank(self, tag, now):
        self._read("live_rank")
        self.moments.append(now)
        return 1234


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

    async def send(self, **message: Any) -> None:
        interaction = self.interaction
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
        self.messages: list[dict[str, Any]] = []

    def record(self, message: dict[str, Any], *, edited: bool = False) -> None:
        embed = message["embed"]
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
    return interaction.last


def texts(interaction: FakeInteraction) -> str:
    return "\n".join(message["text"] for message in interaction.messages)


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
    interaction = FakeInteraction(name="drifter")
    message = run_command(store, "me", interaction, account=None, share=True)
    assert len(interaction.messages) == 1
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
    assert "• Drift #2PP\n• Lens #8QQ" in linked
    assert "Main: Lens." in linked


def test_connected_account_without_players_is_sent_to_verify_one(store) -> None:
    store.connect(ME, [])
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    assert "No player linked yet" in message["text"]
    assert links(message) == {"Link a player": "https://clashlens.test/account/verify-player"}


@pytest.mark.parametrize(("dm", "private"), [(False, True), (True, False)])
def test_database_outage_says_unavailable_never_unconnected(store, dm, private) -> None:
    # The pool's error when no connection comes within its wait.
    store.fail = psycopg_pool.PoolTimeout("couldn't get a connection after 5.00 sec")
    interaction = FakeInteraction(dm=dm, events=store.reads)
    message = run_command(store, "me", interaction, account=None, share=True)
    assert store.reads == ["acknowledged", "account"]
    assert message["text"] == "Clash Lens is unavailable right now, try again in a minute."
    assert message["private"] is private


def test_slow_read_asks_to_try_again(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.fail = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    assert message["text"] == "Clash Lens is taking longer than usual. Try again."


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
    assert message["footer"].startswith("Updated 2 min ago · ")


def test_me_shows_every_player_across_messages_when_one_is_not_enough(store) -> None:
    tags = [f"#{index:04d}" for index in range(60)]
    store.connect(ME, [card(tag, "Wanderer", 5000 + index) for index, tag in enumerate(tags)])
    interaction = FakeInteraction()
    message = run_command(store, "me", interaction, account=None, share=False)
    (button,) = [
        item for item in message["view"].children
        if isinstance(item, discord.ui.Button) and not item.url
    ]
    asyncio.run(button.callback(interaction))
    shown = interaction.messages[1:]
    assert len(shown) > 1
    assert all(len(item["text"]) <= 4096 for item in shown)
    assert all(f"** {tag} " in texts(interaction) for tag in tags)


def test_link_lists_every_player_across_messages(store) -> None:
    tags = [f"#{index:04d}" for index in range(500)]
    store.connect(ME, [card(tag, "Wanderer", 5000) for tag in tags])
    interaction = FakeInteraction()
    run_command(store, "link", interaction)
    assert len(interaction.messages) > 1
    assert all(item["private"] for item in interaction.messages)
    assert all(len(item["text"]) <= 4096 for item in interaction.messages)
    assert all(f"Wanderer {tag}\n" in texts(interaction) + "\n" for tag in tags)


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


def test_full_day_without_a_published_day_says_what_is_unavailable(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    store.pages["#2PP"]["screen_ready"].update(days=[], current_day_start=None)
    text = run_command(store, "me", FakeInteraction(), account=None, share=False)["text"]
    assert text.startswith(
        "5,842 🏆 · #1,234 among tracked · Legend day Unavailable · start trophies Unavailable"
    )
    assert "Net pending" in text


@pytest.mark.parametrize(
    ("state", "reason", "word"),
    [("not_in_legend", None, "Not in Legend"), ("tracking", "pending", "Being checked")],
)
def test_a_player_whose_numbers_do_not_apply_gets_its_status_not_old_numbers(
    store, state, reason, word
) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842, state=state, reason=reason)])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    message = run_command(store, "me", FakeInteraction(), account=None, share=False)
    assert message["title"] == "Drift #2PP"
    assert message["text"] == word
    assert "player_page" not in store.reads


def test_full_day_reads_and_shows_one_legend_day_across_a_reset(store) -> None:
    before = TODAY.end - timedelta(seconds=1)
    clock = iter([before, TODAY.end + timedelta(seconds=1)])
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = page("#2PP", "Drift", [])
    text = run_command(
        store, "me", FakeInteraction(), now=lambda: next(clock), account=None, share=False
    )["text"]
    assert set(store.moments) == {before}
    assert f"<t:{int(TODAY.end.timestamp())}:R>" in text


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
    saved = run_command(store, "main", FakeInteraction(dm=True), account="8qq")
    assert saved["text"].startswith("Main is now Lens #8QQ.")
    assert store.mains == {1: "#8QQ"}


def test_only_the_person_who_ran_the_command_can_use_its_dropdown(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    message = run_command(store, "me", FakeInteraction(), account=None, share=True)
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


def test_picking_a_main_removes_the_dropdown(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    message = run_command(store, "main", FakeInteraction(), account=None)
    click = FakeInteraction()
    pick(message, click, "#8QQ")
    assert click.last["edited"] is True
    assert click.last["text"].startswith("Main is now Lens #8QQ.")
    assert click.last["view"] is None


def test_a_shared_reply_never_turns_into_a_connect_message(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    message = run_command(store, "me", FakeInteraction(), account=None, share=True)
    assert message["private"] is False
    del store.accounts[str(ME)]
    click = FakeInteraction()
    pick(message, click, "#8QQ")
    assert [item["edited"] for item in click.messages] == [False]
    assert click.last["private"] is True
    assert click.last["title"] == "Connect Discord to Clash Lens"


def test_autocomplete_offers_only_the_persons_own_players(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.connect(OTHER, [card("#9RR", "Drifter", 6000)])
    app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
    choices = asyncio.run(app.own_player_choices(FakeInteraction(), "dri"))
    assert [(choice.name, choice.value) for choice in choices] == [("Drift #2PP · 5,842", "#2PP")]
    assert asyncio.run(app.own_player_choices(FakeInteraction(12345), "")) == []
    # A player moved to another account stops being offered at once.
    store.cards[1].pop(0)
    assert asyncio.run(app.own_player_choices(FakeInteraction(), "dri")) == []


def test_token_file_problems_never_show_the_token(tmp_path) -> None:
    secret = tmp_path / "token"
    secret.write_text("abc def-not-a-token\n")
    with pytest.raises(ConfigError) as error:
        read_token(str(secret))
    assert "abc" not in str(error.value)
    secret.write_text("MTIz.abc.def\n")
    assert read_token(str(secret)) == "MTIz.abc.def"
