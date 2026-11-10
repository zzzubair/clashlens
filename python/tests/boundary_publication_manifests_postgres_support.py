"""Shared setup for boundary publication manifests postgres tests."""

from __future__ import annotations

from datetime import timedelta

from domain_test_support import store_observation
from reconciliation_postgres_support import (
    DAY_END,
    DAY_START,
    _battle_log,
    _store_baseline_pair,
)
from snapshot_publication_postgres_support import _process_snapshot_and_analytics

from clashlens import (
    boundary_publication,
)


def _freeze_army_inputs(connection_info: str, archive_server, processor):
    """Process one Legend day up to its Reset's frozen, unbuilt army build.

    Returns a pending-jobs lookup for that Reset and the army job and input.
    """
    jobs = _reconcile_day(connection_info, archive_server, processor)
    _publish_board(connection_info, processor, jobs)
    boundary_publication.reevaluate_boundary_publications(processor.database)
    [(army_job, army_input)] = jobs("build_army_analytics")
    return jobs, army_job, army_input


def _publish_board(connection_info: str, processor, jobs) -> None:
    """Build the Reset's board and its statistics: its army inputs freeze
    at the next publication check."""
    [(snapshot_job, _input)] = jobs("build_snapshot")
    _process_snapshot_and_analytics(
        connection_info,
        processor.database,
        processor,
        snapshot_job,
        owner_prefix="snapshot",
    )


def _reconcile_day(connection_info: str, archive_server, processor):
    """Process one Legend day's reports and Reset; return a pending-jobs lookup."""
    pairs = [
        _store_baseline_pair(
            connection_info,
            archive_server,
            key=key,
            boundary=day,
            trophies=trophies,
            empty_battle_log=empty,
        )
        for key, day, trophies, empty in (
            ("start", DAY_START, 6000, True),
            ("end", DAY_END, 6040, False),
        )
    ]
    middle = store_observation(
        connection_info,
        archive_server,
        occurrence_key="middle",
        endpoint="battle_log",
        body=_battle_log(),
        observed_at=DAY_START + timedelta(hours=7),
        normalized_tag="#2PP",
    )[1]
    boundary_at = DAY_END.strftime("%Y-%m-%dT%H:%M:%SZ")
    for job_id in (*pairs[0][2:], middle, *pairs[1][2:]):
        assert processor.process_job(job_id, owner="source").outcome == "processed"

    def jobs(work_type: str) -> list[tuple]:
        with processor.database.pool.connection() as connection:
            return connection.execute(
                "SELECT id, input_json FROM python_processing_jobs"
                " WHERE work_type = %s AND status = 'pending'"
                " AND coalesce(input_json->>'boundary_at', %s) = %s"
                " ORDER BY id",
                (work_type, boundary_at, boundary_at),
            ).fetchall()

    for job_id, _input in jobs("reconcile_ranked_day"):
        assert processor.process_job(job_id, owner="day").outcome == "processed"
    return jobs
