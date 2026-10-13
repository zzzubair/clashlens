"""End-to-end: the Legend day card's next Reset rank range from saved game
data, through the real worker, as the data service serves it.

The shared board behind the range took 7 seconds to read on 13 October 2026
and every dashboard request waited for it. Now it is read in the
background: a request without a board answers at once without the range,
and the page asks again, each time waiting up to 4 seconds for today's
read, until the range comes. Neither that board nor the Live Leaderboard
read it is built on ever serves the next Legend day, even when a read from
before the Reset finishes after it. See docs/testing.md.

The worker saves a day's live numbers only while the database's clock is in
that day, so the story is today's Legend day, whatever the date.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Any

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from test_reset_scenarios_postgres import _story, attacks, defenses

from clashlens import api_dashboard, api_leaderboard
from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

# Three players, by today's battles, and one first read tomorrow.
CLIMBER, DONE, RESTING, NEWCOMER = "#2PP", "#8PP", "#9PP", "#PPP"


def test_rank_range_fills_in_from_a_slow_background_read_and_never_crosses_a_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            today = ranked_day_for(connection.execute("SELECT clock_timestamp()").fetchone()[0])
        if today.day_number in (1, 28):
            # Day 1 starts everyone at 5,000, and Day 28's Reset starts a new
            # Season: neither has the story's two days in one Season.
            pytest.skip(f"Season Day {today.day_number}")
        day_start, season, reset = today.start, today.season_start, today.end
        story = _story(connection_info, archive_server, monkeypatch,
                       CLIMBER, DONE, RESTING, NEWCOMER)
        # Two +40 attacks so far: 5,380, with 6 attacks and 8 defenses open.
        story.players[CLIMBER].battles = attacks(day_start, 40, 40)
        # Every battle used: 8 attacks of 40 and 8 defenses of 30 end on 5,280.
        story.players[DONE].battles = (
            attacks(day_start, *[40] * 8) + defenses(day_start, *[30] * 8)
        )
        story.reset(day_start, {CLIMBER: (5300, season), DONE: (5200, season),
                                RESTING: (5100, season)})
        afternoon = day_start + timedelta(hours=10)
        story.reads(afternoon, {CLIMBER: 5380, DONE: 5280, RESTING: 5100}, season)
        story.run_worker()

        # Each read of the shared board waits until the test lets it go.
        go, reads = Event(), []
        read_board = api_dashboard._read_board

        def held_read(*args: Any) -> Any:
            reads.append(args[1])
            go.wait(10)
            return read_board(*args)

        monkeypatch.setattr(api_dashboard, "_read_board", held_read)
        # The Live Leaderboard read never expires by time here, as one read
        # seconds before a Reset would not: only the Legend day ends it.
        ticks = [0.0]
        board = api_dashboard.DashboardBoard(
            api_leaderboard.LiveBoardCache(clock=lambda: 0.0), clock=lambda: ticks[0]
        )
        api = ApiDatabase(as_api_role(connection_info))
        follow_up = api_dashboard.BOARD_WAIT_SECONDS
        try:
            now = afternoon + timedelta(minutes=5)
            # The first request answers at once with the player's own
            # numbers and no range.
            started = time.monotonic()
            first = api_dashboard.get_player_today(api, board, CLIMBER, now=now)
            waited = time.monotonic() - started
            # The read takes 7 seconds, as cold ones did: the page's first
            # follow-up gives up after 4, and the next gets the range.
            gave_up = api_dashboard.get_player_today(
                api, board, DONE, now=now, board_wait=follow_up
            )
            gave_up_at = time.monotonic() - started
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(
                    api_dashboard.get_player_today, api, board, DONE, now=now,
                    board_wait=follow_up,
                )
                time.sleep(max(0.0, started + 7 - time.monotonic()))
                go.set()
                again = pending.result(timeout=10)
            served = {
                tag: api_dashboard.get_player_today(api, board, tag, now=now)
                for tag in (CLIMBER, DONE, RESTING)
            }

            # A read due seconds before the next Reset is still going after it.
            go.clear()
            ticks[0] += api_dashboard.BOARD_SECONDS
            before_reset = reset - timedelta(seconds=10)
            api_dashboard.get_player_today(api, board, CLIMBER, now=before_reset)
            # The next Reset finds a player new to the board.
            story.reset(reset, {NEWCOMER: (5400, season)})
            next_day = reset + timedelta(hours=1)
            # The next Legend day never shows the day before's board.
            tomorrow = api_dashboard.get_player_today(api, board, CLIMBER, now=next_day)
            # A follow-up waiting when the late read finishes gets the next
            # day's board, built on that day's Live Leaderboard.
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(
                    api_dashboard.get_player_today, api, board, NEWCOMER, now=next_day,
                    board_wait=follow_up,
                )
                time.sleep(0.5)
                go.set()
                newcomer = pending.result(timeout=10)
            climber = api_dashboard.get_player_today(api, board, CLIMBER, now=next_day)
        finally:
            api.close()

    assert waited < 1
    assert first is not None and gave_up is not None and again is not None
    assert (first["rank_range"], first["board_computed_at"]) == (None, None)
    assert first["open_defenses"] == 8
    assert gave_up["rank_range"] is None
    assert follow_up <= gave_up_at < 7
    assert again["rank_range"] == {"best": 1, "worst": 3}
    # Best ends: 5,380 + 6 x 40 = 5,620; 5,280; 5,100 + 8 x 40 = 5,420.
    # Worst ends: 5,380 - 8 x 40 = 5,060; 5,280; 5,100 - 8 x 40 = 4,780.
    assert {tag: today["rank_range"] for tag, today in served.items()} == {
        CLIMBER: {"best": 1, "worst": 3},
        DONE: {"best": 1, "worst": 3},
        RESTING: {"best": 1, "worst": 3},
    }
    # Every range says when its board was read.
    assert {today["board_computed_at"] for today in served.values()} == {
        now.isoformat()
    }
    assert served[DONE]["legends_held"] == {"held": 8, "defenses": 8}
    assert tomorrow is not None
    assert (tomorrow["rank_range"], tomorrow["board_computed_at"]) == (None, None)
    assert newcomer is not None and climber is not None
    # Every slot open the next day: best ends 5,700, 5,600, 5,420 and the
    # newcomer's 5,720; the newcomer's worst, 5,080, sits under all four.
    assert newcomer["rank_range"] == {"best": 1, "worst": 4}
    assert climber["board_computed_at"] == next_day.isoformat()
    # One read for today's requests, one due before the Reset, one for the next day.
    assert reads == [now, before_reset, next_day]
