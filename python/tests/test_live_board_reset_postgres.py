"""End-to-end: each Live board row carries its place on the Daily board
published at the last Reset, what the player has won or lost today so far,
and each of this Season's last 7 finished Legend days by its start, with
that day's result. Rankings, the Top 10 and a crew show from them how far a
player moved, their day so far and their last 7 days.

Today and each day's result are the player page's own, so Rankings agrees
with the dashboard: today is the day's battles so far from its settled start,
as the dashboard's Legend day card counts them, and each finished day is its
result in the Daily Legend log. An automatic defense loss a Reset reading was
taken before is never today's, and each day is named by its own start, so a
Reset whose board is late or never published moves no day.

The stories save the readings the collector would have saved, run the real
worker until the Daily boards publish, and read the Live board the API
serves. The worker saves a running day only while the database's clock is
in it, so the story of today's net is today's Legend day, whatever the
date; the others run on past dates, when each day saves at its end Reset.
See docs/testing.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from test_crews_postgres import Site
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    SEASON,
    Story,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import api_crew_boards, api_leaderboard, api_players
from clashlens.api_db import ApiDatabase
from clashlens.domain import ranked_day_for

CLIMBER, STILL, NEWCOMER = "#2PP", "#8PP", "#9PP"
LOSER, STEADY = "#2PP", "#8PP"
MINUTE, HOUR, DAY = timedelta(minutes=1), timedelta(hours=1), timedelta(days=1)


def _number(start: str) -> int:
    """The Season day number of a Legend day starting at ``start``."""
    return ranked_day_for(datetime.fromisoformat(start)).day_number


def _live(story: Story, now, focus_tag: str | None = None) -> dict[str, tuple]:
    """Each served Live board row's (place, trophies, place at the last
    Reset, today so far, and its recent days as (Season day, result)). Every
    row agrees with the player's page at ``now``."""
    with psycopg.connect(story.connection_info) as connection:
        api_leaderboard.save_live_unplayed(connection, now)
    api = ApiDatabase(as_api_role(story.connection_info))
    try:
        board = api_leaderboard.get_live_leaderboard(
            api, limit=100, now=now, focus_tag=focus_tag)
        pages = {
            entry["tag"]: api_players.get_player_page(
                api, entry["tag"], now=now, freshness_seconds=600)
            for entry in board["entries"]
        } if board is not None else {}
    finally:
        api.close()
    assert board is not None
    served = {
        entry["tag"]: (entry["position"], entry["trophies"], entry["reset_position"],
                       entry["today_change"],
                       [(_number(saved["ranked_day_start"]), saved["net_trophy_change"])
                        for saved in entry["recent_days"]])
        for entry in board["entries"]
    }
    for tag, row in served.items():
        assert row[3:] == _page_day(pages[tag], [number for number, _ in row[4]]), tag
    return served


def _page_day(page: dict, numbers: list[int]) -> tuple[int | None, list[tuple]]:
    """What the player page shows: today's net as the dashboard's Legend day
    card counts a running day, its battles' gain less loss once they are
    every battle so far, and each day's result in the Daily Legend log by
    Season day number. A story's days have all ended by the real clock, so
    today's saved log carries a result already; a running day's never does."""
    screen = page["screen_ready"]
    days = {_number(saved["ranked_day_start"]): saved for saved in screen["days"]}
    today = next((saved for saved in screen["days"]
                  if saved["ranked_day_start"] == screen["current_day_start"]), None)
    net = (today["attack_gain"] - today["defense_loss"]
           if today is not None and today["battles_complete"] else None)
    return net, [(number, (days.get(number) or {}).get("net_trophy_change"))
                 for number in numbers]


def test_live_rows_carry_their_place_on_the_last_resets_daily_board(
    database_url: str, archive_server, monkeypatch
) -> None:
    """CLIMBER and STILL sit out the previous Season's Day 28, then play
    Day 1. The Day 2 Reset publishes Day 1's board, STILL above CLIMBER.
    On Day 2 CLIMBER wins 3 attacks and passes STILL, and NEWCOMER, never
    on a Daily board, is first read. STILL misses the Day 3 Reset, so it
    is off Day 2's board, and is back for the Day 4 Reset."""
    day_28, day_1, day_2 = day(28, PREVIOUS_SEASON), day(1), day(2)
    day_3, day_4 = day(3), day(4)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       CLIMBER, STILL, NEWCOMER)
        players = story.players
        # Day 1: CLIMBER ends at 5,000 + 40 - 8 * 20 = 4,880, STILL at 4,920.
        players[CLIMBER].battles = attacks(day_1, 40) + defenses(day_1, *[20] * 8)
        players[STILL].battles = attacks(day_1, 40, 40) + defenses(day_1, *[20] * 8)
        # Day 2: CLIMBER climbs to 5,000.
        players[CLIMBER].battles += attacks(day_2, 40, 40, 40)
        players[NEWCOMER].battles = attacks(day_2, 40)

        story.reset(day_28, {CLIMBER: (5300, PREVIOUS_SEASON),
                             STILL: (5400, PREVIOUS_SEASON)})
        story.reads(day_1 - timedelta(minutes=10), {CLIMBER: 5300, STILL: 5400},
                    PREVIOUS_SEASON)
        story.reset(day_1, {CLIMBER: (5000, SEASON), STILL: (5000, SEASON)})
        story.reads(day_1 + timedelta(minutes=20), {CLIMBER: 5000, STILL: 5000})
        story.run_worker()
        season_end_board = story.served_board(day_1)
        # Day 1: the newest Daily board is the previous Season's last.
        first_day = _live(story, day_1 + timedelta(minutes=30))

        ends = {CLIMBER: 4880, STILL: 4920}
        story.reads(day_2 - timedelta(minutes=10), ends)
        story.reset(day_2, {tag: (trophies, SEASON) for tag, trophies in ends.items()})
        story.reads(day_2 + timedelta(minutes=20), ends)
        story.run_worker()
        story.wait(timedelta(hours=1))
        day_1_board = story.served_board(day_2)
        story.reads(day_2 + timedelta(hours=5),
                    {CLIMBER: 5000, STILL: 4920, NEWCOMER: 4960})
        story.run_worker()
        second_day = _live(story, day_2 + timedelta(hours=6))
        focused = _live(story, day_2 + timedelta(hours=6), focus_tag=NEWCOMER)

        day_2_ends = {CLIMBER: 5000, NEWCOMER: 4960}
        story.reads(day_3 - timedelta(minutes=10), day_2_ends)
        story.reset(day_3, {tag: (trophies, SEASON) for tag, trophies in day_2_ends.items()})
        story.reads(day_3 + timedelta(minutes=20), day_2_ends)
        story.run_worker()
        story.wait(timedelta(hours=1))
        day_2_board = story.served_board(day_3)
        day_3_ends = {**day_2_ends, STILL: 4920}
        story.reads(day_4 - timedelta(minutes=10), day_3_ends)
        story.reset(day_4, {tag: (trophies, SEASON) for tag, trophies in day_3_ends.items()})
        story.reads(day_4 + timedelta(minutes=20), day_3_ends)
        story.run_worker()
        story.wait(timedelta(hours=1))
        day_3_board = story.served_board(day_4)
        fourth_day = _live(story, day_4 + timedelta(hours=2))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # The previous Season's last board was published, yet Day 1 compares
    # against nothing and charts no day: that Season's are not this one's.
    # Its day so far starts at the Season's 5,000, as the dashboard's does.
    assert _ranked(season_end_board) == {STILL: 5400, CLIMBER: 5300}
    assert {tag: row[1:] for tag, row in first_day.items()} == {
        CLIMBER: (5000, None, 0, []), STILL: (5000, None, 0, [])}
    assert _ranked(day_1_board) == {STILL: 4920, CLIMBER: 4880}
    # CLIMBER moved from 2nd to 1st; STILL fell to 3rd on the same 4,920.
    # NEWCOMER was on no Daily board. Day 1 is each one's result there.
    assert {tag: (*row[:3], row[4]) for tag, row in second_day.items()} == {
        CLIMBER: (1, 5000, 2, [(1, -120)]),
        NEWCOMER: (2, 4960, None, [(1, 0)]),
        STILL: (3, 4920, 1, [(1, -80)]),
    }
    # The focused page around one player says the same.
    assert focused == second_day
    # Three finished days of this Season, oldest first, each by its own day.
    # STILL missed the Day 3 Reset, so neither Day 2 nor Day 3 has a result:
    # two empty days, not one merged day. NEWCOMER's Day 2 had no start.
    assert _ranked(day_2_board) == {CLIMBER: 5000, NEWCOMER: 4960}
    assert _ranked(day_3_board) == {CLIMBER: 5000, NEWCOMER: 4960, STILL: 4920}
    assert {tag: (row[2], row[4]) for tag, row in fourth_day.items()} == {
        CLIMBER: (1, [(1, -120), (2, 120), (3, 0)]),
        NEWCOMER: (2, [(1, 0), (2, None), (3, 0)]),
        STILL: (3, [(1, -80), (2, None), (3, None)]),
    }


def test_each_day_keeps_its_own_date_whatever_the_boards(
    database_url: str, archive_server, monkeypatch
) -> None:
    """LOSER leaves 6 defense slots open on Day 2. Its automatic loss of 180
    lands after the Day 3 Reset reading, so that Reset's board ranks it at
    4,900, 20 below Day 1's, while Day 2's result is 200 down. The Day 4
    Reset is never swept, so it has no board, and the Day 5 Reset is read
    before its board publishes. STEADY plays all 16 battles every day for 80."""
    days = {number: day(number) for number in range(1, 6)}
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, LOSER, STEADY)
        story.players[LOSER].battles = (
            # Day 1: 4 attacks and 8 defenses, 5,000 to 4,920.
            attacks(days[1], *[40] * 4) + defenses(days[1], *[30] * 8)
            # Day 2: 1 attack and 2 defenses to 4,900, less 6 open slots at
            # floor((240 + 60) / (8 + 2)) = 30 each: 4,720.
            + attacks(days[2], 40) + defenses(days[2], 30, 30)
            # Day 3: 2 attacks and 8 defenses, 4,560; Day 4: 3 and 8, 4,440.
            + attacks(days[3], 40, 40) + defenses(days[3], *[30] * 8)
            + attacks(days[4], 40, 40, 40) + defenses(days[4], *[30] * 8))
        story.players[STEADY].battles = [
            battle for number in range(1, 5)
            for battle in attacks(days[number], *[40] * 8) + defenses(days[number], *[30] * 8)]

        story.reset(days[1], {LOSER: (5000, SEASON), STEADY: (5000, SEASON)})
        story.reads(days[1] + 20 * MINUTE, {LOSER: 5000, STEADY: 5000})
        ends = {LOSER: 4920, STEADY: 5080}
        story.reads(days[2] - 10 * MINUTE, ends)
        story.reset(days[2], {tag: (trophies, SEASON) for tag, trophies in ends.items()})
        story.reads(days[2] + 20 * MINUTE, ends)
        # The Day 3 Reset reads LOSER before its loss lands; the 05:20 check after.
        ends = {LOSER: 4900, STEADY: 5160}
        story.reads(days[3] - 10 * MINUTE, ends)
        story.reset(days[3], {tag: (trophies, SEASON) for tag, trophies in ends.items()})
        story.reads(days[3] + 20 * MINUTE, {LOSER: 4720, STEADY: 5160})
        story.run_worker()
        story.wait(HOUR)
        day_2 = story.saved_day(LOSER, days[2])
        day_2_board = story.served_board(days[3])
        third_day = _live(story, days[3] + HOUR)
        # No sweep at the Day 4 Reset: the collector's checks around it only.
        ends = {LOSER: 4560, STEADY: 5240}
        story.reads(days[4] - 10 * MINUTE, ends)
        story.reads(days[4] + 20 * MINUTE, ends)
        ends = {LOSER: 4440, STEADY: 5320}
        story.reads(days[5] - 10 * MINUTE, ends)
        story.run_worker()
        # The Day 5 Reset is read, but its board is not built yet.
        story.reset(days[5], {tag: (trophies, SEASON) for tag, trophies in ends.items()},
                    run=False)
        unpublished = _live(story, days[5] + 10 * MINUTE)
        story.reads(days[5] + 20 * MINUTE, ends)
        story.run_worker()
        story.wait(HOUR)
        published = _live(story, days[5] + HOUR)
        # Four days on, Day 1 is out of the 7 days before Day 9.
        week_later = _live(story, day(9) + HOUR)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert day_2 is not None
    assert (day_2["state"], day_2["start"], day_2["end"], day_2["automatic_loss"]) == (
        "Complete", 4920, 4720, 180)
    # The board ranks each by their total at 05:00:00, before the loss.
    assert _ranked(day_2_board) == {STEADY: 5160, LOSER: 4900}
    # Day 2 is its result, loss and all: 4,920 to 4,720, not 4,920 to the
    # board's 4,900.
    assert {tag: (*row[:3], row[4]) for tag, row in third_day.items()} == {
        STEADY: (1, 5160, 1, [(1, 80), (2, 80)]),
        LOSER: (2, 4720, 2, [(1, -80), (2, -200)]),
    }
    # Minutes after the Day 5 Reset, before its board: no place to compare
    # with, and Day 4, not saved yet, is an empty Day 4. Day 3, whose end
    # Reset was never swept, stays its own empty day.
    assert {tag: (row[2], row[4]) for tag, row in unpublished.items()} == {
        STEADY: (None, [(1, 80), (2, 80), (3, None), (4, None)]),
        LOSER: (None, [(1, -80), (2, -200), (3, None), (4, None)]),
    }
    # Once saved, Day 4 is Day 4, though Day 3's Reset has no board. LOSER's
    # starts from no Reset reading nor a saved Day 3, so it has no result.
    assert {tag: (row[2], row[4]) for tag, row in published.items()} == {
        STEADY: (1, [(1, 80), (2, 80), (3, None), (4, 80)]),
        LOSER: (2, [(1, -80), (2, -200), (3, None), (4, None)]),
    }
    # Only the 7 days before today's: Days 2 to 8, those not saved empty.
    assert {tag: row[4] for tag, row in week_later.items()} == {
        STEADY: [(2, 80), (3, None), (4, 80), (5, None), (6, None), (7, None), (8, None)],
        LOSER: [(2, -200), (3, None), (4, None), (5, None), (6, None), (7, None),
                (8, None)],
    }


def test_today_starts_from_the_days_settled_start_as_on_the_dashboard(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Today's Legend day, whatever the date. LOSER leaves 6 defense slots
    open yesterday: its automatic loss of 180 lands after today's Reset
    reading, so that Reset's board ranks it at 5,300 while today starts at
    5,120. STEADY plays all 16 battles a day for 80."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            today = ranked_day_for(connection.execute("SELECT clock_timestamp()").fetchone()[0])
        if today.day_number <= 3:
            # Two whole days before today, after the Season's Day 1 reset to 5,000.
            pytest.skip(f"Season Day {today.day_number}")
        season, start = today.season_start, today.start
        before, yesterday = start - 2 * DAY, start - DAY
        story = _story(connection_info, archive_server, monkeypatch, LOSER, STEADY)
        story.players[LOSER].battles = (
            # 4 attacks and 8 defenses, 5,400 to 5,320.
            attacks(before, *[40] * 4) + defenses(before, *[30] * 8)
            # 1 attack and 2 defenses to 5,300, less 6 x 30: 5,120.
            + attacks(yesterday, 40) + defenses(yesterday, 30, 30)
            # Today, by 10:55: 2 attacks and 6 defenses, 100 down.
            + attacks(start, 40, 40) + defenses(start, *[30] * 6))
        story.players[STEADY].battles = [
            battle for at in (before, yesterday)
            for battle in attacks(at, *[40] * 8) + defenses(at, *[30] * 8)
        ] + attacks(start, *[40] * 5) + defenses(start, *[30] * 6)

        story.reset(before, {LOSER: (5400, season), STEADY: (5400, season)})
        story.reads(before + 20 * MINUTE, {LOSER: 5400, STEADY: 5400}, season)
        ends = {LOSER: 5320, STEADY: 5480}
        story.reads(yesterday - 10 * MINUTE, ends, season)
        story.reset(yesterday, {tag: (trophies, season) for tag, trophies in ends.items()})
        story.reads(yesterday + 20 * MINUTE, ends, season)
        # Today's Reset reads LOSER before its loss lands; the 05:20 check after.
        ends = {LOSER: 5300, STEADY: 5560}
        story.reads(start - 10 * MINUTE, ends, season)
        story.reset(start, {tag: (trophies, season) for tag, trophies in ends.items()})
        story.reads(start + 20 * MINUTE, {LOSER: 5120, STEADY: 5560}, season)
        story.run_worker()
        story.wait(HOUR)
        board = story.served_board()
        settled_at = start + 25 * MINUTE
        settled = _live(story, settled_at)
        # A crew of both shows the same day so far.
        site = Site(connection_info)
        try:
            site.account("owner")
            site.link("owner", LOSER)
            site.link("owner", STEADY)
            crew = api_crew_boards.get_crew_boards(
                site.api, site.ids["owner"], site.make("owner", [LOSER, STEADY]),
                period="today", now=settled_at)
        finally:
            site.close()
        story.reads(start + 5 * HOUR + 55 * MINUTE, {LOSER: 5020, STEADY: 5580}, season)
        story.run_worker()
        midday = _live(story, start + 6 * HOUR)
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    # The board ranks LOSER by its total at 05:00:00, before the loss.
    assert _ranked(board) == {STEADY: 5560, LOSER: 5300}
    # LOSER has not played today: nothing today, not the 180 its Reset
    # reading was taken before. Yesterday is its result, loss and all.
    assert {tag: row[1:4] for tag, row in settled.items()} == {
        STEADY: (5560, 1, 0), LOSER: (5120, 2, 0)}
    assert {tag: [change for _, change in row[4][-2:]] for tag, row in settled.items()} == {
        STEADY: [80, 80], LOSER: [-80, -200]}
    assert {row["tag"]: row["today_change"] for row in crew["boards"]["live"]["rows"]} == {
        STEADY: 0, LOSER: 0}
    # Today is the day's battles so far.
    assert {tag: row[1:4] for tag, row in midday.items()} == {
        STEADY: (5580, 1, 20), LOSER: (5020, 2, -100)}
