from __future__ import annotations

import json
from argparse import Namespace
from threading import Event

from clashlens import cli
from clashlens.worker import ObservationProcessor, ProcessResult


class NoClaimDatabase:
    def __init__(self) -> None:
        self.claim_calls = 0

    def claim_job(self, **_kwargs: object) -> None:
        self.claim_calls += 1
        raise AssertionError("shutdown must stop before claiming another job")


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

        def __init__(self, _database_url: str, *, max_size: int = 4) -> None:
            assert max_size == 4

        def close(self) -> None:
            self.closed = True

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
    assert [line["event"] for line in output_lines[:-1]] == ["job_result"] * (
        cli.MAX_REPORTED_RESULTS + 1
    )
    assert database.closed is True


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

    def close(self) -> None:
        self.closed = True


class PoolRecordingArchive:
    def __init__(self, pool_size: int) -> None:
        self.pool_size = pool_size


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

    def fake_database(_url: str, *, max_size: int) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        return PoolRecordingDatabase(_url, max_size=max_size)

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> PoolRecordingArchive:
        recorded["archive_pool_size"] = pool_size
        return PoolRecordingArchive(pool_size)

    monkeypatch.setattr(cli, "Database", fake_database)
    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)

    result = cli._run_worker(_worker_namespace())

    assert result == 0
    assert recorded["database_max_size"] == 4
    assert recorded["archive_pool_size"] == 4
    assert recorded["process_until_idle"]["owner"] == "cli-worker"
    assert recorded["process_until_idle"]["max_jobs"] == 3
    assert recorded["process_until_idle"]["lease_seconds"] == 30
    assert isinstance(recorded["process_until_idle"]["stop_requested"], Event)


def test_run_worker_concurrent_path_uses_explicit_pool_sizes(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

    def fake_database(_url: str, *, max_size: int) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        return PoolRecordingDatabase(_url, max_size=max_size)

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

    def fake_database(_url: str, *, max_size: int) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
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
    assert recorded["archive_pool_size"] == 12
