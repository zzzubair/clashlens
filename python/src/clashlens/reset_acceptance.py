"""One record per Reset of how it went against the 05:30 board target.

The alert check calls ``refresh`` once a minute through the worker's database
role, passing the Reset of the board the website's public Daily leaderboard
page showed and when it read it. The record keeps what a healthy Reset must
show and what a green board build does not prove on its own (8 Oct 2026:
every member collected by 05:10, yet the first board published at 06:26 with
40% of its inputs Partial): how many members the sweep captured; when their
Reset readings were all collected and all processed; when the first frozen
board's inputs froze, when it was saved as published and when the website
first showed it; and that board's input states (Complete, Partial,
Inconsistent and the rest). Each value is set once, when first seen. A record
stays open to its later stages until its readings are processed and its board
shown, for as long as its sweep is kept: a reading can wait days for the
archive. Rows are never deleted: one a day, under 1 KB each.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb


def refresh(
    connection: Any,
    *,
    readable_boundary: datetime | None,
    readable_at: datetime | None,
) -> dict[str, Any] | None:
    """Update the latest Reset's record, and any older one still unfinished,
    and return the latest; None before any sweep."""
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '20s'")
        # The previous Reset too, for a stage the check was not running for,
        # and any kept sweep the check never recorded while it was down.
        sweeps = connection.execute(
            """
            SELECT id, boundary_at, cardinality(member_ids), membership_captured_at
            FROM collector_reset_sweeps
            WHERE boundary_at >= (SELECT max(boundary_at) - interval '1 day'
                                  FROM collector_reset_sweeps)
               OR boundary_at IN (
                   SELECT boundary_at FROM reset_acceptance_records
                   WHERE proof_processed_at IS NULL OR readable_at IS NULL)
               OR boundary_at NOT IN (SELECT boundary_at FROM reset_acceptance_records)
            ORDER BY boundary_at
            """
        ).fetchall()
        if not sweeps:
            return None
        for sweep_id, boundary_at, captured, captured_at in sweeps:
            connection.execute(
                """
                INSERT INTO reset_acceptance_records (
                    boundary_at, sweep_id, captured_count, membership_captured_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (boundary_at) DO NOTHING
                """,
                (boundary_at, sweep_id, captured or 0, captured_at),
            )
            record = _refresh_one(
                connection,
                boundary_at,
                sweep_id,
                readable_at=readable_at if readable_boundary == boundary_at else None,
            )
        return record


def _refresh_one(
    connection: Any,
    boundary_at: datetime,
    sweep_id: int,
    *,
    readable_at: datetime | None,
) -> dict[str, Any]:
    record = _record(connection, boundary_at)
    changes: dict[str, Any] = {}
    collected, not_collected, total, finished_at = connection.execute(
        """
        SELECT count(*) FILTER (WHERE status = 'complete'),
               count(*) FILTER (WHERE status IN ('failed', 'cancelled')),
               count(*), max(COALESCE(completed_at, updated_at))
        FROM collector_work WHERE sweep_id = %s AND kind = 'reset_baseline'
        """,
        (sweep_id,),
    ).fetchone()
    for column, value in (
        ("collected_count", collected),
        ("not_collected_count", not_collected),
    ):
        if record[column] != value:
            changes[column] = value
    collection_done = total > 0 and collected + not_collected == total
    if record["collection_finished_at"] is None and collection_done:
        changes["collection_finished_at"] = finished_at
    if record["proof_processed_at"] is None and collection_done:
        # Every response a Reset item saved counts, even when a later request
        # of that item failed or a newer response replaced it. Unchanged
        # readings reuse a response saved before the Reset, whose finished job
        # may already be cleaned up. A response saved since the Reset, or no
        # longer kept, whose jobs are gone was cleaned up before the check saw
        # it finish: the time is then unknown and stays empty, never the
        # collection time. A response whose job failed counts once a replay
        # job processed it, at the first time any of its jobs finished.
        pending, gone, processed_at = connection.execute(
            """
            SELECT count(*) FILTER (WHERE processing.jobs > 0 AND NOT processing.done),
                   count(*) FILTER (WHERE processing.jobs = 0
                                      AND COALESCE(observation.created_at >= %s, true)),
                   max(processing.completed_at)
            FROM collector_work AS work
            CROSS JOIN LATERAL unnest(
                ARRAY[work.profile_observation_id, work.battle_log_observation_id,
                      work.league_history_observation_id] || work.replaced_observation_ids
            ) AS reading (observation_id)
            LEFT JOIN collector_observations AS observation
              ON observation.id = reading.observation_id
            CROSS JOIN LATERAL (
                SELECT count(*) AS jobs,
                       COALESCE(bool_or(job.state = 'complete'), false) AS done,
                       min(job.completed_at) FILTER (WHERE job.state = 'complete')
                           AS completed_at
                FROM python_processing_jobs_worker AS job
                WHERE COALESCE(job.observation_id, job.replay_observation_id)
                      = reading.observation_id
            ) AS processing
            WHERE work.sweep_id = %s AND work.kind = 'reset_baseline'
              AND reading.observation_id IS NOT NULL
            """,
            (boundary_at, sweep_id),
        ).fetchone()
        if pending == 0 and gone == 0 and processed_at is not None:
            changes["proof_processed_at"] = processed_at
    frozen_at, published_at, manifest_id = connection.execute(
        """
        SELECT manifest.frozen_at, snapshot.published_at, manifest.id
        FROM boundary_publication_generations AS generation
        JOIN boundary_publication_manifests AS manifest
          ON manifest.id = generation.snapshot_manifest_id
        LEFT JOIN leaderboard_snapshots AS snapshot
          ON snapshot.id = generation.snapshot_id
        WHERE generation.boundary_at = %s
        ORDER BY manifest.frozen_at, generation.generation
        LIMIT 1
        """,
        (boundary_at,),
    ).fetchone() or (None, None, None)
    if record["inputs_frozen_at"] is None and frozen_at is not None:
        changes["inputs_frozen_at"] = frozen_at
    if record["published_at"] is None and published_at is not None:
        changes["published_at"] = published_at
    if record["readable_at"] is None and readable_at is not None:
        changes["readable_at"] = readable_at
    if record["board_inputs"] is None and manifest_id is not None:
        changes["board_inputs"] = Jsonb(
            _counts(
                connection,
                """
                SELECT classification, count(*)
                FROM boundary_publication_manifest_rows WHERE manifest_id = %s
                GROUP BY classification
                """,
                (manifest_id,),
            )
        )
    if not changes:
        return record
    assignments = ", ".join(f"{column} = %s" for column in changes)
    connection.execute(
        f"""
        UPDATE reset_acceptance_records
        SET {assignments}, updated_at = clock_timestamp()
        WHERE boundary_at = %s
        """,
        (*changes.values(), boundary_at),
    )
    return _record(connection, boundary_at)


def _record(connection: Any, boundary_at: datetime) -> dict[str, Any]:
    cursor = connection.execute(
        "SELECT * FROM reset_acceptance_records WHERE boundary_at = %s",
        (boundary_at,),
    )
    row = cursor.fetchone()
    return dict(zip([column.name for column in cursor.description], row, strict=True))


def _counts(connection: Any, query: str, parameters: tuple[Any, ...]) -> dict[str, int]:
    return {
        str(state): int(count)
        for state, count in connection.execute(query, parameters).fetchall()
    }
