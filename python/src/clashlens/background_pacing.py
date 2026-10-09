"""When background work may start.

Background work is every job at backfill priority: army re-decodes, Season
repair and day-end recalculations, first battle logs and league histories.
On 9 Oct 2026 six re-decodes each held about 103 battle locks for 5 to 14
seconds, 568 jobs were leased at once beside Season repair, and live
responses fell to about 8,000 players over 2 minutes late. So however many
worker processes run, at most ``BACKGROUND_JOB_LIMIT`` background jobs hold
a lease at once, and none starts while live data waits. Background work
claims come last, so it still runs whenever live work leaves room.

A day's recheck after new evidence (``queue_refresh``) is background work in
its own lane, at ``DAY_RECHECK_PRIORITY``, with its own limit: from 16:41 on
9 Oct 2026 about 530 a minute were queued, two at a time finished about 350,
and Season repair would have waited behind them, or they behind it.

Both limits are settings, so more can run while live work keeps up, and each
halves, rounded up, as soon as live work strains: a live job waiting
``LIVE_STRAIN_SECONDS``, about twice the 14 seconds 9 in 10 live responses
waited on 9 Oct 2026, or a worker statement waiting ``LOCK_STRAIN_SECONDS``
on a lock. Live work is still claimed first, and a claim takes at most one
background job.
"""

from __future__ import annotations

import os
from typing import Any

BACKGROUND_PERMIT_KEY = "background-work-permit"
BACKGROUND_JOB_LIMIT = int(os.environ.get("CLASHLENS_BACKGROUND_JOB_LIMIT", "2"))
DAY_RECHECK_PRIORITY = 26
DAY_RECHECK_JOB_LIMIT = int(os.environ.get("CLASHLENS_DAY_RECHECK_JOB_LIMIT", "4"))
# No background job starts while a live response or daily result has been
# due this long, well before the website's delayed-updates notice at 15 minutes.
LIVE_LAG_PAUSE_SECONDS = 120
LIVE_STRAIN_SECONDS = 30
LOCK_STRAIN_SECONDS = 1
LIVE_WORK_TYPES = ("process_observation", "replay_observation", "reconcile_ranked_day")


def background_lanes(
    connection: Any, jobs_relation: str, denormalized_contract: bool,
    supports_coordinator: bool, supports_dependency: bool,
) -> tuple[int, ...]:
    """The background priorities with room for this claim to lease one job;
    with any, it then holds the permit.

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
        return "(" + " OR ".join(f"""EXISTS (
            SELECT FROM {jobs_relation} AS job
            LEFT JOIN collector_observations AS source_observation
                ON source_observation.id = COALESCE(job.observation_id, job.replay_observation_id)
            WHERE job.priority = ANY(%(live_priorities)s::integer[]) AND {state}
              AND job.due_at <= statement_timestamp() - %({seconds})s * interval '1 second'
              AND job.work_type = ANY(%(live_work_types)s::text[])
              AND {supported_filter})""" for state in (
            f"job.state IN ('pending', 'waiting_retry') AND {tries}",
            "job.state = 'waiting_dependency'" + ("" if supports_dependency else f" AND {tries}"),
            "job.state = 'leased'",
        )) + ")"
    if not connection.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
        (BACKGROUND_PERMIT_KEY,),
    ).fetchone()[0]:
        return ()
    # Only the worker's own sessions show what they wait on.
    bulk, rechecks, late, strained = connection.execute(
        f"""
        SELECT count(*) FILTER (WHERE priority = %(backfill_priority)s),
               count(*) FILTER (WHERE priority = %(recheck_priority)s),
               {live_waiting("live_lag_pause")},
               {live_waiting("live_strain")} OR EXISTS (
                   SELECT FROM pg_stat_activity
                   WHERE datname = current_database() AND usename = current_user
                     AND wait_event_type = 'Lock'
                     AND query_start <= statement_timestamp() - %(lock_strain)s * interval '1 second')
        FROM {jobs_relation} WHERE state = 'leased'
        """,
        {
            **params,
            "backfill_priority": PYTHON_BACKFILL_PRIORITY,
            "recheck_priority": DAY_RECHECK_PRIORITY,
            "live_priorities": [PYTHON_LIVE_PRIORITY, PYTHON_RESET_PRIORITY],
            "live_lag_pause": LIVE_LAG_PAUSE_SECONDS,
            "live_strain": LIVE_STRAIN_SECONDS,
            "lock_strain": LOCK_STRAIN_SECONDS,
            "live_work_types": list(LIVE_WORK_TYPES),
        },
    ).fetchone()
    return () if late else tuple(
        priority for priority, leased, limit in (
            (PYTHON_BACKFILL_PRIORITY, bulk, BACKGROUND_JOB_LIMIT),
            (DAY_RECHECK_PRIORITY, rechecks, DAY_RECHECK_JOB_LIMIT),
        ) if leased < ((limit + 1) // 2 if strained else limit)
    )
