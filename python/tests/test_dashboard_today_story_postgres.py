"""End-to-end: the Legend day card's next Reset rank range from saved game
data, through the real worker, as the data service serves it.

The shared board behind the range took 7 seconds to read on 13 October 2026
and every dashboard request waited for it. Now it is read in the
background: a request answers at once without the range until the read
finishes, and a board never serves the next Legend day. See docs/testing.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from threading import Event
from typing import Any

from domain_test_support import as_api_role, domain_database
from test_reset_scenarios_postgres import SEASON, _story, attacks, day, defenses

from clashlens import api_dashboard
from clashlens.api_db import ApiDatabase

# Three players, by Season Day 8's battles.
CLIMBER, DONE, RESTING = "#2PP", "#8PP", "#9PP"


def test_rank_range_is_read_in_the_background_and_never_crosses_a_reset(
    database_url: str, archive_server, monkeypatch
) -> None:
    monday = day(8)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       CLIMBER, DONE, RESTING)
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
        board = api_dashboard.DashboardBoard()
        api = ApiDatabase(as_api_role(connection_info))
        try:
            now = afternoon + timedelta(minutes=5)
            # The requests answer while the board is still being read, with
            # the player's own numbers and no range, and start one read.
            asked = time.monotonic()
            first = api_dashboard.get_player_today(api, board, CLIMBER, now=now)
            again = api_dashboard.get_player_today(api, board, DONE, now=now)
            waited = time.monotonic() - asked
            go.set()
            served = _when_read(lambda: {
                tag: api_dashboard.get_player_today(api, board, tag, now=now)
                for tag in (CLIMBER, DONE, RESTING)
            })

            # The next Legend day never shows this day's board.
            go.clear()
            next_day = day(9) + timedelta(hours=1)
            tomorrow = api_dashboard.get_player_today(api, board, CLIMBER, now=next_day)
            go.set()
            _when_read(lambda: {
                CLIMBER: api_dashboard.get_player_today(api, board, CLIMBER, now=next_day)
            })
        finally:
            api.close()

    assert waited < 5
    assert first is not None and again is not None
    assert (first["rank_range"], first["board_computed_at"]) == (None, None)
    assert (again["rank_range"], again["board_computed_at"]) == (None, None)
    assert first["open_defenses"] == 8
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
    # One read for the first two requests, one for the new day.
    assert reads == [now, next_day]


def _when_read(serve: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """What ``serve`` gives once every answer has its range, within 10 s."""
    deadline = time.monotonic() + 10
    while True:
        served = serve()
        if all(today["rank_range"] for today in served.values()):
            return served
        assert time.monotonic() < deadline, "the board was never read"
        time.sleep(0.05)
