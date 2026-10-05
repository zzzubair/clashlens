from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.errors import DeadlockDetected
from test_collector import _Client, _collector, _Spool, _Store

from clashlens import worker
from clashlens.archive import ArchiveReadResult
from clashlens.collector_db import CollectorWork
from clashlens.league_history import (
    LEAGUE_HISTORY_ENDPOINT_VERSION,
    LEAGUE_HISTORY_PARSER_VERSION,
    LEAGUE_HISTORY_SCHEMA_VERSION,
)
from clashlens.operating import SchedulingDelayMetrics, WorkerMetrics


@pytest.mark.parametrize(
    ("delay", "label"),
    [
        (-1, "lt_1"),
        (0, "lt_1"),
        (0.999999, "lt_1"),
        (1, "1_to_5"),
        (4.999999, "1_to_5"),
        (5, "5_to_30"),
        (29.999999, "5_to_30"),
        (30, "30_to_120"),
        (119.999999, "30_to_120"),
        (120, "ge_120"),
        (999999, "ge_120"),
    ],
)
def test_scheduling_delay_ranges_are_disjoint(delay, label) -> None:
    metrics = SchedulingDelayMetrics()
    due = datetime(2026, 10, 1, tzinfo=UTC)
    metrics.record("ordinary", due, due + timedelta(seconds=delay))
    lines = metrics.lines()
    assert len(lines) == 15
    assert sum(int(line.rsplit(" ", 1)[1]) for line in lines) == 1
    assert (
        f'clashlens_collector_scheduling_delay_total{{lane="ordinary",range="{label}"}} 1'
        in lines
    )


@pytest.mark.parametrize("lane", ["ordinary", "reset", "interactive"])
def test_collector_exports_one_sample_per_started_check(lane, monkeypatch) -> None:
    spool = _Spool()
    collector = _collector(spool, _Store(spool), _Client(spool))
    work = CollectorWork(1, "#2PP", datetime.now(UTC) - timedelta(seconds=130))
    available = False

    async def spool_available():
        return available

    monkeypatch.setattr(collector, "_spool_available", spool_available)
    assert (
        asyncio.run(collector.collect_player(work, lane=lane))
        == ["capacity_paused"] * 2
    )
    assert all(line.endswith(" 0") for line in collector.scheduling_delay.lines())
    available = True
    asyncio.run(collector.collect_player(work, lane=lane))
    status, _, body = asyncio.run(collector.health_response("/metrics"))
    assert status == 200
    lines = [
        line for line in body.decode().splitlines() if "scheduling_delay_total" in line
    ]
    assert sum(int(line.rsplit(" ", 1)[1]) for line in lines) == 1
    assert (
        f'clashlens_collector_scheduling_delay_total{{lane="{lane}",range="ge_120"}} 1'
        in lines
    )


@pytest.mark.parametrize(
    ("work_type", "target"),
    [
        ("process_observation", "complete_league_history"),
        ("replay_observation", "complete_league_history"),
        ("build_snapshot", "snapshots.complete_snapshot"),
        ("build_analytics", "boundary_publication.complete_analytics"),
        ("build_army_analytics", "army_ingestion.complete_army_analytics"),
        ("redecode_army", "army_ingestion.complete_army_redecode"),
        ("reconcile_ranked_day", "reconciliation_db.complete_reconciliation"),
    ],
)
def test_worker_jobs_expose_elapsed_and_thread_computation(
    work_type, target, monkeypatch
) -> None:
    clock = [100.0, 10.0]
    monkeypatch.setattr(worker, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "thread_time", lambda: clock[1])

    def complete(*_args):
        clock[0] += 2.0
        clock[1] += 0.25

    monkeypatch.setattr(f"clashlens.worker.{target}", complete)
    database = SimpleNamespace(renew_claim=lambda *_args, **_kwargs: None)
    archive = SimpleNamespace(
        read_verified=lambda *_args, **_kwargs: ArchiveReadResult(
            b'{"items":[{"leagueSeasonId":"1781499600"}]}', "s3://test", "digest"
        )
    )
    claim = SimpleNamespace(
        job_id=1,
        work_type=work_type,
        processing_version=worker.PROCESSING_VERSION,
        domain_rule_version=worker.DOMAIN_RULE_VERSION,
        analytics_rule_version=(
            worker.ARMY_ANALYTICS_RULE_VERSION
            if "army" in work_type
            else worker.ANALYTICS_RULE_VERSION
        ),
        endpoint="league_history",
        endpoint_version=LEAGUE_HISTORY_ENDPOINT_VERSION,
        parser_version=LEAGUE_HISTORY_PARSER_VERSION,
        schema_version=LEAGUE_HISTORY_SCHEMA_VERSION,
        archive_reference="s3://test",
        response_hash="digest",
        normalized_tag="#2PP",
        http_status=200,
        observed_at=datetime.now(UTC),
    )
    metrics = worker.StageMetrics()
    processor = worker.ObservationProcessor(database, archive, metrics)
    assert processor._process_claim(claim, lease_seconds=30).outcome == "processed"
    snapshot = WorkerMetrics().snapshot(
        stages=metrics.snapshot(), database_pool={}, queue={}, spool={}
    )
    stage = snapshot["stages"][f"python_{work_type}"]
    assert stage["count"] == 1
    assert stage["elapsed_seconds"] == 2.0
    assert stage["average_ms"] == 2000.0
    assert stage["thread_cpu_seconds"] == 0.25


def test_worker_records_retries_and_exceptions_without_losing_samples(
    monkeypatch,
) -> None:
    clock = [100.0, 10.0]
    monkeypatch.setattr(worker, "monotonic", lambda: clock[0])
    monkeypatch.setattr(worker, "thread_time", lambda: clock[1])
    metrics = worker.StageMetrics()
    processor = worker.ObservationProcessor(SimpleNamespace(), None, metrics)
    attempts = 0

    def process(claim, **_kwargs):
        nonlocal attempts
        attempts += 1
        clock[0] += 1
        clock[1] += 0.1
        if attempts == 1:
            raise DeadlockDetected()
        if attempts == 3:
            raise RuntimeError("unexpected failure")
        return worker.ProcessResult(claim.job_id, "processed")

    monkeypatch.setattr(processor, "_process_claim_once", process)
    claim = SimpleNamespace(job_id=1, work_type="build_snapshot")
    assert processor._process_claim(claim, lease_seconds=30).outcome == "processed"
    with pytest.raises(RuntimeError, match="unexpected failure"):
        processor._process_claim(claim, lease_seconds=30)
    stage = metrics.snapshot()["python_build_snapshot"]
    assert stage["count"] == 2
    assert stage["elapsed_seconds"] == 3
    assert stage["thread_cpu_seconds"] == pytest.approx(0.3)


def test_stages_without_thread_timing_show_none_rather_than_zero() -> None:
    metrics = worker.StageMetrics()
    metrics.record("python_claim", 10.0)
    snapshot = WorkerMetrics().snapshot(
        stages=metrics.snapshot(), database_pool={}, queue={}, spool={}
    )
    assert snapshot["stages"]["python_claim"]["thread_cpu_seconds"] is None
    assert snapshot["stages"]["python_build_snapshot"]["thread_cpu_seconds"] is None
