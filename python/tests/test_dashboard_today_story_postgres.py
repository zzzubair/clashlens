"""End-to-end: the Legend day card's next Reset rank range from saved game
data, through the real worker, as the data service serves it.

The shared board behind the range took 7 seconds to read on 13 October 2026
and every dashboard request waited for it. Now it is read in the
background: a request without a board answers at once without the range,
and a follow-up waits up to 4 seconds for the read under way. Neither that
board nor the Live Leaderboard read it is built on ever serves the next
Legend day. See docs/testing.md.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from typing import Any

from domain_test_support import as_api_role, domain_database
from test_reset_scenarios_postgres import SEASON, _story, attacks, day, defenses

from clashlens import api_dashboard, api_leaderboard
from clashlens.api_db import ApiDatabase

# Three players, by Season Day 8's battles, and one first read on Day 9.
CLIMBER, DONE, RESTING, NEWCOMER = "#2PP", "#8PP", "#9PP", "#PPP"


def test_rank_range_fills_in_from_the_background_read_and_never_crosses_a_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    monday = day(8)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       CLIMBER, DONE, RESTING, NEWCOMER)
        # Two +40 attacks so far: 5,380, with 6 attacks and 8 defenses open.
        story.players[CLIMBER].battles = attacks(monday, 40, 40)
        # Every battle used: 8 attacks of 40 and 8 defenses of 30 end on 5,280.
        story.players[DONE].battles = (
            attacks(monday, *[40] * 8) + defenses(monday, *[30] * 8)
        )
        story.reset(monday, {CLIMBER: (5300, SEASON), DONE: (5200, SEASON),
                             RESTING: (5100, SEASON)})
        afternoon = monday + timedelta(hours=10)
        story.reads(afternoon, {CLIMBER: 5380, DONE: 5280, RESTING: 5100})
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
        board = api_dashboard.DashboardBoard(
            api_leaderboard.LiveBoardCache(clock=lambda: 0.0)
        )
        api = ApiDatabase(as_api_role(connection_info))
        try:
            now = afternoon + timedelta(minutes=5)
            # The first request answers at once with the player's own
            # numbers and no range.
            asked = time.monotonic()
            first = api_dashboard.get_player_today(api, board, CLIMBER, now=now)
            waited = time.monotonic() - asked
            # A follow-up waiting when the read finishes answers with its range.
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(
                    api_dashboard.get_player_today, api, board, DONE, now=now,
                    board_wait=api_dashboard.BOARD_WAIT_SECONDS,
                )
                time.sleep(0.5)
                go.set()
                again = pending.result(timeout=10)
            served = {
                tag: api_dashboard.get_player_today(api, board, tag, now=now)
                for tag in (CLIMBER, DONE, RESTING)
            }

            # The Day 9 Reset finds a player new to the board.
            story.reset(day(9), {NEWCOMER: (5400, SEASON)})
            # The next Legend day never shows this day's board.
            go.clear()
            next_day = day(9) + timedelta(hours=1)
            tomorrow = api_dashboard.get_player_today(api, board, CLIMBER, now=next_day)
            go.set()
            # Nor this day's Live Leaderboard: the newcomer is on Day 9's board.
            later = {
                tag: api_dashboard.get_player_today(
                    api, board, tag, now=next_day,
                    board_wait=api_dashboard.BOARD_WAIT_SECONDS,
                )
                for tag in (CLIMBER, NEWCOMER)
            }
        finally:
            api.close()

    assert waited < 1
    assert first is not None and again is not None
    assert (first["rank_range"], first["board_computed_at"]) == (None, None)
    assert first["open_defenses"] == 8
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
    # Every slot open on Day 9: best ends 5,700, 5,600, 5,420 and the
    # newcomer's 5,720; the newcomer's worst, 5,080, sits under all four.
    assert later[NEWCOMER]["rank_range"] == {"best": 1, "worst": 4}
    assert later[CLIMBER]["board_computed_at"] == next_day.isoformat()
    # One read for the first two requests, one for the new day.
    assert reads == [now, next_day]
