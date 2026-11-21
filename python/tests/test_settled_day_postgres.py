"""End-to-end: a settled day is not recalculated for a reading alone.

On 10 November 2026, 10,090 of about 13,070 players got a new day result
after the first board and the calculated end changed for 1: every profile
or battle log processed, every unchanged battle-log check, the twice-daily
recheck and the day-end recalculation worked out each player's ended day
again, with 39,727 jobs overdue at 05:30 the next morning. A day whose
newest result is Complete, with its Reset total proven and its automatic
loss confirmed or not applicable, is now left alone by readings taken after
the first hour past its Reset; its battles still reach it. See
docs/testing.md.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
from collector_db_postgres_support import _handoff
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

from clashlens import queue_refresh, reconciliation_db
from clashlens.collector_db import CollectorDatabase
from clashlens.db import Database
from clashlens.domain import ranked_day_for

SETTLED, UNPROVEN = "#2UU", "#8UU"


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 7, hour, minute, tzinfo=UTC)


def test_a_settled_day_is_recalculated_only_for_its_battles(
    database_url: str, archive_server, monkeypatch
) -> None:
    """Each day starts at 5,000, gains 80 over 2 attacks and loses 80 over 8
    defenses, which leave no automatic loss: 5,000. SETTLED's Reset reading
    at 05:04 proves it. UNPROVEN, read at 04:50, attacks for 30 at 04:55
    and its Reset profile never answers, so its 5,030 is uncertain.

    - From 07:10, SETTLED is read at 5,000 five times, each with its battle
      log a minute later, its unchanged log is checked twice, the
      twice-daily recheck runs and its day-end recalculation falls due: its
      day is not worked out again and no recalculation is queued for it.
      UNPROVEN's 07:10 reading still reaches its day and proves 5,030.
    - SETTLED's attack for 30 at 04:58 first shows in its battle log at
      13:01: its day is worked out again and the board shows 5,030."""
    start, end = day(2), day(3)
    tags = (SETTLED, UNPROVEN)
    calculated: list[tuple[int, datetime, str]] = []
    calculate = reconciliation_db.recalculate_ranked_day

    def counted(database, connection, **day):
        calculated.append((day["player_id"], day["day_start"], day["trigger"]))
        return calculate(database, connection, **day)

    monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", counted)
    with domain_database(database_url) as connection_info:
        story = _story(connection_info, archive_server, monkeypatch, *tags)
        for tag in tags:
            story.players[tag].battles = [*attacks(start, 40, 40), *defenses(start, *[10] * 8)]
        story.players[UNPROVEN].battles.append(Battle(_at(4, 55), 30, True))
        story.reset(start, dict.fromkeys(tags, (5000, SEASON)))
        story.reads(_at(4, 50), {UNPROVEN: 5000})
        story.reset(end, {SETTLED: (5000, SEASON), UNPROVEN: (None, SEASON)})
        first = _entries(story.served_board(end))
        with psycopg.connect(connection_info) as connection:
            ids = dict(connection.execute(
                "UPDATE players SET active = true WHERE normalized_tag = ANY(%s)"
                " RETURNING normalized_tag, id", (list(tags),)).fetchall())
        settled_day = (ids[SETTLED], start)
        queued_before = _queued(connection_info, ids[SETTLED], start)
        del calculated[:]

        # Five readings, each with its battle log a minute later.
        story.reads(_at(7, 10), {SETTLED: 5000, UNPROVEN: 5030})
        story.run_worker()
        for hour in range(8, 12):
            story.reads(_at(hour, 10), {SETTLED: 5000})
            story.run_worker()
        # Two unchanged checks of the 11:11 log, each covering a reading.
        _check_unchanged(connection_info, story, ids[SETTLED], _at(11, 11),
                         [_at(11, 30), _at(11, 50)])
        # The twice-daily recheck, then the day-end recalculation.
        database = Database(connection_info)
        try:
            rechecked = queue_refresh.queue_recheck(database, "test", end)
            story.run_worker()
            with database.pool.connection() as connection:
                reconciliation_db._enqueue_day_end_reconciliation(
                    connection, ids[SETTLED], ranked_day_for(start))
        finally:
            database.close()
        story.wait(timedelta(hours=1))
        queued = _queued(connection_info, ids[SETTLED], start) - queued_before
        settled_calculations = sorted(item[2] for item in calculated if item[:2] == settled_day)
        unproven_calculations = [item for item in calculated
                                 if item[:2] == (ids[UNPROVEN], start)]
        settled = _entries(story.served_board(end))

        story.players[SETTLED].battles.append(Battle(_at(4, 58), 30, True))
        story.reads(_at(13, 0), {SETTLED: 5030})
        story.wait(timedelta(hours=1))
        late = _entries(story.served_board(end))
        late_calculations = [item[2] for item in calculated if item[:2] == settled_day]
        unfinished = story.unfinished_jobs()

    assert unfinished == []
    assert first == {SETTLED: (5000, "confirmed"), UNPROVEN: (5030, "uncertain")}
    assert (queued, settled_calculations) == (0, [])
    assert rechecked > 0 and unproven_calculations
    assert settled == {SETTLED: (5000, "confirmed"), UNPROVEN: (5030, "confirmed")}
    assert late_calculations and "evidence" in late_calculations
    assert late == {SETTLED: (5030, "confirmed"), UNPROVEN: (5030, "confirmed")}


def _check_unchanged(connection_info: str, story, player_id: int, log_at: datetime,
                     checks: list[datetime]) -> None:
    """The collector's unchanged checks at ``checks`` of the player's battle
    log saved at ``log_at``, each after a profile reading of 5,000 five
    minutes before it: they save no log, yet cover the reading. The worker
    runs after each."""
    with psycopg.connect(connection_info) as connection:
        log_id, digest = connection.execute(
            "SELECT id, response_hash FROM collector_observations"
            " WHERE player_id = %s AND endpoint = 'battle_log' AND response_completed_at = %s",
            (player_id, log_at)).fetchone()
        connection.execute(
            """
            INSERT INTO collector_response_state (
                scope, identity_key, endpoint, player_id, normalized_tag,
                last_response_hash, last_content_fingerprint, last_occurrence_key,
                last_applied_occurrence_key, last_seen_at, last_observation_id,
                last_success_at
            ) VALUES ('player', %s, 'battle_log', %s, %s, %s, %s, 'log', 'log', %s, %s, %s)
            """,
            (SETTLED, player_id, SETTLED, digest, digest, log_at, log_id, log_at),
        )
    collector = CollectorDatabase(connection_info)
    try:
        for at in checks:
            story.read(SETTLED, at - timedelta(minutes=5), 5000)
            assert collector.record_response(_handoff(
                occurrence_key=f"check-{at.isoformat()}", response_hash=digest,
                player_id=player_id, tag=SETTLED, endpoint="battle_log", completed_at=at,
            )).observation_id is None
            story.run_worker()
    finally:
        collector.close()


def _queued(connection_info: str, player_id: int, day_start: datetime) -> int:
    """How many recalculations of the player's day were ever queued, but
    for its day-end one."""
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE work_type = 'reconcile_ranked_day'"
            " AND input_json ->> 'player_id' = %s AND input_json ->> 'ranked_day_start' = %s"
            " AND input_json ->> 'trigger' IS DISTINCT FROM 'day_end'",
            (str(player_id), f"{day_start:%Y-%m-%dT%H:%M:%SZ}"),
        ).fetchone()[0]


def _entries(board: dict[str, object] | None) -> dict[str, tuple[int, str]]:
    _ranked(board)
    return {entry["tag"]: (entry["trophies"], entry["confidence"]) for entry in board["entries"]}
