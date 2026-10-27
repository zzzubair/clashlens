"""End-to-end: the Live Leaderboard follows the weekly league record across
mid-Season Monday Resets.

On 26 October 2026 the Live board showed "1-100 of 15,051" against 13,024
Legend I players. 2,047 of the 2,079 players dropped at that Reset stayed at
their last Legend I trophies all week: lower-league profiles naming Season 0
or the Legend II week's ID never pass the Season check mid-Season, so the
drop is proven only by two of them and the player is still tracked. 317
promoted players still ranked at a Legend II profile from the Season's first
week, whose Season ID passed, and 98 promoted back after a drop at a Legend I
profile from before it, while the game ranked all 415 at 5,000. These stories
save the readings the collector would have saved, run the real worker over
them and read the Live board the API serves. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import as_api_role, domain_database
from test_league_week_days_postgres import Climber
from test_reset_scenarios_postgres import Story, attacks, day, defenses

from clashlens import api_leaderboard
from clashlens.api_db import ApiDatabase

STEADY, DROPPED, BACK, STALE_II, PROMOTED = "#9PP", "#2PP", "#8PP", "#PPP", "#2QQ"


def _live(story: Story, now) -> dict[str, int]:
    """The served Live board's players and trophies, after the worker's
    search for players at 5,000 who have not played yet."""
    with psycopg.connect(story.connection_info) as connection:
        api_leaderboard.save_live_unplayed(connection, now)
    api = ApiDatabase(as_api_role(story.connection_info))
    try:
        board = api_leaderboard.get_live_leaderboard(api, limit=100, now=now)
    finally:
        api.close()
    assert board is not None
    return {entry["tag"]: entry["trophies"] for entry in board["entries"]}


def _tracked(story: Story, tag: str) -> bool:
    with psycopg.connect(story.connection_info) as connection:
        return connection.execute(
            "SELECT active FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone()[0]


def test_the_live_board_follows_the_weekly_league_record(
    database_url: str, archive_server, monkeypatch
) -> None:
    """STEADY plays Legend I all Season. DROPPED and BACK play the Season's
    first week, then read Legend II naming Season 0 twice after the second
    Monday Reset, which proves the drop but keeps them tracked. BACK is
    promoted back at the third Monday Reset and reads Legend I naming Season
    0 at 5,000, as #L8J0JRC22 did on 26 October 2026 after its 4,951 of 19
    October, then signs up, wins an attack and reads 5,040 again, the same
    profile it read in the first week. STALE_II and PROMOTED read Legend II naming the Season in its
    first week, which passes, as #JUVP8Y89's 783 of 9 October did, and are
    promoted at the second Monday Reset: STALE_II reads Legend I naming
    Season 0 at 5,000; PROMOTED signs up, wins an attack and reads 5,040."""
    monday, next_monday = day(8), day(15)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {
            tag: Climber(tag) for tag in (STEADY, DROPPED, BACK, STALE_II, PROMOTED)})
        players = story.players
        for tag in (STEADY, DROPPED, BACK):
            players[tag].battles = attacks(day(2), 40) + defenses(day(6), 30, 30)
        players[STEADY].battles += attacks(monday, 40) + attacks(next_monday, 40)
        players[PROMOTED].battles = attacks(monday + timedelta(hours=1), 40)
        for tag in (STALE_II, PROMOTED):
            players[tag].legend_ii = True

        # The Season's first week.
        story.reads(day(2) + timedelta(hours=3), {STEADY: 5040, DROPPED: 5040, BACK: 5040})
        story.reads(day(4) + timedelta(hours=9), {STALE_II: 783, PROMOTED: 900})
        story.reads(day(7) + timedelta(hours=3), {STEADY: 4980, DROPPED: 4900, BACK: 4951})
        story.wait(timedelta(minutes=5))

        # The second Monday: DROPPED and BACK drop, STALE_II and PROMOTED
        # are promoted.
        for tag in (DROPPED, BACK):
            players[tag].dropped = True
        for tag in (STALE_II, PROMOTED):
            players[tag].legend_ii = False
        story.reads(monday + timedelta(hours=2), {STEADY: 5020, DROPPED: 40, BACK: 40})
        story.reads(monday + timedelta(hours=2), {STALE_II: 5000}, None)
        story.reads(monday + timedelta(hours=3), {DROPPED: 48, BACK: 48, PROMOTED: 5040})
        story.wait(timedelta(minutes=5))
        second_week = _live(story, monday + timedelta(hours=4))
        dropped_tracked = _tracked(story, DROPPED)

        # The third Monday: BACK is promoted back; DROPPED is still in
        # Legend II.
        players[BACK].dropped = False
        story.reads(next_monday + timedelta(hours=2), {STEADY: 5020, DROPPED: 60})
        story.reads(next_monday + timedelta(hours=2), {BACK: 5000}, None)
        players[BACK].battles += attacks(next_monday + timedelta(hours=2, minutes=30), 40)
        story.reads(next_monday + timedelta(hours=3), {BACK: 5040})
        story.wait(timedelta(minutes=5))
        third_week = _live(story, next_monday + timedelta(hours=4))

    # STALE_II has not played since its promotion, so ranks at 5,000 from
    # its newest profile; PROMOTED's own Legend I profile ranks it.
    assert second_week == {PROMOTED: 5040, STEADY: 5020, STALE_II: 5000}
    # Polling goes on until stronger proof settles DROPPED's week.
    assert dropped_tracked
    # BACK ranks at its fresh reading, though it matches a first-week profile.
    assert third_week == {STEADY: 5020, STALE_II: 5000, PROMOTED: 5040, BACK: 5040}
