"""Shared setup for reconcile pass postgres tests."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
from domain_test_support import text
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from reconciliation_postgres_support import _processor
from reset_scenarios_postgres_support import (
    SEASON,
)

from clashlens import reconcile_pass
from clashlens.stage_metrics import StageMetrics


def _as_worker(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(connection_info, options=f"{options} -c role=clashlens_python_worker")


def _run_pass(story, monkeypatch, now: datetime, slot: str | None = None,
              *, steps: int | None = None, reads_end: datetime | None = None) -> int:
    """Queue the pass of ``slot``, by default the night recheck's at ``now``,
    and run its waiting steps, or the first ``steps``, at ``now``, or at
    ``reads_end`` once a step's reads end, each claimed by the real worker
    with its own database role; how many ran."""
    clock = [now]
    monkeypatch.setattr(reconcile_pass, "_now", lambda: clock[0])
    database, processor = _processor(_as_worker(story.connection_info), story.archive_server)
    metrics = StageMetrics()
    processor.stage_metrics = metrics
    judge = reconcile_pass.judge

    def slow_judge(*args):
        judgment = judge(*args)
        clock[0] = reads_end or clock[0]
        return judgment

    ran = 0
    try:
        with monkeypatch.context() as patch:
            patch.setattr(reconcile_pass, "judge", slow_judge)
            reconcile_pass.queue_pass(database, slot or f"{day_of(now):%Y-%m-%d}:night")
            while ran != steps and (job := _waiting_step(story.connection_info)) is not None:
                assert processor.process_job(job, owner="test").outcome == "processed"
                ran += 1
    finally:
        database.close()
    if ran:
        # A real pass from saved days through its worker records each job's duration.
        assert metrics.snapshot()["python_reconcile_pass"]["count"] == ran
    return ran


def _waiting_step(connection_info: str) -> int | None:
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            "SELECT id FROM python_processing_jobs WHERE work_type = 'reconcile_pass'"
            " AND status = 'pending' ORDER BY id LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def day_of(now: datetime) -> datetime:
    """The Reset starting the Legend day at ``now``."""
    return now - (now - SEASON) % timedelta(days=1)


def _open(connection_info: str) -> set[tuple[str, str, int]]:
    """Each open finding: its check, player and Season day."""
    with psycopg.connect(connection_info) as connection:
        return {
            (text(check), text(tag), (start - SEASON).days + 1)
            for check, tag, start in connection.execute(
                """
                SELECT finding.check_name, player.normalized_tag, finding.ranked_day_start
                FROM reconcile_findings AS finding
                JOIN players AS player ON player.id = finding.player_id
                WHERE finding.resolved_at IS NULL
                """
            ).fetchall()
        }


def _queued(connection_info: str) -> list[tuple[str, str, int, str]]:
    """The recalculations the re-check queued: player, Season day, priority and state."""
    with psycopg.connect(connection_info) as connection:
        return sorted(
            (text(tag), text(job.get("trigger")), int(priority), text(state))
            for tag, job, priority, state in connection.execute(
                """
                SELECT player.normalized_tag, job.input_json, job.priority, job.status
                FROM python_processing_jobs AS job
                JOIN players AS player ON player.id = (job.input_json ->> 'player_id')::bigint
                WHERE job.deduplication_key LIKE 'reconcile:pass:%%'
                """
            ).fetchall()
        )
