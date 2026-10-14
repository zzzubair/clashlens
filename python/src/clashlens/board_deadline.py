"""When a Reset's Daily board stops waiting for a member.

A board builds once every captured member has a result (``boundary``). On
12 Oct 2026 two members' day calculations failed for good at 06:54, the
database refusing their numbers, and the 13,389 others waited with them until
a fix shipped: the board published at 12:27, 7 hours late. So a member whose
day calculation failed for good after the Reset is Failed. And from
``BOARD_DEADLINE`` after the Reset, while the Reset has no published board,
at most ``MAX_UNFINISHED`` members still waiting are Unavailable, so the
board builds without them (owner decision, 12 Oct 2026). More waiting means
something bigger is broken: the board keeps waiting, and the alert check
says how many (``alerts``), until ``HARD_DEADLINE``, when every member still
waiting is Unavailable (owner decision, 14 Oct 2026). When such a member's
day saves later, it changes the member as any new result does, which starts
a correction, and the board is rebuilt with it. A board build or statistics
job that fails runs again (``retry_failed_build``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
)

BOARD_DEADLINE = timedelta(minutes=30)
MAX_UNFINISHED = 50
HARD_DEADLINE = timedelta(hours=1)


def cut_due(connection: Any, boundary_at: datetime) -> bool:
    """Whether the Reset's newest unfrozen generation has members to leave
    out now, as below."""
    generation = connection.execute(
        """
        SELECT id FROM boundary_publication_generations
        WHERE boundary_at = %s AND snapshot_state = 'pending'
          AND snapshot_manifest_id IS NULL
        ORDER BY generation DESC LIMIT 1
        """,
        (boundary_at,),
    ).fetchone()
    return generation is not None and _cut_due(connection, boundary_at, generation[0])


def _cut_due(connection: Any, boundary_at: datetime, generation_id: int) -> bool:
    now, unpublished, waiting = connection.execute(
        """
        SELECT clock_timestamp(), NOT EXISTS (
            SELECT FROM boundary_publication_generations
            WHERE boundary_at = %s AND snapshot_state = 'published'), (
            SELECT count(*) FROM (
                SELECT FROM boundary_publication_generation_members
                WHERE generation_id = %s AND snapshot_status = 'pending'
                LIMIT %s) AS waiting)
        """,
        (boundary_at, generation_id, MAX_UNFINISHED + 1),
    ).fetchone()
    return unpublished and now >= boundary_at + BOARD_DEADLINE and 0 < waiting and (
        waiting <= MAX_UNFINISHED or now >= boundary_at + HARD_DEADLINE)


def overdue(connection: Any, boundary_at: datetime) -> bool:
    """Whether the Reset still has no published board and army results
    ``HARD_DEADLINE`` after it: nothing may hold them back then."""
    return connection.execute(
        """
        SELECT clock_timestamp() >= %s AND NOT EXISTS (
            SELECT FROM boundary_publication_generations
            WHERE boundary_at = %s AND snapshot_state = 'published'
              AND army_state = 'published')
        """,
        (boundary_at + HARD_DEADLINE, boundary_at),
    ).fetchone()[0]


def settle_waiting_members(connection: Any, boundary_at: datetime, generation_id: int) -> None:
    """Under the Reset's full lock, settle ``generation_id``'s waiting members
    as above while its board has not frozen."""
    if connection.execute(
        """
        SELECT FROM boundary_publication_generations
        WHERE id = %s AND snapshot_state = 'pending' AND snapshot_manifest_id IS NULL
        """,
        (generation_id,),
    ).fetchone() is None:
        return
    day = f"{(boundary_at - timedelta(days=1)).astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
    # Failed jobs are few, found through the queue's state index.
    failed = connection.execute(
        """
        UPDATE boundary_publication_generation_members AS member
        SET status = 'unavailable', snapshot_status = 'failed',
            army_status = 'failed', updated_at = clock_timestamp()
        WHERE member.generation_id = %(generation)s AND member.snapshot_status = 'pending'
          AND member.player_id IN (
              SELECT (job.input_json ->> 'player_id')::bigint
              FROM python_processing_jobs_worker AS job
              WHERE job.state = 'failed' AND job.work_type = 'reconcile_ranked_day'
                AND job.updated_at >= %(boundary)s
                AND %(day)s IN (job.input_json ->> 'ranked_day_start',
                                job.input_json ->> 'last_ranked_day_start'))
        """,
        {"generation": generation_id, "boundary": boundary_at, "day": day},
    ).rowcount
    unfinished = 0
    if _cut_due(connection, boundary_at, generation_id):
        unfinished = connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET status = 'unavailable', snapshot_status = 'unavailable',
                army_status = 'unavailable', updated_at = clock_timestamp()
            WHERE generation_id = %s AND snapshot_status = 'pending'
            """,
            (generation_id,),
        ).rowcount
    if failed or unfinished:
        print(json.dumps({
            "event": "board_members_settled", "boundary_at": boundary_at.isoformat(),
            "generation_id": generation_id, "failed": failed, "unfinished": unfinished,
        }), flush=True)


def retry_failed_build(connection: Any, boundary_at: datetime, generation_id: int) -> None:
    """Queue again, under a new key, the failed board build or statistics job
    a generation still waits for: one error failed it for good, and the next
    job met the failed one's key. Each retry waits twice as long as the one
    before, from a minute to at most an hour."""
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json, state,
            due_at, parser_version, processing_version, domain_rule_version,
            analytics_rule_version, priority)
        SELECT NULL, job.work_type,
               split_part(job.deduplication_key, ':retry:', 1) || ':retry:' || job.id,
               job.input_json, 'pending',
               job.updated_at + least(interval '1 hour',
                   interval '1 minute' * 2 ^ (count(*) OVER () - 1)),
               %s, %s, %s, %s, job.priority
        FROM boundary_publication_generations AS generation
        JOIN python_processing_jobs_worker AS job
          ON job.state = 'failed'
         AND job.work_type = CASE generation.snapshot_state
             WHEN 'ready' THEN 'build_snapshot' WHEN 'building' THEN 'build_analytics' END
         AND job.input_json ->> 'boundary_at' = %s
         AND job.input_json ->> 'generation' = generation.generation::text
        WHERE generation.id = %s
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (DEFAULT_PARSER_VERSION, PROCESSING_VERSION, DOMAIN_RULE_VERSION,
         ANALYTICS_RULE_VERSION, f"{boundary_at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}",
         generation_id),
    )
