"""End-to-end: the Live Leaderboard's shared copy around a Reset.

Saved game data goes through the real worker, which publishes the Reset's
Daily board; the Live Leaderboard, its search and public profiles' ranks then
read one shared copy of the board. A copy is never served for a Legend day it
was not read for, one 20 seconds old is replaced in the background instead of
making a visitor wait, and one 30 seconds old is never served. See
docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import as_api_role, domain_database
from test_api_db_organization import create_owner
from test_group_comparison_postgres import link_player
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import api_accounts, api_leaderboard
from clashlens.api_db import ApiDatabase

FIRST, SECOND, THIRD = "#2PP", "#8PP", "#9PP"


def test_live_board_copy_follows_the_reset_and_refreshes_in_the_background(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Day 3 of the Season from 5 October 2026: FIRST and THIRD end tied at
    5,340 and SECOND at 5,100. On Day 4 SECOND attacks at 05:30 and FIRST at
    07:30."""
    day_3, day_4 = day(3), day(4)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, FIRST, SECOND, THIRD)
        # 8 defenses each, so no automatic loss changes the totals at Reset.
        story.players[FIRST].battles = attacks(day_3, 40, 40, 40) + defenses(day_3, *[10] * 8)
        story.players[SECOND].battles = attacks(day_3, 40) + defenses(day_3, *[30] * 8)
        story.players[THIRD].battles = attacks(day_3, 40, 40, 40, 40) + defenses(
            day_3, *[5] * 8)
        story.reset(day_3, {FIRST: (5300, SEASON), SECOND: (5300, SEASON),
                            THIRD: (5220, SEASON)})
        story.reads(day_4 - timedelta(minutes=10), {FIRST: 5340, SECOND: 5100, THIRD: 5340})
        story.run_worker()

        api = ApiDatabase(as_api_role(connection_info))
        owner = ApiDatabase(connection_info)
        clock = [0.0]
        refreshes = []
        cache = api_leaderboard.LiveBoardCache(
            clock=lambda: clock[0], start=refreshes.append)

        def live(now):
            board = api_leaderboard.get_live_leaderboard(
                api, limit=100, now=now, cache=cache)
            return {entry["tag"]: entry["trophies"] for entry in board["entries"]}

        try:
            before_reset = live(day_4 - timedelta(minutes=2))

            story.reset(day_4, {FIRST: (5340, SEASON), SECOND: (5100, SEASON),
                                THIRD: (5340, SEASON)})
            story.players[SECOND].battles.append(
                Battle(day_4 + timedelta(minutes=30), 40, True))
            story.reads(day_4 + timedelta(minutes=45), {SECOND: 5140})
            story.run_worker()
            unfinished = story.unfinished_jobs()
            daily = story.served_board()

            # 10 seconds later by the copy's clock, but a new Legend day.
            clock[0] = 10
            after_reset = live(day_4 + timedelta(minutes=50))
            search = api_leaderboard.search_live_leaderboard(
                api, "Synthetic", now=day_4 + timedelta(minutes=50), cache=cache)

            # Public profiles take their ranks from the same copy.
            account_id = create_owner(owner)
            for tag in (FIRST, THIRD):
                link_player(owner, account_id, tag)
            cards = api_accounts.get_public_user(
                api, "groupowner", now=day_4 + timedelta(minutes=50), live_board=cache,
            )["verified_players"]
            with api.pool.connection() as connection:
                ranked_now = api_leaderboard.live_positions(
                    connection, [FIRST, SECOND, THIRD], now=day_4 + timedelta(minutes=50))

            # FIRST's next reading lands; the copy is 25 seconds old, so the
            # next visitor still gets it at once and one background read follows.
            story.players[FIRST].battles.append(
                Battle(day_4 + timedelta(hours=2, minutes=30), 40, True))
            story.reads(day_4 + timedelta(hours=3), {FIRST: 5380})
            story.run_worker()
            clock[0] = 35
            expired = live(day_4 + timedelta(hours=3, minutes=5))
            queued = len(refreshes)
            # At 30 seconds old it is never served, though its background
            # read has not run yet: the visitor waits for a new one.
            clock[0] = 40
            too_old = live(day_4 + timedelta(hours=3, minutes=5))
            for refresh in refreshes:
                refresh()
            refreshed = live(day_4 + timedelta(hours=3, minutes=5))
        finally:
            api.close()
            owner.close()

    assert unfinished == []
    # The Reset's Daily board, from the saved days: Day 3's ends.
    assert _ranked(daily) == {FIRST: 5340, THIRD: 5340, SECOND: 5100}
    assert before_reset == {FIRST: 5340, THIRD: 5340, SECOND: 5100}
    # The copy read before the Reset is not served after it.
    assert after_reset == {FIRST: 5340, THIRD: 5340, SECOND: 5140}
    # Ranks are the same positions as ranking every tracked player afresh,
    # in the same tie order, for search and public profiles alike.
    assert sorted(ranked_now.values()) == [1, 2, 3] and ranked_now[SECOND] == 3
    assert {result["tag"]: result["rank"] for result in search["results"]} == ranked_now
    assert [(card["tag"], card["rank"]) for card in cards] == [
        (FIRST, ranked_now[FIRST]), (THIRD, ranked_now[THIRD])]
    assert expired == after_reset
    assert queued == 1
    assert too_old == refreshed == {FIRST: 5380, THIRD: 5340, SECOND: 5140}
