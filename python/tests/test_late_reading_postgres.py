"""End-to-end: a reading long after the Reset can confirm a proven day but
not contradict it.

On 6 November 2026 #LGRUVPGGC's day was proven at 4,900 by its 05:23:22
reading. A reading at 13:58:13 fit nothing, 27 off, and made it
Inconsistent; recalculations then flipped it back and forth. The game's
automatic defense loss and weekly raise land within the first hour after a
Reset, so a reading after that hour, once a reading has proven the day,
can still confirm the day but no longer contradict it. See docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database
from reset_scenarios_postgres_support import (
    SEASON,
    Battle,
    _ranked,
    _story,
    attacks,
    day,
    defenses,
)

LATE = "#2QQ"


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 7, hour, minute, second, tzinfo=UTC)


def test_a_reading_hours_after_the_reset_cannot_contradict_a_proven_day(
    database_url: str, archive_server, monkeypatch
) -> None:
    """#LGRUVPGGC's shape. The day starts at 5,000, gains 80 over 2 attacks
    and loses 100 over 8 defenses, the last for 30 at 04:34, which leave no
    automatic loss: 4,980. Its 04:54:49 reading, 5,010, had not shown that
    defense yet and its Reset profile never answers, so the day is unproven
    and its entry uncertain until its 05:23:22 reading of 4,980 proves it.
    A 13:58:13 reading of 4,953 fits nothing: the day stays Complete, and
    its entry 4,980 and confirmed. A reading inside the first hour still
    contradicts a proven day (``test_board_later_reading_postgres.py``,
    AT_RESET)."""
    start, end = day(2), day(3)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, LATE)
        story.players[LATE].battles = [*attacks(start, 40, 40), *defenses(start, *[10] * 7),
                                       Battle(_at(4, 34), -30, False)]
        story.reset(start, {LATE: (5000, SEASON)})
        story.reads(_at(4, 54, 49), {LATE: 5010})
        story.reset(end, {LATE: (None, SEASON)})
        first = (_entries(story.served_board(end)), story.saved_day(LATE, start)["state"])
        story.reads(_at(5, 23, 22), {LATE: 4980})
        story.wait(timedelta(hours=1))
        proven = (_entries(story.served_board(end)), story.saved_day(LATE, start)["state"],
                  _versions(connection_info, end))
        story.reads(_at(13, 58, 13), {LATE: 4953})
        story.wait(timedelta(hours=1))
        late = (_entries(story.served_board(end)), story.saved_day(LATE, start),
                _versions(connection_info, end))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert first == ({LATE: (4980, "uncertain")}, "Partial")
    assert proven[:2] == ({LATE: (4980, "confirmed")}, "Complete")
    # The 13:58 reading publishes no new board.
    assert (late[0], late[2]) == ({LATE: (4980, "confirmed")}, proven[2])
    saved = late[1]
    assert saved == {
        "state": "Complete", "start": 5000, "end": 4980, "next_start": 4980,
        "automatic_loss": None, "raise": 0, "attacks": 2, "defenses": 8, "residual": 0,
    }


def _entries(board: dict[str, object] | None) -> dict[str, tuple[int, str]]:
    _ranked(board)
    return {entry["tag"]: (entry["trophies"], entry["confidence"]) for entry in board["entries"]}


def _versions(connection_info: str, boundary: datetime) -> int:
    """How many boards the Reset has published."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT count(DISTINCT snapshot_id) FROM boundary_publication_generations"
            " WHERE boundary_at = %s",
            (boundary,),
        ).fetchone()[0]
