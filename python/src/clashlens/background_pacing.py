"""When background work may start.

Background work is every job at backfill priority: army re-decodes, Season
repair and day-end recalculations, first battle logs and league histories.
On 9 Oct 2026 six re-decodes each held about 103 battle locks for 5 to 14
seconds, 568 jobs were leased at once beside Season repair, and live
responses fell to about 8,000 players over 2 minutes late. So however many
worker processes run, at most ``BACKGROUND_JOB_LIMIT`` background jobs hold
a lease at once, and none starts while live data is behind. Background work
claims come last, so it still runs whenever live work leaves room.

A day's recheck after new evidence (``queue_refresh``) is background work in
its own lane, at ``DAY_RECHECK_PRIORITY``, with its own limit: from 16:41 on
9 Oct 2026 about 530 a minute were queued, two at a time finished about 350,
and Season repair would have waited behind them, or they behind it.

Both limits are settings, so more can run while live work keeps up, and each
halves, rounded up, as soon as live work strains: live jobs waiting
``LIVE_STRAIN_SECONDS``, about twice the 14 seconds 9 in 10 live responses
waited on 9 Oct 2026, or a worker statement waiting ``LOCK_STRAIN_SECONDS``
on a lock. Live work is still claimed first, and background work fills only
what the claim has left, each kind up to its own room, the kind using less
of its limit first, ties going at random in proportion to the limits, so a
worker with one free background thread still runs both. A claim that found
no live work waits up to ``PERMIT_WAIT`` for another claim's permit. On 9 Oct 2026 a claim gave up at
once, took one background job and the older kind first, and its thread slept
the 1 second poll: raising the limits from 2 and 4 to 6 and 6 cut background
work from about 217 jobs a minute to 178, with rechecks averaging 0.05
running against 2.6 other background jobs.

Live work is behind, or strains, when enough of it has waited that long:
at least ``LIVE_LAG_MIN_JOBS`` jobs and ``LIVE_LAG_SHARE`` of the live work
waiting, counting at most ``LIVE_COUNT_CAP`` jobs in each waiting state, so
a count stays cheap during a Reset. One late job is not enough: from 16:00
to 18:00 on 9 Oct 2026 about 120 saved responses waited 2 to 7 minutes for
their first try while 9 in 10 waited under 14 seconds, and stopping for any
one of them stopped background work 32% of the time. Replayed every 5
seconds, this rule stops it 0.6% of that time and halves it 3.2%, and still
stops it 39% of 05:00 to 06:20, when up to 21,800 live jobs waited.
"""

from __future__ import annotations

import os
import random
from typing import Any

from psycopg import ClientCursor
from psycopg.errors import LockNotAvailable

BACKGROUND_PERMIT_KEY = "background-work-permit"
# Under the second a worker statement may wait on a lock before live work
# counts as strained, so waiting for the permit never halves the limits.
PERMIT_WAIT = "500ms"
BACKGROUND_JOB_LIMIT = int(os.environ.get("CLASHLENS_BACKGROUND_JOB_LIMIT", "2"))
DAY_RECHECK_PRIORITY = 26
DAY_RECHECK_JOB_LIMIT = int(os.environ.get("CLASHLENS_DAY_RECHECK_JOB_LIMIT", "4"))
# No background job starts while enough live responses and daily results have
# been due this long, well before the website's delayed-updates notice at 15 minutes.
LIVE_LAG_PAUSE_SECONDS = 120
LIVE_STRAIN_SECONDS = 30
LIVE_LAG_MIN_JOBS = 5
LIVE_LAG_SHARE = 0.05
LIVE_COUNT_CAP = 200
LOCK_STRAIN_SECONDS = 1
LIVE_WORK_TYPES = ("process_observation", "replay_observation", "reconcile_ranked_day")


def background_lanes(
    connection: Any, jobs_relation: str, denormalized_contract: bool,
    supports_coordinator: bool, supports_dependency: bool, *, wait: bool = False,
) -> list[tuple[int, int]]:
    """Each background priority with room, and how many more jobs it may
    lease, the one using less of its limit first, ties at random in proportion
    to the limits; with any, this claim holds the permit. With ``wait`` it
    waits up to ``PERMIT_WAIT`` for the permit.

    Only live work this worker can claim counts, so a newer contract's never
    pauses background work for good. Late live work counts while it waits,
    under each waiting state's own attempt rule and claim index. A leased job,
    live or background, counts until it leaves its lease, even past expiry or
    on its last attempt: its transaction can still be running, and queue
    maintenance moves dead ones out. The lease count is its own statement, so
    it reads every background claim committed before this one took the permit.
    """
    from .db import (
        PYTHON_BACKFILL_PRIORITY,
        PYTHON_LIVE_PRIORITY,
        PYTHON_RESET_PRIORITY,
        _supported_claim_filter,
    )

    supported_filter, params = _supported_claim_filter(
        "job", "source_observation", denormalized_contract=denormalized_contract,
        supports_coordinator=supports_coordinator,
    )
    tries = "job.attempt_count < job.max_attempts"

    def live_waiting(seconds: str) -> str:
        return " + ".join(f"""(SELECT count(*) FROM (
            SELECT FROM {jobs_relation} AS job
            LEFT JOIN collector_observations AS source_observation
                ON source_observation.id = COALESCE(job.observation_id, job.replay_observation_id)
            WHERE job.priority = ANY(%(live_priorities)s::integer[]) AND {state}
              AND job.due_at <= statement_timestamp() - %({seconds})s * interval '1 second'
              AND job.work_type = ANY(%(live_work_types)s::text[])
              AND {supported_filter}
            LIMIT %(live_cap)s) AS capped)""" for state in (
            f"job.state IN ('pending', 'waiting_retry') AND {tries}",
            "job.state = 'waiting_dependency'" + ("" if supports_dependency else f" AND {tries}"),
            "job.state = 'leased'",
        ))
    if not _take_permit(connection, wait):
        return []
    # Only the worker's own sessions show what they wait on. The lock table,
    # which says when each wait began, is read only once one of them has run
    # that long and is waiting on a lock.
    since = "statement_timestamp() - %(lock_strain)s * interval '1 second'"
    # Its values never change, so they are written into it: from its sixth
    # run on a connection psycopg prepares it and PostgreSQL keeps its plan,
    # 0.4 ms a run instead of about 15 ms planning it with the permit held.
    bulk, rechecks, waiting, strained, late, lock_wait = connection.execute(ClientCursor(
        connection).mogrify(f"""
        SELECT count(*) FILTER (WHERE priority = %(backfill_priority)s),
               count(*) FILTER (WHERE priority = %(recheck_priority)s),
               {live_waiting("live_due")}, {live_waiting("live_strain")},
               {live_waiting("live_lag_pause")},
               CASE WHEN EXISTS (
                   SELECT FROM pg_stat_activity
                   WHERE datname = current_database() AND usename = current_user
                     AND wait_event_type = 'Lock' AND query_start <= {since})
               THEN EXISTS (
                   SELECT FROM pg_locks AS blocked
                   JOIN pg_stat_activity AS session ON session.pid = blocked.pid
                   WHERE session.datname = current_database()
                     AND session.usename = current_user
                     AND NOT blocked.granted AND blocked.waitstart <= {since})
               ELSE false END
        FROM {jobs_relation} WHERE state = 'leased'
        """,
        {
            **params,
            "backfill_priority": PYTHON_BACKFILL_PRIORITY,
            "recheck_priority": DAY_RECHECK_PRIORITY,
            "live_priorities": [PYTHON_LIVE_PRIORITY, PYTHON_RESET_PRIORITY],
            "live_due": 0,
            "live_lag_pause": LIVE_LAG_PAUSE_SECONDS,
            "live_strain": LIVE_STRAIN_SECONDS,
            "live_cap": LIVE_COUNT_CAP,
            "lock_strain": LOCK_STRAIN_SECONDS,
            "live_work_types": list(LIVE_WORK_TYPES),
        },
    )).fetchone()
    threshold = max(LIVE_LAG_MIN_JOBS, LIVE_LAG_SHARE * waiting)
    strained = strained >= threshold or lock_wait
    lanes = sorted((leased / cap, -random.random() ** (1 / cap), priority, cap - leased)
                   for priority, leased, limit in (
        (PYTHON_BACKFILL_PRIORITY, bulk, BACKGROUND_JOB_LIMIT),
        (DAY_RECHECK_PRIORITY, rechecks, DAY_RECHECK_JOB_LIMIT),
    ) for cap in [(limit + 1) // 2 if strained else limit] if leased < cap)
    return [] if late >= threshold else [(priority, room) for *_, priority, room in lanes]


def _take_permit(connection: Any, wait: bool) -> bool:
    """Take the permit for this transaction; with ``wait``, wait up to
    ``PERMIT_WAIT`` for the claim that holds it to commit."""
    from .db import lock_wait

    take = "SELECT pg_{}advisory_xact_lock(hashtextextended(%s, 0))"
    if connection.execute(take.format("try_"), (BACKGROUND_PERMIT_KEY,)).fetchone()[0]:
        return True
    if not wait:
        return False
    try:
        # The savepoint keeps a timed-out wait from ending the claim.
        with connection.transaction(), lock_wait(connection, PERMIT_WAIT):
            connection.execute(take.format(""), (BACKGROUND_PERMIT_KEY,))
    except LockNotAvailable:
        return False
    return True
