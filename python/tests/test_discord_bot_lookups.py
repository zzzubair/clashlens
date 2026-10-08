"""/player, /top, /rank, /group and /season through their real command
definitions, with a fake Discord interaction and a fake store."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import discord
import pytest
from test_discord_bot import (
    ME,
    NOW,
    RESET,
    SITE,
    TODAY,
    FakeInteraction,
    FakeStore,
    battle,
    card,
    links,
    page,
    run_command,
)

from bot.commands import Commands
from bot.discord_app import DiscordApp
from clashlens.api_db import _screen_daily_log_with_events
from clashlens.api_groups import GroupTooLarge

GROUP_ID = "6f1c2c1e-6c8a-4c7e-9a2e-1d0f5a1b2c3d"
OTHER_GROUP = "0b9e8a4d-3f2e-4c1b-8a7d-6e5f4c3b2a10"


def entry(position: int, tag: str, name: str, trophies: int, minutes: int = 1) -> dict[str, Any]:
    return {
        "position": position,
        "tag": tag,
        "name": name,
        "trophies": trophies,
        "observed_at": (NOW - timedelta(minutes=minutes)).isoformat(),
    }


class LookupStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.known: list[dict[str, Any]] = []
        self.saved_players: dict[int, list[dict[str, Any]]] = {}
        self.boards: dict[str | None, dict[str, Any]] = {}
        self.group_lists: dict[int, list[dict[str, Any]]] = {}
        self.comparisons: dict[tuple[int, str], dict[str, Any] | Exception] = {}

    def search(self, query, now):
        self._read("search")
        return [item for item in self.known if query.casefold() in item["name"].casefold()]

    def saved(self, account):
        self._read("saved")
        return self.saved_players.get(account.internal_id, [])

    def board(self, now, focus_tag=None):
        self._read("board")
        return self.boards.get(focus_tag)

    def groups(self, account, now):
        self._read("groups")
        return self.group_lists.get(account.internal_id, [])

    def group(self, account, group_id, days, now):
        self._read(f"group {days}")
        found = self.comparisons.get((account.internal_id, group_id))
        if isinstance(found, Exception):
            raise found
        return None if found is None else {**found, "days": days}


@pytest.fixture
def store() -> LookupStore:
    return LookupStore()


def autocomplete(store: LookupStore, method: str, current: str) -> list[tuple[str, str]]:
    async def go():
        app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
        return await getattr(app, method)(FakeInteraction(), current)

    return [(choice.name, choice.value) for choice in asyncio.run(go())]


def test_player_finds_any_tracked_player_by_tag_or_name(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = page("#8QQ", "Theirs", [battle("offense", 10, 3, 40, "First")])
    store.known = [{"tag": "#8QQ", "name": "Theirs", "trophies": 5900}]
    by_tag = run_command(store, "player", FakeInteraction(), player="8qq", share=False)
    assert by_tag["title"] == "Theirs #8QQ · Lens Clan"
    assert "**Attacks** 1/8 · +40" in by_tag["text"]
    by_name = run_command(store, "player", FakeInteraction(), player=" theirs ", share=False)
    assert by_name["title"] == "Theirs #8QQ · Lens Clan"


def test_player_by_name_keeps_the_spaces_typed(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = page("#8QQ", "Dark King", [])
    store.pages["#9RR"] = page("#9RR", "Dark  King", [])
    store.known = [
        {"tag": "#8QQ", "name": "Dark King", "trophies": 5900},
        {"tag": "#9RR", "name": "Dark  King", "trophies": 5800},
    ]
    message = run_command(store, "player", FakeInteraction(), player=" Dark  King ", share=False)
    assert "#9RR" in message["title"]
    assert autocomplete(store, "any_player_choices", "dark  k") == [
        ("Dark  King #9RR · 5,800", "#9RR")
    ]


def test_player_explains_bad_untracked_and_unknown_players(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    bad = run_command(store, "player", FakeInteraction(), player="#AB!", share=False)
    assert bad["text"] == "That doesn't look like a player tag."
    too_long = run_command(store, "player", FakeInteraction(), player="#" + "2" * 16, share=False)
    assert too_long["text"] == "That doesn't look like a player tag."
    untracked = run_command(store, "player", FakeInteraction(), player="#9RR", share=False)
    assert untracked["text"].startswith("Clash Lens hasn't tracked #9RR yet.")
    assert links(untracked) == {"Open on Clash Lens": "https://clashlens.test/players/%239RR"}
    unknown = run_command(store, "player", FakeInteraction(), player="Nobody", share=False)
    assert unknown["text"].startswith("Clash Lens doesn't know a player called Nobody.")


def test_player_known_without_a_current_profile_gets_its_status(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = {
        **card("#8QQ", "Lucy", None, reason="no_legend_battles", observed_at=None),
        "rank": None,
    }
    message = run_command(store, "player", FakeInteraction(), player="#8QQ", share=False)
    assert message["title"] == "Lucy #8QQ"
    assert message["text"].splitlines() == ["No Legend battles yet", "", RESET]


def test_player_needs_a_connected_account(store) -> None:
    message = run_command(store, "player", FakeInteraction(), player="#8QQ", share=True)
    assert message["private"] is True
    assert "doesn't know this Discord account" in message["text"]
    assert "player_page" not in store.reads


def test_player_autocomplete_offers_own_and_saved_players_then_name_matches(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.saved_players[1] = [{"tag": "#8QQ", "name": "Saved One"}]
    store.known = [{"tag": "#9RR", "name": "Drifter", "trophies": 6000}]
    assert autocomplete(store, "any_player_choices", "") == [
        ("Drift #2PP · 5,842", "#2PP"),
        ("Saved One #8QQ", "#8QQ"),
    ]
    assert autocomplete(store, "any_player_choices", "dri") == [
        ("Drift #2PP · 5,842", "#2PP"),
        ("Drifter #9RR · 6,000", "#9RR"),
    ]
    assert autocomplete(store, "any_player_choices", "#9rr") == []
    store.pages["#9RR"] = page("#9RR", "Drifter", [])
    assert autocomplete(store, "any_player_choices", "9rr") == [("Drifter #9RR · 5,842", "#9RR")]
    assert autocomplete(store, "any_player_choices", "#9RR ") == [("Drifter #9RR · 5,842", "#9RR")]
    assert autocomplete(store, "any_player_choices", "drif") == [
        ("Drift #2PP · 5,842", "#2PP"),
        ("Drifter #9RR · 6,000", "#9RR"),
    ]
    store.known.append({"tag": "#0UU", "name": "Lucy", "trophies": 5500})
    assert autocomplete(store, "any_player_choices", "lucy") == [("Lucy #0UU · 5,500", "#0UU")]
    assert autocomplete(store, "any_player_choices", "l") == [("Lucy #0UU · 5,500", "#0UU")]
    store.cards[1].append(card("#0LL", "Chasing 8QQ", 5000))
    assert autocomplete(store, "any_player_choices", "8qq") == [
        ("Saved One #8QQ", "#8QQ"),
        ("Chasing 8QQ #0LL · 5,000", "#0LL"),
    ]


def test_top_lists_the_live_leaderboard_among_tracked_players(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.boards[None] = {
        "tracked_population": 13263,
        "total_entries": 12000,
        "entries": [
            entry(1, "#8QQ", "First", 6120, minutes=1),
            entry(2, "#9RR", "@everyone", 6100, minutes=7),
        ],
    }
    message = run_command(store, "top", FakeInteraction(), share=False)
    assert message["title"] == "Live Leaderboard · among 13,263 tracked players"
    assert message["text"].splitlines()[0].endswith(RESET)
    assert message["text"].splitlines()[2] == "#1 First · 6,120"
    assert "@everyone" not in message["text"]
    assert "not the official world ranking" in message["footer"]
    assert message["footer"].endswith("Updated 7 min ago")
    assert links(message) == {"Full leaderboard": "https://clashlens.test/leaderboards/tracked"}


def test_top_with_an_empty_board_still_shows_the_reset(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    message = run_command(store, "top", FakeInteraction(), share=False)
    assert message["text"].splitlines()[0] == "The Live Leaderboard is empty right now."
    assert message["text"].splitlines()[-1].endswith(RESET)


def test_rank_shows_the_main_with_the_players_around_it(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.mains[1] = "#2PP"
    store.boards["#2PP"] = {
        "tracked_population": 13263,
        "total_entries": 12000,
        "entries": [
            entry(1233, "#9RR", "Above", 5850),
            entry(1234, "#2PP", "Drift", 5842),
            entry(1235, "#0UU", "Below", 5842),
        ],
    }
    message = run_command(store, "rank", FakeInteraction(), account=None, share=False)
    assert message["title"] == "Drift #2PP · #1,234 of 12,000 tracked · 5,842"
    assert message["text"].splitlines()[0].endswith(RESET)
    assert message["text"].splitlines()[2:] == [
        "#1,233 Above · 5,850 (+8)",
        "▶ **#1,234 Drift · 5,842**",
        "#1,235 Below · 5,842 (+0)",
    ]


def test_rank_without_a_main_asks_which_player_and_answers_the_pick(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842), card("#8QQ", "Lens", 5100)])
    store.boards["#8QQ"] = {
        "tracked_population": 10,
        "total_entries": 9,
        "entries": [entry(4, "#8QQ", "Lens", 5100)],
    }
    interaction = FakeInteraction()
    asked = run_command(store, "rank", interaction, account=None, share=False)
    assert asked["text"].startswith("Which player?")
    (select,) = [item for item in asked["view"].children if isinstance(item, discord.ui.Select)]
    select._values = ["#8QQ"]  # what Discord fills in when the person picks
    asyncio.run(select.callback(interaction))
    assert interaction.last["title"] == "Lens #8QQ · #4 of 9 tracked · 5,100"


def test_rank_for_a_player_off_the_board_says_why(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", None, season_reset_pending=True)])
    message = run_command(store, "rank", FakeInteraction(), account=None, share=False)
    assert message["text"].splitlines() == ["Waiting for Season reset", "", RESET]


def comparison(players: list[dict[str, Any]]) -> dict[str, Any]:
    return {"group_id": GROUP_ID, "name": "Night Crew", "players": players}


def member(tag: str, name: str, trophies: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "tag": tag,
        "name": name,
        "you": False,
        "in_group": True,
        "status": "tracking",
        "trophies": trophies,
        "season_reset_pending": False,
        "observed_at": (NOW - timedelta(minutes=3)).isoformat(),
        "today": {"net": 20, "attacks": 4, "defenses": 3},
        "net": 212,
        "counted_days": 14,
        **extra,
    }


def test_group_lists_the_persons_groups_and_compares_one(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.group_lists[1] = [{"group_id": GROUP_ID, "name": "Night Crew", "tags": ["#2PP", "#8QQ"]}]
    store.comparisons[(1, GROUP_ID)] = comparison(
        [
            member("#8QQ", "Friend", 5900, net=None),
            member("#2PP", "Drift", 5842, you=True),
            member("#0UU", "New", 5000, net=100, counted_days=2),
            member("#3YY", "Left", None, status="not_in_legend", net=-40),
            member("#7LL", "Checking", None, net=None),
            member("#9RR", "Not in it", 6500, in_group=False),
        ]
    )
    listed = run_command(store, "group", FakeInteraction(), group=None, days=7, share=False)
    assert "**Night Crew** · 2 players" in listed["text"]
    compared = run_command(
        store, "group", FakeInteraction(), group=GROUP_ID, days=14, share=False
    )
    assert compared["title"] == "Night Crew · last 14 ended Legend days"
    lines = [line for line in compared["text"].splitlines() if line.startswith("**")]
    assert lines == [
        "**Friend** #8QQ · 5,900 🏆 · ⚔ 4/8 · 🛡 3/8 · net +20 so far · 14 days: pending",
        "**Drift** #2PP (you) · 5,842 🏆 · ⚔ 4/8 · 🛡 3/8 · net +20 so far · 14 days: +212",
        (
            "**New** #0UU · 5,000 🏆 · ⚔ 4/8 · 🛡 3/8 · net +20 so far"
            " · 14 days: +100 (2 of 14 days counted)"
        ),
        "**Left** #3YY · Not in Legend · 14 days: −40",
        "**Checking** #7LL · Being checked · 14 days: pending",
    ]
    assert "group 14" in store.reads
    by_name = run_command(
        store, "group", FakeInteraction(), group="Night Crew", days=7, share=False
    )
    assert by_name["text"] == "That group isn't on your Clash Lens account."


def test_group_picked_from_the_list_keeps_the_chosen_days(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.group_lists[1] = [{"group_id": GROUP_ID, "name": "Night Crew", "tags": ["#2PP"]}]
    store.comparisons[(1, GROUP_ID)] = comparison([member("#2PP", "Drift", 5842, you=True)])
    interaction = FakeInteraction()
    listed = run_command(store, "group", interaction, group=None, days=3, share=False)
    (select,) = [item for item in listed["view"].children if isinstance(item, discord.ui.Select)]
    select._values = [GROUP_ID]  # what Discord fills in when the person picks
    asyncio.run(select.callback(interaction))
    assert interaction.last["title"] == "Night Crew · last 3 ended Legend days"
    assert "group 3" in store.reads


def test_group_of_another_account_or_too_large_is_refused(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.group_lists[1] = [{"group_id": GROUP_ID, "name": "Big", "tags": []}]
    store.comparisons[(1, GROUP_ID)] = GroupTooLarge(30)
    theirs = run_command(store, "group", FakeInteraction(), group=OTHER_GROUP, days=7, share=False)
    assert theirs["text"] == "That group isn't on your Clash Lens account."
    large = run_command(store, "group", FakeInteraction(), group=GROUP_ID, days=7, share=False)
    assert large["text"] == "This group is too large to compare here."


def test_group_autocomplete_offers_only_the_persons_groups(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.group_lists[1] = [{"group_id": GROUP_ID, "name": "Night Crew", "tags": []}]
    store.group_lists[2] = [{"group_id": OTHER_GROUP, "name": "Night Owls", "tags": []}]
    assert autocomplete(store, "own_group_choices", "night") == [("Night Crew", GROUP_ID)]


def season_page() -> dict[str, Any]:
    start = TODAY.start - timedelta(days=2)
    days = []
    for number, battles, net, rank in (
        (1, [battle("offense", 10, 3, 40, "A"), battle("defense", 20, 1, -10, "B")], 30, 1500),
        (2, [battle("offense", 30, 2, 20, "C")], 20, 1300),
        (3, [battle("defense", 40, 3, -40, "D")], None, None),
    ):
        moment = start + timedelta(days=number - 1)
        attacks = [item for item in battles if item["lens"] == "offense"]
        defenses = [item for item in battles if item["lens"] == "defense"]
        day = _screen_daily_log_with_events(
            {
                "ranked_day_start": moment.isoformat(),
                "ranked_day_end": (moment + timedelta(days=1)).isoformat(),
                "official_season_id": "1",
                "season_day_number": number,
                "version": 1,
                "state": "Complete" if number < 3 else "Live",
                "coverage": "complete",
                "confidence": "high",
                "attack_count": len(attacks),
                "attack_three_star_count": sum(item["stars"] == 3 for item in attacks),
                "attack_gain": sum(item["trophy_change"] for item in attacks),
                "defense_count": len(defenses),
                "defense_three_star_count": sum(item["stars"] == 3 for item in defenses),
                "defense_loss": -sum(item["trophy_change"] for item in defenses),
                "net_trophy_change": net,
                "adjustments": [],
                "battles": battles,
                "partial_reasons": [],
                "start_trophies": 5000 if number == 1 else None,
                "start_trophies_source": None,
            },
            "high",
            NOW,
        )
        day["reset_rank"] = rank
        days.append(day)
    result = page("#2PP", "Drift", [])
    result["trophies"] = 5040
    result["screen_ready"] = {
        "days": days,
        "current_day_start": days[-1]["ranked_day_start"],
        "season_day_starts": [day["ranked_day_start"] for day in days],
        "season": {
            "id": "1786000000",
            "current_day_number": 3,
            "start": start.isoformat(),
            "end": (start + timedelta(days=28)).isoformat(),
        },
        "data_quality": [],
    }
    return result


def test_season_totals_the_seasons_recorded_battles(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    store.pages["#2PP"] = season_page()
    message = run_command(store, "season", FakeInteraction(), account=None, share=False)
    text = message["text"]
    assert "Day 3 of 28 · 5,000 → 5,040 (+40)" in text
    assert (
        "**Attacks** 2 · hit rate 50% (1 three-stars) · +60 · +30 per attack"
        " · average destruction 85.5%"
    ) in text
    assert (
        "**Defenses** 2 · held 1 of 2 (not tripled) · −50 · −25 per defense"
        " · average stars given up 2.0"
    ) in text
    assert text.splitlines()[-4:] == [
        "Day 2 · net +20 · Reset rank #1,300",
        "Day 1 · net +30 · Reset rank #1,500",
        "",
        RESET,
    ]
    assert links(message) == {
        "Season on Clash Lens": "https://clashlens.test/players/%232PP"
    }


def test_season_keeps_missing_days_and_numbers_visible(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    season = season_page()
    ready = season["screen_ready"]
    # Day 2 was never published and Day 3 lost its defense totals.
    del ready["days"][1]
    ready["season_day_starts"] = [day["ranked_day_start"] for day in ready["days"]]
    ready["days"][-1]["defense_count"] = None
    ready["days"][-1]["defense_loss"] = None
    ready["days"][0]["start_trophies"] = None
    store.pages["#2PP"] = season
    text = run_command(store, "season", FakeInteraction(), account=None, share=False)["text"]
    assert "Day 3 of 28 · Unavailable → 5,040" in text
    assert "(+" not in text.splitlines()[0]
    assert "**Defenses** Unavailable · held Unavailable of Unavailable (not tripled)" in text
    assert text.splitlines()[-4:-2] == [
        "Day 2 · net pending · Reset rank Unavailable",
        "Day 1 · net +30 · Reset rank #1,500",
    ]


def test_season_without_any_recorded_day_shows_unavailable_not_zero(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    season = season_page()
    season["screen_ready"]["days"] = []
    season["screen_ready"]["season_day_starts"] = []
    store.pages["#2PP"] = season
    text = run_command(store, "season", FakeInteraction(), account=None, share=False)["text"]
    assert "**Attacks** Unavailable · hit rate Unavailable (Unavailable three-stars)" in text
    assert " 0 " not in text


def test_season_history_shows_after_the_player_leaves_legend(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    store.pages["#2PP"] = {**season_page(), "state": "not_in_legend"}
    text = run_command(store, "season", FakeInteraction(), account=None, share=False)["text"]
    assert "**Attacks** 2 · hit rate 50%" in text


def test_season_not_available_yet_still_shows_the_reset(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    season = season_page()
    season["screen_ready"]["season"] = None
    store.pages["#2PP"] = season
    text = run_command(store, "season", FakeInteraction(), account=None, share=False)["text"]
    assert text.splitlines() == ["This Season's days are not available yet.", "", RESET]
