"""End-to-end: a Reset's board is rebuilt only when a member's row changes.

On 10 November 2026, 10,090 day results were saved again after the first
board and 1 changed its end, yet each one rebuilt the whole board and its
army records: 28 board versions in 9.5 hours. A day result saved again now
rebuilds the board only when what the board shows for its member changes,
and the army records only when the battles they read change. See
docs/testing.md.
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

SAVED_AGAIN, CONTRADICTED, UNPROVEN = "#2RR", "#8RR", "#9RR"


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 7, hour, minute, tzinfo=UTC)


def test_a_day_saved_again_rebuilds_only_what_its_row_changes(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each day starts at 5,000, gains 80 over 2 attacks and loses 80 over 8
    defenses, which leave no automatic loss: 5,000. SAVED_AGAIN's and
    CONTRADICTED's Reset readings at 05:04 prove it. UNPROVEN, read at 04:50,
    attacks for 30 at 04:55 and its Reset profile never answers, so its
    5,030 is uncertain until its 05:30 reading proves it.

    - The 05:30 proof changes UNPROVEN's mark, not its battles: the board is
      rebuilt once, its army records are kept.
    - SAVED_AGAIN is read at 5,000 five more times, an hour apart, and
      CONTRADICTED at 4,963 once, 37 lower than any automatic loss the rule
      expects, which makes its day Inconsistent. Each reading saves a day
      again with the same Reset total and mark: nothing is rebuilt.
    - SAVED_AGAIN's attack for 30 at 04:58 first shows in its battle log at
      13:01: its Reset total moves to 5,030, and the board and its army
      records are rebuilt exactly once more."""
    start, end = day(2), day(3)
    tags = (SAVED_AGAIN, CONTRADICTED, UNPROVEN)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in tags:
            story.players[tag].battles = [*attacks(start, 40, 40), *defenses(start, *[10] * 8)]
        story.players[UNPROVEN].battles.append(Battle(_at(4, 55), 30, True))
        story.reset(start, dict.fromkeys(tags, (5000, SEASON)))
        story.reads(_at(4, 50), {UNPROVEN: 5000})
        story.reset(end, {SAVED_AGAIN: (5000, SEASON), CONTRADICTED: (5000, SEASON),
                          UNPROVEN: (None, SEASON)})
        first = (_entries(story.served_board(end)), _versions(connection_info, end))
        story.reads(_at(5, 30), {UNPROVEN: 5030})
        story.wait(timedelta(hours=1))
        proven = (_entries(story.served_board(end)), _versions(connection_info, end))
        saved = _results(connection_info, start, SAVED_AGAIN)
        for hour in range(7, 12):
            story.reads(_at(hour, 10), {SAVED_AGAIN: 5000} if hour > 7 else {
                SAVED_AGAIN: 5000, CONTRADICTED: 4963})
            story.wait(timedelta(hours=1))
        saved_again = (_entries(story.served_board(end)), _versions(connection_info, end),
                       _results(connection_info, start, SAVED_AGAIN) - saved,
                       story.saved_day(CONTRADICTED, start)["state"])
        story.players[SAVED_AGAIN].battles.append(Battle(_at(4, 58), 30, True))
        story.reads(_at(13, 0), {SAVED_AGAIN: 5030})
        story.wait(timedelta(hours=1))
        late = (_entries(story.served_board(end)), _versions(connection_info, end))
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    first_board = {SAVED_AGAIN: (5000, "confirmed"), CONTRADICTED: (5000, "confirmed"),
                   UNPROVEN: (5030, "uncertain")}
    board = {**first_board, UNPROVEN: (5030, "confirmed")}
    entries, versions, results, contradicted = saved_again
    # Each reading saved a day again.
    assert (results, contradicted) == (5, "Inconsistent")
    # Published (boards, army records): UNPROVEN's mark changed, its battles
    # did not, so a new board and the same armies. The days saved again
    # change nothing. A late battle changes both.
    assert {"first": first, "proven": proven, "saved again": (entries, versions),
            "late battle": late} == {
        "first": (first_board, (1, 1)), "proven": (board, (2, 1)),
        "saved again": (board, (2, 1)),
        "late battle": ({**board, SAVED_AGAIN: (5030, "confirmed")}, (3, 2))}


def _entries(board: dict[str, object] | None) -> dict[str, tuple[int, str]]:
    _ranked(board)
    return {entry["tag"]: (entry["trophies"], entry["confidence"]) for entry in board["entries"]}


def _versions(connection_info: str, boundary: datetime) -> tuple[int, int]:
    """How many boards and army records the Reset has published."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT count(DISTINCT snapshot_id), count(DISTINCT army_publication_id)
            FROM boundary_publication_generations
            WHERE boundary_at = %s
            """,
            (boundary,),
        ).fetchone()


def _results(connection_info: str, start: datetime, tag: str) -> int:
    """How many results the player's day from ``start`` has saved."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT count(*) FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            WHERE day.ranked_day_start = %s AND player.normalized_tag = %s
            """,
            (start, tag),
        ).fetchone()[0]
