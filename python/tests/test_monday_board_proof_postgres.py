"""End-to-end: Monday board entries proven by a reading taken during the day.

On a Monday the game raises every total at or below 5,000 to 5,000 from
about 05:20, so a 5,000 reading after the Reset proves nothing. The
12 October 2026 board left 1,326 Complete days ending at or below 5,000
uncertain, 1,247 of them never played and read at 5,000 during the day. A
reading taken after the day's last battle landed, with none after it, shows
the total at the Reset as well as one taken after it. A Complete day no
reading proves still shows its own total, never an older reading. See
docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import test_reset_scenarios_postgres as scenarios
from domain_test_support import domain_database
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    _ranked,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)

NEVER, EXACT, LL02YLGG, BELOW, AFTER = "#2PP", "#8PP", "#9PP", "#PPP", "#YPP"


def _at(hour: int, minute: int, second: int, date: int = 12) -> datetime:
    return datetime(2026, 10, date, hour, minute, second, tzinfo=UTC)


def _sweep(totals: dict[str, int]) -> dict[str, tuple[int, datetime | None]]:
    """A Reset sweep's readings: NEVER's names Season 0, the rest the Season."""
    return {NEVER: (5000, None), **{tag: (trophies, SEASON) for tag, trophies in totals.items()}}


# #LL02YLGG's Sunday, 11 October 2026: 4,999 to 4,996 over 8 attacks and 8
# defenses, the last two defenses stamped 03:01:53 and 04:58:28.
LL02YLGG_SUNDAY = [
    Battle(at, trophies, trophies > 0) for at, trophies in (
        (_at(5, 50, 8, 11), -26), (_at(12, 59, 46, 11), 20), (_at(14, 2, 54, 11), 40),
        (_at(14, 19, 5, 11), -25), (_at(15, 7, 48, 11), -29), (_at(15, 30, 59, 11), 40),
        (_at(15, 34, 12, 11), 40), (_at(16, 10, 10, 11), 26), (_at(16, 13, 23, 11), 28),
        (_at(16, 15, 44, 11), 40), (_at(16, 20, 12, 11), 14), (_at(16, 53, 9, 11), -25),
        (_at(18, 8, 27, 11), -40), (_at(19, 31, 26, 11), -40), (_at(3, 1, 53), -26),
        (_at(4, 58, 28), -40),
    )
]


def test_monday_board_proves_days_from_readings_during_the_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The 12 October 2026 Monday Reset. NEVER has not played this Season:
    every profile, at 04:50, at the Reset and raised at 05:29, shows 5,000
    and names Season 0. EXACT ends at exactly 5,000 and BELOW at 4,940, both
    read at 04:50 after their last battle landed; BELOW's Reset reading
    already shows the raise. AFTER, raised by its Reset reading too, ends at
    4,970 and was read at that total at 03:00, but an attack and a defense
    of 30 came after it: that reading proves nothing. LL02YLGG is
    #LL02YLGG's real Sunday: its 03:05:53 reading of 5,036 came 4 minutes
    after a defense, so the battles after it are not proven, and its Reset
    reading at 05:04:56 matched while its 04:58:28 defense may still have
    been landing. In production that attack ended at 05:00:41, so 5,036 was
    its total at 05:00:00; its attacker's report is not saved here, so the
    defense counts: 4,996, where the board once showed the 03:05:53 reading."""
    saturday, sunday, monday = day(6), day(7), day(8)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       NEVER, EXACT, LL02YLGG, BELOW, AFTER)
        monkeypatch.setattr(scenarios, "READ_AFTER", timedelta(minutes=4, seconds=56))
        # From 5,300 each Saturday ends at 5,140; EXACT's at 5,160, and
        # LL02YLGG's, from 5,159, at 4,999.
        for tag in (EXACT, LL02YLGG, BELOW, AFTER):
            story.players[tag].battles = _saturday(saturday)
        story.players[EXACT].battles += attacks(saturday + timedelta(hours=13), 20)
        story.players[EXACT].battles += defenses(sunday, *[20] * 8)
        story.players[LL02YLGG].battles += LL02YLGG_SUNDAY
        story.players[BELOW].battles += (
            attacks(sunday, 40) + defenses(sunday, *[30] * 8))
        story.players[AFTER].battles += (
            attacks(sunday, 40) + defenses(sunday, *[30] * 7)
            + [Battle(_at(3, 30, 0), 30, True), Battle(_at(4, 0, 0), -30, False)])
        story.reset(saturday, _sweep({EXACT: 5300, LL02YLGG: 5159, BELOW: 5300, AFTER: 5300}))
        story.reset(sunday, _sweep({EXACT: 5160, LL02YLGG: 4999, BELOW: 5140, AFTER: 5140}))
        story.reads(_at(3, 0, 0), {AFTER: 4970})
        story.reads(_at(3, 5, 53), {LL02YLGG: 5036})
        story.reads(monday - timedelta(minutes=10), {EXACT: 5000, BELOW: 4940})
        story.reads(monday - timedelta(minutes=10), {NEVER: 5000}, None)
        story.reset(monday, _sweep({EXACT: 5000, LL02YLGG: 4996, BELOW: 5000, AFTER: 5000}))
        raised = _at(5, 29, 7)
        story.reads(raised, dict.fromkeys((EXACT, LL02YLGG, BELOW, AFTER), 5000))
        story.reads(raised, {NEVER: 5000}, None)
        story.wait(timedelta(hours=1))
        sundays = {tag: story.saved_day(tag, sunday) for tag in story.players}
        unfinished = story.unfinished_jobs()
        members = story.board_members(monday)
        board = story.served_board(monday)

    assert unfinished == []
    assert {tag: saved["state"] for tag, saved in sundays.items()} == dict.fromkeys(
        story.players, "Complete")
    # No accepted profile names NEVER's Season, yet the Season rule ranks it.
    assert members == {**dict.fromkeys(story.players, "complete"), NEVER: "missing"}
    assert _ranked(board) == {
        NEVER: 5000, EXACT: 5000, LL02YLGG: 4996, AFTER: 4970, BELOW: 4940}
    labels = {entry["tag"]: entry["confidence"] for entry in board["entries"]}
    assert labels == {
        NEVER: "confirmed", EXACT: "confirmed", BELOW: "confirmed",
        LL02YLGG: "uncertain", AFTER: "uncertain"}
