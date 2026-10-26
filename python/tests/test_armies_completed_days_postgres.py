"""End-to-end: the Armies view reads only the Legend days the worker has counted.

The worker counts no armies from 04:30 to 07:00 UTC. After each Reset the
view added up the day that had just ended from its attacks, about 90,000 on
25 October 2026, and every player group timed out until about 07:00. It now
leaves that day out until its counts are saved. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import domain_database
from test_reset_scenarios_postgres import (
    SEASON,
    THROWER,
    _story,
    attacks,
    day,
    defenses,
)

PLAYERS = ("#2PP", "#8PP", "#9PP")


def test_armies_leave_out_a_finished_day_until_it_is_counted(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Three players attack 8 times a day with Thrower, 40 trophies each, and
    defend 8 times for 30: each day ends 80 up with no automatic loss. Day 9
    is counted at noon on Day 10. Day 10 ends at the Day 11 Reset and its
    board publishes, but the worker's army counts wait out the quiet window:
    at 06:00 the view serves Day 9 alone, from saved counts, and at 07:00,
    once Day 10 is counted, Days 9 and 10."""
    first, second, third = day(9), day(10), day(11)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *PLAYERS)
        for player in story.players.values():
            player.battles = [
                battle
                for start in (first, second)
                for battle in (attacks(start + timedelta(hours=8), *[40] * 8, army=THROWER)
                               + defenses(start, *[30] * 8))
            ]
        story.reset(day(8), dict.fromkeys(PLAYERS, (5300, SEASON)))
        story.reset(first, dict.fromkeys(PLAYERS, (5300, SEASON)))
        story.reset(second, dict.fromkeys(PLAYERS, (5380, SEASON)))
        story.wait(timedelta(hours=1))
        story.count_armies(second + timedelta(hours=7))
        story.reset(third, dict.fromkeys(PLAYERS, (5460, SEASON)))
        story.wait(timedelta(hours=1))
        # Inside the quiet window only the rank band totals are counted.
        story.count_armies(third + timedelta(minutes=30))
        before = story.served_army_types(third + timedelta(hours=1))
        story.count_armies(third + timedelta(hours=2))
        after = story.served_army_types(third + timedelta(hours=2))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    for view, days in ((before, [9]), (after, [9, 10])):
        assert view is not None
        assert view["collection_coverage"]["covered_days"][-1] == days[-1]
        assert [day for day in view["collection_coverage"]["covered_days"]
                if day >= 9] == days
        assert [(row["label"], row["usage_count"]) for row in view["rows"]] == [
            ("Thrower", 24 * len(days)),
        ]
