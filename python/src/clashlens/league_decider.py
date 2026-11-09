"""Decide marked weekly league records and update their dependent days."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from typing import Any

from psycopg import errors

from . import league_days
from .db import RESET_LOCK_WAIT
from .domain import SEASON_DURATION, ranked_day_for
from .league_week import WEEK, decide_season, load_season

# The worker decides on a thread of its own, without pause while marks wait,
# so a Monday Reset's marks are decided within minutes of being saved and
# the Reset publication checks never wait behind it; with nothing marked it
# looks again every IDLE_SECONDS. Each call decides for at most TICK_SECONDS
# so the thread notices the worker stopping.
TICK_SECONDS = 2.0
IDLE_SECONDS = 2.0
BATCH = 200
# A player whose lock was busy is skipped for BUSY_SECONDS, then tried after
# every player not found busy, so they hold up no one else.
BUSY_SECONDS = 10.0
RETRY_SECONDS = 60
REPORT_SECONDS = 60
_retry_at = float("-inf")
_report_at = float("-inf")
_busy: dict[int, float] = {}
_tally: dict[str, float] = {"decided": 0, "changed": 0, "marks": 0, "skipped": 0, "seconds": 0.0}


def _now() -> datetime:
    return datetime.now(UTC)


def decide_until(database: Any, stop: Event) -> None:
    """The worker's league week thread: decide marked Seasons until ``stop``
    is set."""
    while not stop.is_set():
        if not decide_due(database):
            stop.wait(IDLE_SECONDS)


def decide_due(database: Any) -> bool:
    """Decide the marked players' Seasons for up to ``TICK_SECONDS``; whether
    any mark was found. Only the current and previous Season are decided
    again: raw responses older than that are pruned, so an older week's
    evidence is incomplete. A failure is reported and tried again after
    ``RETRY_SECONDS``; once a minute the work done is reported."""
    global _retry_at, _report_at
    started = monotonic()
    if started < _retry_at:
        return False
    deadline = started + TICK_SECONDS
    found = False
    try:
        while _decide_batch(database, deadline):
            found = True
            if monotonic() >= deadline:
                break
        if monotonic() >= _report_at and _tally["marks"]:
            _report_at = monotonic() + REPORT_SECONDS
            with database.pool.connection() as connection:
                waiting, oldest = connection.execute(
                    "SELECT count(*), min(marked_at) FROM league_week_dirty"
                    " WHERE failed_at IS NULL"
                ).fetchone()
            print(json.dumps({
                "event": "league_week", **_tally, "seconds": round(_tally["seconds"], 1),
                "waiting": waiting, "oldest_mark": oldest.isoformat() if oldest else None,
            }), flush=True)
            _tally.update(decided=0, changed=0, marks=0, skipped=0, seconds=0.0)
    except Exception as error:  # noqa: BLE001 - tried again after RETRY_SECONDS
        _retry_at = monotonic() + RETRY_SECONDS
        print(json.dumps({"event": "league_week", "status": "failed",
                          "error": type(error).__name__}), flush=True)
        return False
    return found


def _decide_batch(database: Any, deadline: float) -> bool:
    """Decide one batch of marked Seasons in one transaction; whether any
    mark was found. Marks are deleted in the transaction reading the
    evidence: evidence committed later leaves a new mark.

    Each player is decided on their own, so one cannot stop the rest. A
    player whose lock is busy, as a Reset's board's while it freezes, keeps
    their marks, is left out of the batches for ``BUSY_SECONDS``, then is
    picked after every player not found busy, until decided or set aside.
    In a test on 20 October 2026, ten such players at the head of the queue
    were tried first in every batch, so a player looked up after them waited
    as long as the lock was held. The batch query picks in this order: a
    batch of the oldest marks, ordered only once picked, still left the
    newer player out while more busy players were due again than a batch
    holds. One whose decision
    fails any other way is set aside: their marks stay, marked failed,
    until new evidence marks them again, and the failed-work alert counts
    them (collector_metrics)."""
    now = _now()
    oldest = ranked_day_for(now).season_start - SEASON_DURATION
    clock = monotonic()
    with database.pool.connection() as connection, connection.transaction():
        if not connection.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended('league-week-decider', 0))"
        ).fetchone()[0]:
            return False
        # A player whose week or Reset a day calculation holds is tried in
        # a later batch, so the batch's locks are not held while it waits.
        connection.execute("SELECT set_config('lock_timeout', %s, true)", (RESET_LOCK_WAIT,))
        marked = connection.execute(
            """
            SELECT DISTINCT player_id, week_start FROM (
                SELECT player_id, week_start FROM league_week_dirty
                WHERE failed_at IS NULL AND player_id <> ALL(%(skipped)s)
                ORDER BY player_id = ANY(%(busy)s), id LIMIT %(batch)s
            ) AS mark
            """,
            {"skipped": [player for player, due in _busy.items() if due > clock],
             "busy": list(_busy), "batch": BATCH},
        ).fetchall()
        for player_id, season_start in sorted(
            {(int(player), ranked_day_for(week).season_start) for player, week in marked},
            key=lambda pair: (pair[0] in _busy, pair),
        ):
            try:
                with connection.transaction():
                    _decide_player(database, connection, player_id, season_start, now, oldest)
                _busy.pop(player_id, None)
            except (errors.LockNotAvailable, errors.TransactionRollback, errors.QueryCanceled):
                _busy[player_id] = monotonic() + BUSY_SECONDS
            except Exception as error:  # noqa: BLE001 - set aside, the rest proceed
                _busy.pop(player_id, None)
                connection.execute(
                    """
                    UPDATE league_week_dirty SET failed_at = clock_timestamp()
                    WHERE player_id = %s AND week_start >= %s AND week_start < %s
                      AND failed_at IS NULL
                    """,
                    (player_id, season_start, season_start + SEASON_DURATION),
                )
                print(json.dumps({"event": "league_week", "status": "set_aside",
                                  "player_id": player_id, "error": type(error).__name__}),
                      flush=True)
            if monotonic() >= deadline:
                break
        return bool(marked)


def _decide_player(
    database: Any, connection: Any, player_id: int, season_start: datetime, now: datetime,
    oldest: datetime,
) -> None:
    """Decide one marked player's Season, deleting its marks."""
    sources = [row[0] for row in connection.execute(
        """
        DELETE FROM league_week_dirty
        WHERE player_id = %s AND week_start >= %s AND week_start < %s
        RETURNING source
        """,
        (player_id, season_start, season_start + SEASON_DURATION),
    ).fetchall()]
    _tally["marks"] += len(sources)
    if season_start < oldest:
        _tally["skipped"] += 1
        return
    started = monotonic()
    # Seeded weeks' days are the Season repair's to recalculate, with those
    # of live evidence marked before the seed's first decision: queueing
    # every week of every tracked player at deploy would flood the queue.
    changed = league_days.save_season(
        database, connection, player_id, season_start,
        decide_season(load_season(connection, player_id, season_start, now)),
        now, queue_days="seed" not in sources)
    next_season = season_start + SEASON_DURATION
    if next_season - WEEK in changed and next_season <= now:
        # The next Season's first week counts its early answers
        # against this Season's last.
        connection.execute(
            "INSERT INTO league_week_dirty (player_id, week_start, source)"
            " VALUES (%s, %s, 'decision')",
            (player_id, next_season),
        )
    _tally["decided"] += 1
    _tally["changed"] += len(changed)
    _tally["seconds"] += monotonic() - started
