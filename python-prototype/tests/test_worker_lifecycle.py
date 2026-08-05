from __future__ import annotations

import json
from argparse import Namespace
from threading import Event

from clashlens_prototype import cli
from clashlens_prototype.worker import ObservationProcessor, ProcessResult


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


def test_worker_does_not_claim_when_archive_readiness_is_false() -> None:
    database = NoClaimDatabase()
    processor = ObservationProcessor(database, archive=object())

    results = processor.process_until_idle(
        owner="archive-down-worker",
        readiness_check=lambda: False,
    )

    assert results == []
    assert database.claim_calls == 0


def test_run_forever_keeps_reported_results_bounded(monkeypatch, capsys) -> None:
    class FakeDatabase:
        closed = False

        def __init__(self, _database_url: str) -> None:
            return

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
            stop_requested = kwargs["stop_requested"]
            assert isinstance(stop_requested, Event)
            stop_requested.set()
            return [
                ProcessResult(index, "processed")
                for index in range(cli.MAX_REPORTED_RESULTS + 1)
            ]

    monkeypatch.setattr(cli, "Database", lambda _url: database)
    monkeypatch.setattr(cli, "_archive", lambda _arguments: FakeArchive())
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
    )

    result = cli._run_worker(arguments)
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["processed_count"] == cli.MAX_REPORTED_RESULTS + 1
    assert len(output["results"]) == cli.MAX_REPORTED_RESULTS
    assert database.closed is True
