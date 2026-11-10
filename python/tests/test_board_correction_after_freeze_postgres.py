"""End-to-end: a Daily board frozen without two players' days takes them,
and a later proof, in its correction.

On 8 November 2026 the board froze at 05:43 and published at 05:44. It
ranked #2Q2C2U8UL at its 03:53 reading, 4,899, uncertain: a defense for 21
three minutes before that reading may or may not have shown in it, and its
Reset reading of 4,859 did not prove the day's end. A second sighting of
that reading at 05:50 made the day Complete at 4,859 at 06:06. It also left
out #YUJQU0QQG and #RV0GJURQ, whose Reset battle logs saved at 05:17 but
waited in the worker until 05:45 and 05:48, after the board froze. Each new
day queued the board's correction. But each late Reset log put its player
back to waiting on the frozen board, whose army results then waited for
them for good; a correction starts only once both are published, so at
10:48 the board still showed 4,899 without the two. See docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from board_later_reading_postgres_support import _hold
from domain_test_support import domain_database, text
from reset_scenarios_postgres_support import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

from clashlens import board_deadline, queue_refresh

# SWEDE is #2Q2C2U8UL's day; LATE stands for the two players left out.
SWEDE, LATE, ON_TIME = "#2PP", "#8PP", "#9PP"


def _at(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 10, 7, hour, minute, second, tzinfo=UTC)


def _first_board(connection_info: str, boundary: datetime) -> dict[str, tuple]:
    """Each member of the Reset's first board: their trophies and
    confidence where it ranks them, else their frozen row's quality."""
    with psycopg.connect(connection_info) as connection:
        return {text(tag): (trophies, text(confidence)) if trophies else text(quality)
                for tag, trophies, confidence, quality in connection.execute(
            """
            SELECT player.normalized_tag, entry.trophies, entry.confidence,
                   frozen.input_identity ->> 'snapshot_quality'
            FROM boundary_publication_generations AS generation
            CROSS JOIN boundary_publication_manifest_entries(
                generation.snapshot_manifest_id) AS frozen
            JOIN players AS player ON player.id = frozen.player_id
            LEFT JOIN leaderboard_snapshot_entries AS entry
              ON entry.snapshot_id = generation.snapshot_id
             AND entry.player_id = frozen.player_id
            WHERE generation.boundary_at = %s AND generation.generation = 1
            """,
            (boundary,),
        )}


def test_a_board_frozen_without_late_days_takes_them_and_a_later_proof(
    database_url: str, archive_server, monkeypatch
) -> None:
    """SWEDE starts the Legend day ending 7 October 2026 at 4,873, as
    #2Q2C2U8UL did on 7 November, and has its 16 battles: 4,899 at its
    03:53:36 reading, after a defense for 21 at 03:49:59, then 4,859 after a
    defense for 40 at 04:53:04. LATE and ON_TIME each attack once for 40.
    LATE's Reset responses wait in the worker until the board has frozen
    without it at 05:30; SWEDE's Reset reading is seen again at 05:50:38."""
    start, end = day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, SWEDE, LATE, ON_TIME)
        # No reading rechecks a day until the board has frozen, as none did
        # on 8 November before 05:43.
        rechecks = {name: getattr(queue_refresh, name)
                    for name in ("queue_for_reading", "queue_for_battles")}
        for name in rechecks:
            monkeypatch.setattr(queue_refresh, name, lambda *_: None)
        story.players[SWEDE].battles = [
            *defenses(start, 40, 30, 40, 25, 19, 26),
            *attacks(start, 40, 30, 40, 24, 14, 27, 24, 28),
            Battle(_at(3, 49, 59), -21, False),
            Battle(_at(4, 53, 4), -40, False),
        ]
        for tag in (LATE, ON_TIME):
            story.players[tag].battles = attacks(start, 40)
        story.reset(start, {SWEDE: (4873, SEASON), LATE: (5100, SEASON),
                            ON_TIME: (5101, SEASON)})
        story.reads(_at(3, 53, 36), {SWEDE: 4899})
        story.reads(_at(4, 50, 0), {LATE: 5140, ON_TIME: 5141})
        story.reset(end, {SWEDE: (4859, SEASON), LATE: (5140, SEASON),
                          ON_TIME: (5141, SEASON)}, run=False)
        _hold(connection_info, (LATE,), end)
        # 05:30 has passed: the board publishes without the player waiting.
        monkeypatch.setattr(board_deadline, "BOARD_DEADLINE", timedelta(minutes=30))
        story.run_worker()
        first = _first_board(connection_info, end)
        frozen = story.saved_day(SWEDE, start)
        # LATE's day saved since: the board is rebuilt with it at once.
        arrived = story.served_board(end)

        for name, recheck in rechecks.items():
            monkeypatch.setattr(queue_refresh, name, recheck)
        story.read(SWEDE, _at(5, 50, 38), 4859)
        story.read(SWEDE, _at(5, 51, 38))
        story.wait(timedelta(hours=1))
        unfinished = story.unfinished_jobs()
        day_after = story.saved_day(SWEDE, start)
        corrected = story.served_board(end)

    assert (frozen["state"], frozen["end"]) == ("Partial", 4859)
    assert first == {
        ON_TIME: (5141, "confirmed"), SWEDE: (4899, "uncertain"), LATE: "unavailable"}
    assert _ranked(arrived) == {ON_TIME: 5141, LATE: 5140, SWEDE: 4899}
    assert unfinished == []
    assert (day_after["state"], day_after["end"]) == ("Complete", 4859)
    assert _ranked(corrected) == {ON_TIME: 5141, LATE: 5140, SWEDE: 4859}
    assert {entry["tag"]: entry["confidence"] for entry in corrected["entries"]} == {
        ON_TIME: "confirmed", LATE: "confirmed", SWEDE: "confirmed"}
