from __future__ import annotations

import json
from argparse import Namespace
from threading import Event

import pytest

from clashlens import cli
from clashlens.worker import ObservationProcessor, ProcessResult, StageMetrics


class NoClaimDatabase:
    def __init__(self) -> None:
        self.claim_calls = 0

    def claim_job(self, **_kwargs: object) -> None:
        self.claim_calls += 1
        raise AssertionError("shutdown must stop before claiming another job")


def test_stage_metrics_report_bounded_histogram_percentiles() -> None:
    metrics = StageMetrics()
    for duration in (0.0002, 0.001, 0.02, 0.2):
        metrics.record("claim", duration)

    snapshot = metrics.snapshot()["claim"]
    assert snapshot["count"] == 4
    assert snapshot["average_ms"] == pytest.approx(55.3)
    assert snapshot["p50_upper_ms"] == 1.0
    assert snapshot["p95_upper_ms"] == 250.0
    assert snapshot["p99_upper_ms"] == 250.0


def test_worker_does_not_claim_after_shutdown_is_requested() -> None:
    database = NoClaimDatabase()
    stop_requested = Event()
    stop_requested.set()
    processor = ObservationProcessor(database, archive=object())

    results = processor.process_until_idle(
        owner="shutdown-worker",
        stop_requested=stop_requested,
    )

    assert results == []
    assert database.claim_calls == 0


def test_run_forever_keeps_reported_results_bounded(monkeypatch, capsys) -> None:
    class FakeDatabase:
        closed = False
        maintenance_calls = 0

        def __init__(self, _database_url: str, *, max_size: int = 4) -> None:
            assert max_size == 4

        def close(self) -> None:
            self.closed = True

        def maintain_queue(self, *, max_jobs: int) -> int:
            assert max_jobs == 100
            self.maintenance_calls += 1
            return 0

    database = FakeDatabase("postgresql://prototype@postgres/db")

    class FakeArchive:
        def check_ready(self) -> bool:
            return True

    class FakeProcessor:
        def __init__(self, _database: FakeDatabase, _archive: object) -> None:
            return

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            assert "readiness_check" not in kwargs
            stop_requested = kwargs["stop_requested"]
            assert isinstance(stop_requested, Event)
            stop_requested.set()
            return [
                ProcessResult(index, "processed")
                for index in range(cli.MAX_REPORTED_RESULTS + 1)
            ]

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: database)

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> FakeArchive:
        del _arguments, pool_size
        return FakeArchive()

    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    arguments = Namespace(
        database_url="postgresql://prototype@postgres/db",
        database_url_file="",
        archive_endpoint="archive.example:9000",
        owner="bounded-worker",
        max_jobs=1,
        lease_seconds=30,
        run_forever=True,
        poll_interval_seconds=0.01,
        concurrency=1,
        database_pool_size=None,
        archive_pool_size=None,
    )

    result = cli._run_worker(arguments)
    output_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    output = output_lines[-1]

    assert result == 0
    assert output["processed_count"] == cli.MAX_REPORTED_RESULTS + 1
    assert len(output["results"]) == cli.MAX_REPORTED_RESULTS
    assert [line["event"] for line in output_lines[:-1]] == (
        ["job_result"] * (cli.MAX_REPORTED_RESULTS + 1) + ["worker_health"]
    )
    assert database.closed is True
    assert database.maintenance_calls == 1


@pytest.mark.parametrize("concurrency", [1, 3])
def test_worker_does_not_claim_or_maintain_when_archive_is_unavailable(
    monkeypatch, capsys, concurrency: int
) -> None:
    class FakeDatabase:
        closed = False
        maintenance_calls = 0

        def __init__(self, _database_url: str, *, max_size: int = 4) -> None:
            del max_size

        def close(self) -> None:
            self.closed = True

        def maintain_queue(self, *, max_jobs: int) -> int:
            del max_jobs
            self.maintenance_calls += 1
            raise AssertionError("archive outage must stop before maintenance")

    database = FakeDatabase("postgresql://prototype@postgres/db")

    class UnavailableArchive:
        checks = 0

        def check_ready(self) -> bool:
            self.checks += 1
            return False

    archive = UnavailableArchive()
    claim_attempts = 0

    class NoClaimProcessor:
        def __init__(self, _database: FakeDatabase, _archive: object) -> None:
            del _database, _archive

        def process_until_idle(self, **_kwargs: object) -> list[ProcessResult]:
            nonlocal claim_attempts
            claim_attempts += 1
            raise AssertionError("archive outage must stop before claiming")

    def forbidden_concurrent(*_args: object, **_kwargs: object) -> list[ProcessResult]:
        nonlocal claim_attempts
        claim_attempts += 1
        raise AssertionError("archive outage must stop before claiming")

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: database)
    monkeypatch.setattr(cli, "_archive", lambda _arguments, **kwargs: archive)
    monkeypatch.setattr(cli, "ObservationProcessor", NoClaimProcessor)
    monkeypatch.setattr(cli, "process_concurrently", forbidden_concurrent)

    result = cli._run_worker(
        _worker_namespace(concurrency=concurrency, run_forever=False)
    )

    assert result == 0
    assert archive.checks == 1
    assert claim_attempts == 0
    assert database.maintenance_calls == 0
    assert database.closed is True
    assert json.loads(capsys.readouterr().out)["results"] == []


def test_run_forever_rechecks_archive_and_resumes_after_outage(
    monkeypatch, capsys
) -> None:
    class FakeDatabase:
        closed = False
        maintenance_calls = 0

        def __init__(self, _database_url: str, *, max_size: int = 4) -> None:
            del max_size

        def close(self) -> None:
            self.closed = True

        def maintain_queue(self, *, max_jobs: int) -> int:
            assert max_jobs == 100
            self.maintenance_calls += 1
            return 0

    database = FakeDatabase("postgresql://prototype@postgres/db")

    class RecoveringArchive:
        def __init__(self) -> None:
            self.checks = 0

        def check_ready(self) -> bool:
            self.checks += 1
            return self.checks > 1

    archive = RecoveringArchive()
    claim_attempts = 0

    class ResumingProcessor:
        def __init__(self, _database: FakeDatabase, _archive: object) -> None:
            del _database, _archive

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            nonlocal claim_attempts
            claim_attempts += 1
            stop_requested = kwargs["stop_requested"]
            assert isinstance(stop_requested, Event)
            stop_requested.set()
            return [ProcessResult(7, "processed")]

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: database)
    monkeypatch.setattr(cli, "_archive", lambda _arguments, **kwargs: archive)
    monkeypatch.setattr(cli, "ObservationProcessor", ResumingProcessor)

    result = cli._run_worker(
        _worker_namespace(run_forever=True, poll_interval_seconds=0.001)
    )

    assert result == 0
    assert archive.checks == 2
    assert claim_attempts == 1
    assert database.maintenance_calls == 1
    assert database.closed is True
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[-1]["processed_count"] == 1


def _worker_namespace(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "database_url": "postgresql://prototype@postgres/db",
        "database_url_file": "",
        "archive_endpoint": "archive.example:9000",
        "owner": "cli-worker",
        "max_jobs": 3,
        "lease_seconds": 30,
        "run_forever": False,
        "poll_interval_seconds": 0.01,
        "concurrency": 1,
        "database_pool_size": None,
        "archive_pool_size": None,
    }
    values.update(overrides)
    return Namespace(**values)


class PoolRecordingDatabase:
    def __init__(self, _database_url: str, *, max_size: int) -> None:
        self.max_size = max_size
        self.closed = False
        self.maintenance_limits: list[int] = []

    def maintain_queue(self, *, max_jobs: int) -> int:
        self.maintenance_limits.append(max_jobs)
        return 0

    def close(self) -> None:
        self.closed = True


class PoolRecordingArchive:
    def __init__(self, pool_size: int) -> None:
        self.pool_size = pool_size

    def check_ready(self) -> bool:
        return True


def test_run_worker_defaults_preserve_the_single_thread_path(
    monkeypatch, capsys
) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            recorded["process_until_idle"] = kwargs
            return []

    def fake_database(
        _url: str, *, max_size: int, expected_contract_version: int
    ) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        recorded["expected_contract_version"] = expected_contract_version
        database = PoolRecordingDatabase(_url, max_size=max_size)
        recorded["database"] = database
        return database

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> PoolRecordingArchive:
        recorded["archive_pool_size"] = pool_size
        return PoolRecordingArchive(pool_size)

    monkeypatch.setattr(cli, "Database", fake_database)
    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)

    result = cli._run_worker(_worker_namespace())

    assert result == 0
    assert recorded["database_max_size"] == 4
    assert recorded["expected_contract_version"] == 4
    assert recorded["archive_pool_size"] == 4
    assert recorded["process_until_idle"]["owner"] == "cli-worker"
    assert recorded["process_until_idle"]["max_jobs"] == 3
    assert recorded["process_until_idle"]["lease_seconds"] == 30
    assert isinstance(recorded["process_until_idle"]["stop_requested"], Event)
    database = recorded["database"]
    assert isinstance(database, PoolRecordingDatabase)
    assert database.maintenance_limits == [100]


def test_run_worker_concurrent_path_uses_explicit_pool_sizes(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

    def fake_database(
        _url: str, *, max_size: int, expected_contract_version: int
    ) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        recorded["expected_contract_version"] = expected_contract_version
        database = PoolRecordingDatabase(_url, max_size=max_size)
        recorded["database"] = database
        return database

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> PoolRecordingArchive:
        recorded["archive_pool_size"] = pool_size
        return PoolRecordingArchive(pool_size)

    def fake_concurrent(
        processor: object,
        *,
        concurrency: int,
        owner: str,
        max_jobs: int,
        lease_seconds: int,
        stop_requested: object,
    ) -> list[ProcessResult]:
        recorded["concurrent_args"] = {
            "concurrency": concurrency,
            "owner": owner,
            "max_jobs": max_jobs,
            "lease_seconds": lease_seconds,
        }
        return []

    monkeypatch.setattr(cli, "Database", fake_database)
    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    monkeypatch.setattr(cli, "process_concurrently", fake_concurrent)

    result = cli._run_worker(
        _worker_namespace(concurrency=3, max_jobs=25, lease_seconds=40)
    )

    assert result == 0
    assert recorded["database_max_size"] == 8
    assert recorded["expected_contract_version"] == 4
    assert recorded["archive_pool_size"] == 4
    assert recorded["concurrent_args"] == {
        "concurrency": 3,
        "owner": "cli-worker",
        "max_jobs": 25,
        "lease_seconds": 40,
    }


def test_run_worker_honors_explicit_pool_size_flags(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

    def fake_database(
        _url: str, *, max_size: int, expected_contract_version: int
    ) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        recorded["expected_contract_version"] = expected_contract_version
        return PoolRecordingDatabase(_url, max_size=max_size)

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> PoolRecordingArchive:
        recorded["archive_pool_size"] = pool_size
        return PoolRecordingArchive(pool_size)

    def fake_concurrent(processor: object, **kwargs: object) -> list[ProcessResult]:
        del processor
        recorded["concurrent_args"] = kwargs
        return []

    monkeypatch.setattr(cli, "Database", fake_database)
    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    monkeypatch.setattr(cli, "process_concurrently", fake_concurrent)

    result = cli._run_worker(
        _worker_namespace(
            concurrency=20,
            database_pool_size=6,
            archive_pool_size=12,
        )
    )

    assert result == 0
    assert recorded["database_max_size"] == 6
    assert recorded["expected_contract_version"] == 4
    assert recorded["archive_pool_size"] == 12
