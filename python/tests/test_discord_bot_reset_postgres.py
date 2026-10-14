"""/stats end to end: saved game data through the real worker to published
Daily boards, then the bot's Season totals read as the API's database role.
See docs/testing.md."""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from test_discord_bot_postgres import DISCORD_ID, _account
from test_reset_scenarios_postgres import SEASON, Player, Story, attacks, day, defenses

from bot.commands import Commands
from bot.replies import Site
from bot.store import Store
from clashlens.api_db import ApiDatabase

FULL, LATE = "#2PP", "#8PP"


def test_stats_totals_every_saved_day_and_never_hides_a_missing_one(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The first two days of the Season that started on 5 October 2026, asked
    a minute before the Day 3 Reset. FULL was tracked from Day 1, so /stats
    totals both days. Clash Lens first saw LATE at the Day 3 Reset, so their
    Day 1 was never saved: their totals are Unavailable, not Day 2's alone."""
    day_1, day_2, day_3 = day(1), day(2), day(3)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Player(tag) for tag in (FULL, LATE)})
        # Day 1: 4 attacks gain 150, three of them three-stars, and 8
        # defenses lose 80: 5,000 to 5,070. Day 2: 3 attacks gain 100, two
        # three-stars, and 8 defenses lose 110, one tripled: 5,060.
        story.players[FULL].battles = (
            attacks(day_1, 40, 40, 40, 30) + defenses(day_1, *[10] * 8)
            + attacks(day_2, 40, 40, 20) + defenses(day_2, 40, *[10] * 7)
        )
        # LATE sat out Day 1, then played Day 2: 5,000 to 4,920.
        story.players[LATE].battles = attacks(day_2, 40, 40) + defenses(day_2, *[20] * 8)
        story.reset(day_1, {FULL: (5000, SEASON)})
        story.reads(day_1 + timedelta(minutes=20), {FULL: 5000})
        story.run_worker()
        story.reads(day_2 - timedelta(minutes=10), {FULL: 5070})
        story.reset(day_2, {FULL: (5070, SEASON)})
        story.reads(day_2 + timedelta(minutes=20), {FULL: 5070})
        story.run_worker()
        story.reads(day_3 - timedelta(minutes=10), {FULL: 5060})
        story.reset(day_3, {FULL: (5060, SEASON), LATE: (4920, SEASON)})
        story.reads(day_3 + timedelta(minutes=20), {FULL: 5060, LATE: 4920})
        story.run_worker()
        story.wait(timedelta(hours=1))
        board = story.served_board(day_3)

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            _account(owner, "discord", DISCORD_ID, "drift")
            store = Store(api)
            commands = Commands(
                store, Site("https://clashlens.test"), now=lambda: day_3 - timedelta(minutes=1)
            )
            full = commands.stats(DISCORD_ID, "drift", FULL)
            late = commands.stats(DISCORD_ID, "drift", LATE)
        finally:
            api.close()
            owner.close()

    assert board is not None
    assert {entry["tag"]: entry["trophies"] for entry in board["entries"]}[FULL] == 5060
    # 5 three-stars in 7 attacks, 1 of 16 defenses tripled.
    assert full.cards[0].parts[1].splitlines()[:3] == [
        "**Season Stats**",
        "Attack hit rate: 71%",
        "Defenses held: 94%",
    ]
    assert late.cards[0].parts[1].splitlines()[:3] == [
        "**Season Stats**",
        "Attack hit rate: Unavailable",
        "Defenses held: Unavailable",
    ]
