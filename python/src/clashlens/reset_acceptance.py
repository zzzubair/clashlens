"""One record per Reset of how it went against the 05:30 board target.

The alert check calls ``refresh`` once a minute through the worker's database
role, passing the Reset of the frozen board it just read back through the
request the website makes. The record keeps what a healthy Reset must show
and what a green board build does not prove on its own (8 Oct 2026: every
member collected by 05:10, yet the first board published at 06:26 with 40% of
its inputs Partial): how many members the sweep captured; when their Reset
readings were all collected and all processed; when the first frozen board's
inputs froze, when it was saved as published and when it was first readable;
that board's input states; and the ended Legend day's results and boundary
settlement at 06:00 and just before the next Reset. Each time is set once,
when first seen. Rows are never deleted: one a day, under 1 KB each.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

AT_0600 = timedelta(hours=1)
# The next Reset's pause starts at 04:55; the check runs every minute.
AT_NEXT_RESET = timedelta(hours=23, minutes=50)


def refresh(
    connection: Any, *, readable_boundary: datetime | None, now: datetime
) -> dict[str, Any] | None:
    """Update the latest Reset's record and return it; None before any sweep."""
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        # The day results read about 13,000 latest versions: 5.6 s on 8 Oct 2026.
        connection.execute("SET LOCAL statement_timeout = '20s'")
        sweeps = connection.execute(
            """
            SELECT id, boundary_at, cardinality(member_ids), membership_captured_at
            FROM collector_reset_sweeps ORDER BY boundary_at DESC LIMIT 2
            """
        ).fetchall()
        if not sweeps:
            return None
        # The previous Reset too, for a checkpoint the check was not running for.
        for sweep_id, boundary_at, captured, captured_at in reversed(sweeps):
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
                readable=readable_boundary == boundary_at,
                latest=boundary_at == sweeps[0][1],
                now=now,
            )
        return record


def _refresh_one(
    connection: Any,
    boundary_at: datetime,
    sweep_id: int,
    *,
    readable: bool,
    latest: bool,
    now: datetime,
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
        # Unchanged readings reuse an older saved response whose finished job
        # may already be cleaned up; only an unfinished job holds this back.
        pending, processed_at = connection.execute(
            """
            SELECT count(*) FILTER (WHERE job.state <> 'complete'), max(job.completed_at)
            FROM collector_work AS work
            CROSS JOIN LATERAL (VALUES (work.profile_observation_id),
                                       (work.battle_log_observation_id))
                AS reading (observation_id)
            LEFT JOIN python_processing_jobs_worker AS job
              ON job.observation_id = reading.observation_id
            WHERE work.sweep_id = %s AND work.kind = 'reset_baseline'
              AND work.status = 'complete'
            """,
            (sweep_id,),
        ).fetchone()
        if pending == 0:
            changes["proof_processed_at"] = processed_at or finished_at
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
    if record["readable_at"] is None and readable:
        changes["readable_at"] = now
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
    for column, offset in (("at_0600", AT_0600), ("at_next_reset", AT_NEXT_RESET)):
        # A checkpoint the check missed is taken late, with the time it was
        # taken; once the next Reset has begun its last one is due anyway.
        due = now >= boundary_at + offset or (column == "at_next_reset" and not latest)
        if record[column] is None and due:
            changes[column] = Jsonb(_checkpoint(connection, sweep_id, boundary_at, now))
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


def _checkpoint(
    connection: Any, sweep_id: int, boundary_at: datetime, now: datetime
) -> dict[str, Any]:
    """The ended Legend day's latest result states and the Reset's settlement."""
    return {
        "at": now.isoformat(),
        "day_results": _counts(
            connection,
            """
            SELECT COALESCE(day.state, 'Missing'), count(*)
            FROM collector_reset_sweeps AS sweep
            CROSS JOIN unnest(sweep.member_ids) AS member (player_id)
            LEFT JOIN LATERAL (
                SELECT version.state FROM ranked_day_versions AS version
                WHERE version.player_id = member.player_id
                  AND version.ranked_day_start = sweep.boundary_at - interval '1 day'
                ORDER BY version.version DESC, version.id DESC LIMIT 1
            ) AS day ON true
            WHERE sweep.id = %s
            GROUP BY 1
            """,
            (sweep_id,),
        ),
        "settlement": _counts(
            connection,
            """
            SELECT state, count(*) FROM reset_boundary_settlements
            WHERE boundary_at = %s GROUP BY state
            """,
            (boundary_at,),
        ),
    }
