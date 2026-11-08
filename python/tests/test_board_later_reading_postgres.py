"""End-to-end: a reading after the Reset proves the battles an older
reading left unproven.

On 31 October 2026 the Daily board ranked #CLU28LR2 at 4,924, its 02:54:19
reading, instead of 5,233. A defense stamped 02:51:16, 3 minutes before
that reading, may or may not have shown in it, so the 8 attacks after it,
309 trophies, did not count either. Its Reset reading lacked their credits
too, so its day stayed Partial, but its 05:23:18 reading showed 5,233: the
reading plus the 8 attacks. See docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database
from test_reset_scenarios_postgres import SEASON, Battle, _ranked, _story, day, defenses

from clashlens import queue_refresh

# PROVEN is #CLU28LR2's numbers. EARLY's 02:54:19 reading came before that
# defense showed, so the reading plus the attacks is 26 too many: its later
# reading proves the other way of reading it, which the board cannot rank
# from that reading. NEW_DAY attacks for 30 at 05:10, before its later reading.
# SLOW, read at 03:00 after that defense showed, has its Reset responses
# saved last, as #CLU28LR2's board waited for other players' until 05:43.
PROVEN, EARLY, NEW_DAY, SLOW = "#2PP", "#8PP", "#9PP", "#PPP"
LATE = (PROVEN, EARLY, NEW_DAY)


def _at(hour: int, minute: int, second: int) -> datetime:
    return datetime(2026, 10, 7, hour, minute, second, tzinfo=UTC)


def _hold(connection_info: str, tags: tuple[str, ...], since: datetime) -> None:
    """Leave the players' responses from ``since`` for the worker's next
    round: it saves the others first."""
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            UPDATE python_processing_jobs AS job SET due_at = now() + interval '1 day'
            FROM collector_observations AS observation
            WHERE observation.id = job.observation_id
              AND observation.normalized_tag = ANY(%s)
              AND observation.response_completed_at >= %s
            """,
            (list(tags), since),
        )


def test_a_later_reading_proves_the_battles_after_an_older_one(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each player starts the Legend day ending 7 October 2026 at 5,202,
    loses 278 over 8 defenses, the last for 26 at 02:51:16, and gains 309
    over 8 attacks from 04:35:53 to 04:56:53: 5,233. The Reset readings at
    05:04 still lack every attack's credit, 4,924; SLOW's shows 5,233. The
    worker saves the three days, then SLOW's Reset responses and the
    readings at 05:23:18, then builds the board. On 31 October it had not
    yet rechecked #CLU28LR2's day with the evidence saved after the Reset
    when the board froze, nor by 07:36: here it never does."""
    start, end = day(2), day(3)
    battles = [
        *defenses(start, *[40] * 6, 12),
        Battle(_at(2, 51, 16), -26, False),
        *(Battle(_at(4, 35, 53) + timedelta(minutes=3 * n), 40 if n < 7 else 29, True)
          for n in range(8)),
    ]
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *LATE, SLOW)
        for recheck in ("queue_for_reading", "queue_for_battles"):
            monkeypatch.setattr(queue_refresh, recheck, lambda *_: None)
        for tag in story.players:
            story.players[tag].battles = list(battles)
        story.players[NEW_DAY].battles.append(Battle(end + timedelta(minutes=10), 30, True))
        story.reset(start, dict.fromkeys(story.players, (5202, SEASON)))
        story.reads(_at(2, 54, 19), {PROVEN: 4924, EARLY: 4950, NEW_DAY: 4924})
        story.reads(_at(3, 0, 0), {SLOW: 4924})
        story.reset(end, {**dict.fromkeys(LATE, (4924, SEASON)), SLOW: (5233, SEASON)},
                    run=False)
        _hold(connection_info, (SLOW,), end)
        story.reads(_at(5, 23, 18), {PROVEN: 5233, EARLY: 5233, NEW_DAY: 5263})
        _hold(connection_info, LATE, _at(5, 23, 18))
        story.run_worker()
        days = {tag: story.saved_day(tag, start) for tag in LATE}
        unfinished = story.unfinished_jobs()
        board = story.served_board(end)

    assert unfinished == []
    # Each day's own readings leave its end unproven: the board ranks a reading.
    assert {tag: (days[tag]["state"], days[tag]["end"]) for tag in LATE} == dict.fromkeys(
        LATE, ("Partial", 5233))
    assert _ranked(board) == {SLOW: 5233, PROVEN: 5233, NEW_DAY: 5233, EARLY: 4950}
    labels = {entry["tag"]: entry["confidence"] for entry in board["entries"]}
    # SLOW's 03:00 reading proves the battles after it, but no reading
    # proves the day's end: its Reset reading came while its last attack landed.
    assert labels == {
        SLOW: "uncertain", PROVEN: "confirmed", NEW_DAY: "confirmed", EARLY: "uncertain"}
