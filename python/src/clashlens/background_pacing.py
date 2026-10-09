"""When background work may start.

Background work is every job at backfill priority: army re-decodes, Season
repair and day-end recalculations, first battle logs and league histories.
On 9 Oct 2026 six re-decodes each held about 103 battle locks for 5 to 14
seconds, 568 jobs were leased at once beside Season repair, and live
responses fell to about 8,000 players over 2 minutes late. So however many
worker processes run, at most ``BACKGROUND_JOB_LIMIT`` background jobs hold
a lease at once, and none starts while live data waits. Background work
claims come last, so it still runs whenever live work leaves room.
"""

from __future__ import annotations

from typing import Any

BACKGROUND_PERMIT_KEY = "background-work-permit"
BACKGROUND_JOB_LIMIT = 2
# No background job starts while a live response or daily result has been
# due this long; the website's delayed-updates notice reads the same work.
LIVE_LAG_PAUSE_SECONDS = 120
LIVE_WORK_TYPES = ("process_observation", "replay_observation", "reconcile_ranked_day")


def background_turn_free(
    connection: Any, jobs_relation: str, denormalized_contract: bool,
    supports_coordinator: bool, supports_dependency: bool,
) -> bool:
    """Whether this claim may lease one background job; it then holds the permit.

    Only live work this worker can claim counts, so a newer contract's never
    pauses background work for good. Late live work counts while it waits, is
    leased, or keeps a lease after a lock conflict refunded its attempt, under
    each state's own attempt rule and claim index. The lease count is its own
    statement, so it reads every background claim committed before this one
    took the permit.
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
    late_live = "(" + " OR ".join(f"""EXISTS (
        SELECT FROM {jobs_relation} AS job
        LEFT JOIN collector_observations AS source_observation
            ON source_observation.id = COALESCE(job.observation_id, job.replay_observation_id)
        WHERE job.priority = ANY(%(live_priorities)s::integer[]) AND {state}
          AND job.due_at <= statement_timestamp() - %(live_lag_pause)s * interval '1 second'
          AND job.work_type = ANY(%(live_work_types)s::text[])
          AND {supported_filter})""" for state in (
        f"job.state IN ('pending', 'waiting_retry') AND {tries}",
        "job.state = 'waiting_dependency'" + ("" if supports_dependency else f" AND {tries}"),
        f"job.state = 'leased' AND (job.lease_expires_at > statement_timestamp() OR {tries})",
    )) + ")"
    if not connection.execute(
        "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
        (BACKGROUND_PERMIT_KEY,),
    ).fetchone()[0]:
        return False
    return connection.execute(
        f"""
        SELECT (SELECT count(*) FROM {jobs_relation}
                WHERE state = 'leased' AND lease_expires_at > statement_timestamp()
                  AND priority = %(backfill_priority)s) < %(background_limit)s
           AND NOT {late_live}
        """,
        {
            **params,
            "backfill_priority": PYTHON_BACKFILL_PRIORITY,
            "background_limit": BACKGROUND_JOB_LIMIT,
            "live_priorities": [PYTHON_LIVE_PRIORITY, PYTHON_RESET_PRIORITY],
            "live_lag_pause": LIVE_LAG_PAUSE_SECONDS,
            "live_work_types": list(LIVE_WORK_TYPES),
        },
    ).fetchone()[0]
