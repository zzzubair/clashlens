"""/stats through its real command
definitions, with a fake Discord interaction and a fake store."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

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


class LookupStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.known: list[dict[str, Any]] = []

    def search(self, query, now):
        self._read("search")
        return [item for item in self.known if query.casefold() in item["name"].casefold()]


@pytest.fixture
def store() -> LookupStore:
    return LookupStore()


def autocomplete(store: LookupStore, method: str, current: str) -> list[tuple[str, str]]:
    async def go():
        app = DiscordApp(Commands(store, SITE, now=lambda: NOW))
        return await getattr(app, method)(FakeInteraction(), current)

    return [(choice.name, choice.value) for choice in asyncio.run(go())]


def test_stats_finds_any_tracked_player_by_tag_or_name(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = page("#8QQ", "Theirs", [battle("offense", 10, 3, 40, "First")])
    store.known = [{"tag": "#8QQ", "name": "Theirs", "trophies": 5900}]
    by_tag = run_command(store, "stats", FakeInteraction(), player="8qq")
    assert by_tag["title"] == "Theirs #8QQ · Lens Clan"
    assert by_tag["private"] is False
    by_name = run_command(store, "stats", FakeInteraction(), player=" theirs ")
    assert by_name["title"] == "Theirs #8QQ · Lens Clan"


def test_player_by_name_keeps_the_spaces_typed(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = page("#8QQ", "Dark King", [])
    store.pages["#9RR"] = page("#9RR", "Dark  King", [])
    store.known = [
        {"tag": "#8QQ", "name": "Dark King", "trophies": 5900},
        {"tag": "#9RR", "name": "Dark  King", "trophies": 5800},
    ]
    message = run_command(store, "stats", FakeInteraction(), player=" Dark  King ")
    assert "#9RR" in message["title"]
    assert autocomplete(store, "any_player_choices", "dark  k") == [
        ("Dark  King #9RR · 5,800", "#9RR")
    ]


def test_player_explains_bad_untracked_and_unknown_players(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    bad = run_command(store, "stats", FakeInteraction(), player="#AB!")
    assert bad["text"] == "That doesn't look like a player tag."
    too_long = run_command(store, "stats", FakeInteraction(), player="#" + "2" * 16)
    assert too_long["text"] == "That doesn't look like a player tag."
    untracked = run_command(store, "stats", FakeInteraction(), player="#9RR")
    assert untracked["text"].startswith("Clash Lens hasn't tracked #9RR yet.")
    assert links(untracked) == {"Open on Clash Lens": "https://clashlens.test/players/%239RR"}
    unknown = run_command(store, "stats", FakeInteraction(), player="Nobody")
    assert unknown["text"].startswith("Clash Lens doesn't know a player called Nobody.")


def test_player_known_without_a_current_profile_gets_its_status(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.pages["#8QQ"] = {
        **card("#8QQ", "Lucy", None, reason="no_legend_battles", observed_at=None),
        "rank": None,
    }
    message = run_command(store, "stats", FakeInteraction(), player="#8QQ")
    assert message["title"] == "Lucy #8QQ"
    assert message["text"].splitlines() == ["No Legend battles yet", "", RESET]


def test_player_needs_a_connected_account(store) -> None:
    message = run_command(store, "stats", FakeInteraction(), player="#8QQ")
    assert message["private"] is True
    assert "on the website to use the bot" in message["text"]
    assert "player_page" not in store.reads


def test_player_autocomplete_offers_own_players_then_name_matches(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5842)])
    store.known = [{"tag": "#9RR", "name": "Drifter", "trophies": 6000}]
    assert autocomplete(store, "any_player_choices", "") == [("Drift #2PP · 5,842", "#2PP")]
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
    store.pages["#8QQ"] = page("#8QQ", "Lens", [])
    assert autocomplete(store, "any_player_choices", "8qq") == [
        ("Lens #8QQ · 5,842", "#8QQ"),
        ("Chasing 8QQ #0LL · 5,000", "#0LL"),
    ]


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


def test_stats_without_a_player_totals_the_default_players_season(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    store.pages["#2PP"] = season_page()
    message = run_command(store, "stats", FakeInteraction(), player=None)
    lines = message["text"].splitlines()
    assert lines[0].endswith(" · Legend day 3 of 28")
    assert lines[1:] == [
        "5,000 → 5,040 (+40)",
        "",
        "**Attacks** 2 · hit rate 50% (1 three-stars) · +30 per attack",
        "**Defenses** 2 · held 50% (1 not tripled) · −25 per defense",
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
    text = run_command(store, "stats", FakeInteraction(), player=None)["text"]
    assert text.splitlines()[1] == "Unavailable → 5,040"
    assert (
        "**Attacks** Unavailable · hit rate Unavailable (Unavailable three-stars) · "
        "Unavailable per attack"
    ) in text
    assert "**Defenses** Unavailable · held Unavailable (Unavailable not tripled)" in text


def test_season_without_any_recorded_day_shows_unavailable_not_zero(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    season = season_page()
    season["screen_ready"]["days"] = []
    season["screen_ready"]["season_day_starts"] = []
    store.pages["#2PP"] = season
    text = run_command(store, "stats", FakeInteraction(), player=None)["text"]
    assert "**Attacks** Unavailable · hit rate Unavailable (Unavailable three-stars)" in text
    assert " 0 " not in text


def test_season_history_shows_after_the_player_leaves_legend(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    store.pages["#2PP"] = {**season_page(), "state": "not_in_legend"}
    text = run_command(store, "stats", FakeInteraction(), player=None)["text"]
    assert "**Attacks** 2 · hit rate 50%" in text


def test_season_not_available_yet_still_shows_the_reset(store) -> None:
    store.connect(ME, [card("#2PP", "Drift", 5040)])
    season = season_page()
    season["screen_ready"]["season"] = None
    store.pages["#2PP"] = season
    text = run_command(store, "stats", FakeInteraction(), player=None)["text"]
    assert text.splitlines() == ["This Season's days are not available yet.", "", RESET]
