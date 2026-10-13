"""End-to-end: a Monday Reset reading taken before the weekly raise landed.

On 12 October 2026 the game raised Legend I totals below 5,000 to 5,000
from about 05:20, after every Reset reading had been taken by 05:13. 4,136
Sunday days whose battles added up to that unraised reading were saved
Inconsistent and shown uncertain on the Monday board, and their Mondays
started from the unraised reading instead of 5,000. A reading that shows the
raise still cannot be followed by an unraised one. See docs/testing.md.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import domain_database, text
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    _ranked,
    _saturday,
    _story,
    attacks,
    day,
    defenses,
)

LATE_RAISE, LOSS_TOO, RAISE_UNDONE, LATE_CREDIT = "#2PP", "#8PP", "#9PP", "#PPP"


def _loss_states(connection_info: str, start) -> dict[str, str]:
    """Each player's newest saved automatic loss state for the day from ``start``."""
    with psycopg.connect(connection_info) as connection:
        return {
            text(tag): text(state)
            for tag, state in connection.execute(
                """
                SELECT DISTINCT ON (player.normalized_tag)
                       player.normalized_tag, day.automatic_defense_evidence_state
                FROM ranked_day_versions AS day
                JOIN players AS player ON player.id = day.player_id
                WHERE day.ranked_day_start = %s
                ORDER BY player.normalized_tag, day.version DESC
                """,
                (start,),
            ).fetchall()
        }


def test_monday_reset_reading_before_the_weekly_raise(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each Sunday ends below 5,000 and the Reset reading at 05:04 shows it
    unraised; the 05:20 check shows the raise. Sunday is Complete, on the
    Monday board as complete and proven, even when the last reading before
    the Reset still lacked a late attack's gain, and Monday starts at 5,000.
    A player read unraised again at 05:40, after the raise showed, stays
    Inconsistent.
    The days must have ended by the real clock, so this is the previous
    Season's Monday, 28 September 2026."""
    season = PREVIOUS_SEASON
    saturday, sunday, monday, tuesday = (day(number, season) for number in (20, 21, 22, 23))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch,
                       LATE_RAISE, LOSS_TOO, RAISE_UNDONE, LATE_CREDIT)
        # From 5,300 each Saturday ends at 5,140.
        for player in story.players.values():
            player.battles = _saturday(saturday)
        # 1 attack and 8 defenses: Sunday ends at 4,940, raised by 60.
        # Monday: 1 attack and 8 defenses from 5,000 end at 4,800.
        monday_battles = attacks(monday, 40) + defenses(monday, *[30] * 8)
        story.players[LATE_RAISE].battles += (
            attacks(sunday, 40) + defenses(sunday, *[30] * 8) + monday_battles)
        # 1 attack and 7 defenses: 4,970 before its loss of 30, then 4,940,
        # raised by 60. The Reset reading comes before both.
        story.players[LOSS_TOO].battles += (
            attacks(sunday, 40) + defenses(sunday, *[30] * 7) + monday_battles)
        story.players[RAISE_UNDONE].battles += attacks(sunday, 40) + defenses(sunday, *[30] * 8)
        # Its attack at 04:48 also ends Sunday at 4,940, but the 04:50
        # check does not show its gain yet: 4,900.
        story.players[LATE_CREDIT].battles += defenses(sunday, *[30] * 8) + attacks(
            monday - timedelta(hours=1, minutes=12), 40)
        story.reset(saturday, {tag: (5300, season) for tag in story.players})
        story.reset(sunday, {tag: (5140, season) for tag in story.players})
        story.reads(monday - timedelta(minutes=10), {
            LATE_RAISE: 4940, LOSS_TOO: 4970, RAISE_UNDONE: 4940, LATE_CREDIT: 4900}, season)
        story.reset(monday, {LATE_RAISE: (4940, season), LOSS_TOO: (4970, season),
                             RAISE_UNDONE: (4940, season), LATE_CREDIT: (4940, season)})
        # The 05:20 settlement check: the raise has landed. RAISE_UNDONE then
        # reads unraised again, which no way the game works can show.
        story.reads(monday + timedelta(minutes=20), dict.fromkeys(story.players, 5000), season)
        story.reads(monday + timedelta(minutes=40), {RAISE_UNDONE: 4940}, season)
        # The board's first build came before these checks; the days they
        # change join it at the hourly rebuild.
        story.wait(timedelta(hours=1))
        sundays = {tag: story.saved_day(tag, sunday) for tag in story.players}
        loss_states = _loss_states(connection_info, sunday)
        monday_members = story.board_members(monday)
        monday_board = story.served_board(monday)
        story.reads(tuesday - timedelta(minutes=10), {LATE_RAISE: 4800, LOSS_TOO: 4800}, season)
        story.reset(tuesday, {LATE_RAISE: (4800, season), LOSS_TOO: (4800, season)})
        mondays = {tag: story.saved_day(tag, monday) for tag in (LATE_RAISE, LOSS_TOO)}
        unfinished = story.unfinished_jobs()
        tuesday_members = story.board_members(tuesday)
        tuesday_board = story.served_board(tuesday)

    assert unfinished == []
    sunday_day = {
        "state": "Complete", "start": 5140, "end": 4940, "next_start": 5000,
        "automatic_loss": None, "raise": 60, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert sundays[LATE_RAISE] == sundays[LATE_CREDIT] == sunday_day
    assert sundays[LOSS_TOO] == {**sunday_day, "automatic_loss": 30, "defenses": 7}
    # The raise hides the end, so no 5,000 reading proves the loss.
    assert loss_states[LOSS_TOO] == "calculated"
    assert sundays[RAISE_UNDONE]["state"] == "Inconsistent"
    monday_day = {
        "state": "Complete", "start": 5000, "end": 4800, "next_start": 4800,
        "automatic_loss": None, "raise": 0, "attacks": 1, "defenses": 8, "residual": 0,
    }
    assert mondays == {LATE_RAISE: monday_day, LOSS_TOO: monday_day}
    assert monday_members == {
        LATE_RAISE: "complete", LOSS_TOO: "complete", RAISE_UNDONE: "inconsistent",
        LATE_CREDIT: "complete"}
    assert tuesday_members == {LATE_RAISE: "complete", LOSS_TOO: "complete"}
    # The Monday board ranks each total at 05:00:00, before the loss and raise.
    assert _ranked(monday_board) == {
        LOSS_TOO: 4970, LATE_RAISE: 4940, RAISE_UNDONE: 4940, LATE_CREDIT: 4940}
    # The Reset reading matched each total before the raise, so it proves it.
    assert {entry["tag"]: entry["confidence"] for entry in monday_board["entries"]} == {
        LATE_RAISE: "confirmed", LOSS_TOO: "confirmed", RAISE_UNDONE: "uncertain",
        LATE_CREDIT: "confirmed"}
    assert _ranked(tuesday_board) == {LATE_RAISE: 4800, LOSS_TOO: 4800}
