"""The Discord bot, driven through its real command definitions with a fake
Discord interaction and a fake store: no Discord connection, no database."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import discord
import psycopg
import psycopg_pool
import pytest
from discord_alerts_support import DARK, LIGHT, background
from discord_bot_support import (
    ME,
    NOW,
    RESET,
    SITE,
    TODAY,
    FakeInteraction,
    battle,
    card,
    links,
    page,
    run_command,
)
from PIL import Image

from bot import discord_app
from bot.__main__ import ConfigError, main, read_token
from bot.commands import Commands
from bot.discord_app import DiscordApp, MenuState, build_client
from bot.replies import GOLD, PLAYER_COLOURS

OTHER = 100000000000000002


def test_commands_install_for_users_and_servers_with_no_share_option(store) -> None:
    _client, tree = build_client(Commands(store, SITE))
    commands = {command.name: command.to_dict(tree) for command in tree.get_commands()}
    assert set(commands) == {"help", "link", "alerts", "stats"}
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
    assert message["text"].startswith("### Drift #2PP")
    assert message["private"] is False


def test_stats_works_for_anyone_and_never_asks_to_sign_up(store) -> None:
    interaction = FakeInteraction(name="drifter")
    message = run_command(store, "stats", interaction, player=None)
    assert len(interaction.messages) == 1
    assert message["private"] is True
    assert message["text"] == (
        "Type a player tag or name after /stats to look them up, like "
        "`/stats #2PP22` or `/stats Wanderer`."
    )
    assert links(message) == {}
    assert store.reads == ["account"]
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    named = run_command(store, "stats", FakeInteraction(name="drifter"), player="#8QQ")
    assert named["text"].startswith("### Lens #8QQ")
    assert named["private"] is False
    assert named["colours"] == [GOLD]
    assert links(named) == {}


def test_stats_shows_todays_picture_between_its_trophies_and_the_day_link(store) -> None:
    """Today's trophies, then the picture of today's Legend day, uploaded
    with the message, then a link opening that day on the website, then the
    Season. The picture is dark only for someone whose own account chose
    it."""
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = page(
        "#8QQ", "Lens", [battle("offense", 5, 3, 40, "A"), battle("defense", 9, 2, -24, "B")]
    )
    shown = {}
    for who, user in (("stranger", OTHER), ("light", ME), ("dark", ME)):
        if who == "dark":
            store.dark.add(1)
        shown[who] = run_command(store, "stats", FakeInteraction(user), player="#8QQ")
    message = shown["stranger"]
    day = TODAY.start.date().isoformat()
    assert message["shown"] == [
        "### Lens #8QQ\n## +40 (1)  −24 (1)",
        "picture",
        f"[View day](https://clashlens.test/players/%238QQ?day={day}#legend-day-{day})",
        "line",
        "**Season Stats**\nThis Season's days are not available yet.\n-# Updated 2 min ago",
    ]
    assert message["pictures"] == ["attachment://legend-day.png"]
    assert {who: background(item["files"]["legend-day.png"]) for who, item in shown.items()} == {
        "stranger": LIGHT, "light": LIGHT, "dark": DARK
    }
    assert Image.open(io.BytesIO(message["files"]["legend-day.png"])).size == (1200, 860)


def test_a_day_not_saved_yet_has_no_picture(store) -> None:
    store.pages["#8QQ"] = {**page("#8QQ", "Lens", []), "screen_ready": {"days": []}}
    message = run_command(store, "stats", FakeInteraction(), player="#8QQ")
    assert message["shown"][:2] == ["### Lens #8QQ\nToday's battles are not available yet", "line"]
    assert message["pictures"] == [] and message["files"] == {}


def test_own_linked_accounts_keep_their_colour_and_other_players_are_yellow(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    for tag in ("#2PP", "#8QQ", "#9RR"):
        store.pages[tag] = page(tag, "Someone", [])
    shown = {
        tag: run_command(store, "stats", FakeInteraction(), player=tag)["colours"]
        for tag in ("#2PP", "#8QQ", "#9RR")
    }
    assert shown == {
        "#2PP": [PLAYER_COLOURS[0]],
        "#8QQ": [PLAYER_COLOURS[1]],
        "#9RR": [GOLD],
    }
    assert GOLD not in PLAYER_COLOURS


def test_a_private_reply_is_dropped_when_the_public_placeholder_cannot_be_removed(store) -> None:
    interaction = FakeInteraction()
    interaction.delete_fails = True
    assert run_command(store, "stats", interaction, player=None) is None


CONNECT = "[Connect Discord](https://clashlens.test/account/settings)"


def test_help_link_and_alerts_offer_sign_up_without_a_connection(store) -> None:
    help_message = run_command(store, "help", FakeInteraction())
    assert "this Discord account isn't connected to Clash Lens yet" in help_message["text"]
    assert help_message["private"] is False
    assert links(help_message) == {"Get started on Clash Lens": "https://clashlens.test/signup"}
    assert CONNECT in help_message["text"]
    for name, purpose in (
        ("link", "to see your own Legend days here"),
        ("alerts", "to get attack alerts and Legend day summaries for it in your Discord DMs"),
    ):
        message = run_command(store, name, FakeInteraction(name="drifter"))
        assert message["private"] is True
        assert "Sign up with this Discord account (@drifter)" in message["text"]
        assert purpose in message["text"]
        # Signing up and connecting happen on the website: the bot only points there.
        assert links(message) == {
            "Sign up with Discord": (
                "https://clashlens.test/auth/discord?returnPath=%2Faccount%2Fverify-player"
            )
        }
        # Someone with a Clash Lens account already connects instead.
        assert CONNECT in message["text"]


def test_help_link_and_alerts_show_a_connected_accounts_settings(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.mains[1] = "#8QQ"
    help_message = run_command(store, "help", FakeInteraction())
    assert "connected as @drift, 2 linked accounts" in help_message["text"]
    assert links(help_message) == {"Open Clash Lens": "https://clashlens.test/"}
    assert CONNECT not in help_message["text"]
    # The default account is chosen in the Clasher's Settings.
    assert (
        "choose your default account in "
        "[your Clash Lens settings](https://clashlens.test/account/settings)"
    ) in help_message["text"]
    linked = run_command(store, "link", FakeInteraction())
    assert linked["private"] is False
    assert "**Drift** #2PP\n**Lens** #8QQ · default account" in linked["text"]
    assert linked["colours"] == [GOLD]
    assert links(linked) == {"Change linked accounts": "https://clashlens.test/account/settings"}
    alerts = run_command(store, "alerts", FakeInteraction())
    assert alerts["private"] is False
    assert (
        "**Drift** #2PP\n-# Attack alerts **On** · Legend day summary Off\n"
        "**Lens** #8QQ\n-# Attack alerts Off · Legend day summary Off"
    ) in alerts["text"]
    assert alerts["colours"] == [GOLD]
    assert links(alerts) == {"Change settings": "https://clashlens.test/account/discord-alerts"}


def test_connected_account_without_players_is_asked_for_a_player_on_stats(store) -> None:
    store.connect(ME, [])
    message = run_command(store, "stats", FakeInteraction(), player=None)
    # /stats never asks to link or use Clash Lens, connected or not.
    assert message["text"].startswith("Type a player tag or name after /stats")
    assert links(message) == {}
    linked = run_command(store, "link", FakeInteraction())
    assert "Link a Clash of Clans account on Clash Lens" in linked["text"]
    assert links(linked) == {
        "Link an account on Clash Lens": "https://clashlens.test/account/verify-player"
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


def test_only_stats_says_how_old_its_numbers_are(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    stats = run_command(store, "stats", FakeInteraction(), player="#8QQ")["text"]
    assert stats.endswith("-# Updated 2 min ago")
    # The player menu, as when the default read finds no player.
    store.no_default.add(1)
    for message in (
        run_command(store, "stats", FakeInteraction(), player=None),
        run_command(store, "help", FakeInteraction()),
        run_command(store, "link", FakeInteraction()),
    ):
        assert "Updated" not in message["text"]


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
    assert message["text"].splitlines() == [
        "### Drift #2PP", "-# Lens Clan", "", word, "", RESET, "-# Updated 2 min ago"
    ]


def test_player_names_cannot_ping_or_add_formatting(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#2PP"] = {**page("#2PP", "@everyone **loud**", []), "clan": "<@123456789012345678>"}
    title = run_command(store, "stats", FakeInteraction(), player=None)["text"].split("\n")[0]
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
    assert first["text"].startswith("### Lens #8QQ")
    store.cards[1].pop()
    store.cards[1].append(card("#9RR", "New", 5000))
    after = run_command(store, "stats", FakeInteraction(), player=None)
    assert after["text"].startswith("### Drift #2PP")


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
    (select,) = [
        item for item in message["view"].walk_children() if isinstance(item, discord.ui.Select)
    ]
    select._values = [value]
    asyncio.run(select.callback(interaction))


def test_a_player_picked_from_the_dropdown_gets_their_picture(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.no_default.add(1)
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    message = run_command(store, "stats", FakeInteraction(), player=None)
    assert message["files"] == {}
    click = FakeInteraction()
    pick(message, click, "#8QQ")
    assert click.last["edited"] is True
    assert click.last["shown"][:2] == ["### Lens #8QQ\nNo battles yet today", "picture"]
    assert click.last["pictures"] == ["attachment://legend-day.png"]
    assert background(click.last["files"]["legend-day.png"]) == LIGHT


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
    # /stats never asks to sign up, link or connect, even from its menu.
    assert click.last["text"] == (
        "Type a player tag or name after /stats to look them up, like "
        "`/stats #2PP22` or `/stats Wanderer`."
    )
    assert links(click.last) == {}


def test_autocomplete_that_ran_out_of_time_waiting_never_reads(store, monkeypatch) -> None:
    monkeypatch.setattr(discord_app, "AUTOCOMPLETE_SECONDS", 0.05)
    store.connect(ME, [card("#2PP", "Drift", 5842)])

    async def go() -> None:
        app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
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


def test_a_menu_closes_after_one_pick_or_when_its_timer_runs_out(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.no_default.add(1)
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    message = run_command(store, "stats", FakeInteraction(), player=None)
    menu = message["view"]
    assert menu.state is MenuState.OPEN
    first = FakeInteraction()
    pick(message, first, "#8QQ")
    assert menu.state is MenuState.CLOSED
    # The new reply has only link buttons, which never expire.
    assert first.last["view"].state is MenuState.LINKS_ONLY
    # A second pick on the replaced menu changes nothing.
    second = FakeInteraction()
    pick(message, second, "#2PP")
    assert second.messages == []

    started = FakeInteraction()
    timed = run_command(store, "stats", started, player=None)
    menu = timed["view"]
    shown_links = links(timed)
    menu.stop()  # what discord.py does when the timer runs out
    asyncio.run(menu.on_timeout())
    assert menu.state is MenuState.CLOSED
    assert started.last["edited"] is True
    assert not any(
        isinstance(item, discord.ui.Select) for item in started.last["view"].walk_children()
    )
    assert links(started.last) == shown_links
