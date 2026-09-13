from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from clashlens import cli
from clashlens.db import DOMAIN_RULE_VERSION, PROCESSING_VERSION
from clashlens.domain import DomainRuleError
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


def test_worker_terminalizes_race_to_retired_season() -> None:
    class RetiredDatabase:
        def __init__(self) -> None:
            self.finished: list[tuple[int, str]] = []

        def renew_claim(self, _claim: object, *, lease_seconds: int) -> None:
            del lease_seconds

        def complete_reconciliation(self, _claim: object) -> None:
            raise DomainRuleError("season_detail_retired", "season fence won the race")

        def complete_terminal(self, claim: object, *, outcome: str) -> None:
            self.finished.append((claim.job_id, outcome))  # type: ignore[attr-defined]

    claim = type(
        "Claim",
        (),
        {
            "job_id": 17,
            "work_type": "reconcile_ranked_day",
            "processing_version": PROCESSING_VERSION,
            "domain_rule_version": DOMAIN_RULE_VERSION,
        },
    )()
    database = RetiredDatabase()
    result = ObservationProcessor(database, archive=object())._process_claim(
        claim, lease_seconds=30
    )

    assert result == ProcessResult(17, "season_detail_retired", "season_detail_retired")
    assert database.finished == [(17, "season_detail_retired")]


def test_new_observation_reads_only_the_local_spool() -> None:
    from clashlens.archive import ArchiveReadResult

    body = (Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json").read_bytes()
    digest = hashlib.sha256(body).hexdigest()

    class LocalSpool:
        def __init__(self) -> None:
            self.calls = 0

        def verify(self, expected_hash: str, expected_size: int | None = None) -> bytes:
            self.calls += 1
            assert expected_hash == digest
            assert expected_size is None
            return body

    class Archive:
        def __init__(self, spool: LocalSpool) -> None:
            self.spool = spool
            self.remote_calls = 0

        def read_verified(self, *_args: object, **_kwargs: object) -> ArchiveReadResult:
            self.remote_calls += 1
            raise AssertionError("new observations must not use archive fallback")

    class Database:
        def __init__(self) -> None:
            self.profile = None

        def renew_claim(self, _claim: object, *, lease_seconds: int) -> None:
            assert lease_seconds == 30

        def complete_profile(self, _claim: object, profile: object) -> None:
            self.profile = profile

    spool = LocalSpool()
    archive = Archive(spool)
    database = Database()
    claim = SimpleNamespace(
        job_id=41,
        work_type="process_observation",
        processing_version=PROCESSING_VERSION,
        domain_rule_version=DOMAIN_RULE_VERSION,
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        parser_version="supercell-profile-parser-v3",
        archive_reference=None,
        response_hash=digest,
        normalized_tag="#2PP",
        http_status=200,
        observed_at=datetime.now(UTC),
    )

    result = ObservationProcessor(database, archive)._process_claim(
        claim, lease_seconds=30
    )

    assert result == ProcessResult(41, "processed")
    assert spool.calls == 1
    assert archive.remote_calls == 0
    assert database.profile is not None


def test_missing_new_observation_is_not_repaired_from_archive() -> None:
    from clashlens.archive import ArchiveReadResult

    class LocalSpool:
        def verify(self, _expected_hash: str) -> None:
            return None

    class Archive:
        spool = LocalSpool()
        remote_calls = 0

        def read_verified(self, *_args: object, **_kwargs: object) -> ArchiveReadResult:
            self.remote_calls += 1
            raise AssertionError("new observations must not use archive fallback")

    class Database:
        def renew_claim(self, _claim: object, *, lease_seconds: int) -> None:
            assert lease_seconds == 30

        def fail_claim(self, _claim: object, *, category: str, detail: str, retryable: bool) -> str:
            assert category == "spool_missing"
            assert detail.startswith("spool_missing:")
            assert retryable is False
            return "failed"

    claim = SimpleNamespace(
        job_id=42,
        work_type="process_observation",
        processing_version=PROCESSING_VERSION,
        domain_rule_version=DOMAIN_RULE_VERSION,
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        parser_version="supercell-profile-parser-v3",
        archive_reference="s3://evidence/sha256/00/" + "0" * 64,
        response_hash="0" * 64,
        normalized_tag="#2PP",
        http_status=200,
        observed_at=datetime.now(UTC),
    )
    archive = Archive()

    result = ObservationProcessor(Database(), archive)._process_claim(
        claim, lease_seconds=30
    )

    assert result == ProcessResult(42, "failed", "spool_missing")
    assert archive.remote_calls == 0


def test_replay_observation_can_use_archive_fallback() -> None:
    from clashlens.archive import ArchiveReadResult

    body = (Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json").read_bytes()
    digest = hashlib.sha256(body).hexdigest()

    class LocalSpool:
        def verify(self, _expected_hash: str) -> None:
            return None

    class Archive:
        def __init__(self) -> None:
            self.spool = LocalSpool()
            self.remote_calls = 0

        def read_verified(self, *_args: object, **_kwargs: object) -> ArchiveReadResult:
            self.remote_calls += 1
            return ArchiveReadResult(body, "s3://evidence/source", digest)

    class Database:
        def renew_claim(self, _claim: object, *, lease_seconds: int) -> None:
            assert lease_seconds == 30

        def complete_profile(self, _claim: object, _profile: object) -> None:
            return None

    claim = SimpleNamespace(
        job_id=43,
        work_type="replay_observation",
        processing_version=PROCESSING_VERSION,
        domain_rule_version=DOMAIN_RULE_VERSION,
        endpoint="profile",
        endpoint_version="profile-v1",
        schema_version="profile-schema-v1",
        parser_version="supercell-profile-parser-v3",
        archive_reference="s3://evidence/sha256/" + digest[:2] + "/" + digest,
        response_hash=digest,
        normalized_tag="#2PP",
        http_status=200,
        observed_at=datetime.now(UTC),
    )
    archive = Archive()

    result = ObservationProcessor(Database(), archive)._process_claim(
        claim, lease_seconds=30
    )

    assert result == ProcessResult(43, "processed")
    assert archive.remote_calls == 1


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
    operating_snapshots: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "write_private_snapshot",
        lambda path, snapshot: operating_snapshots.append(snapshot),
    )
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
        operating_snapshot_file="/tmp/clashlens-worker-operating.json",
        disable_player_discovery=False,
    )

    result = cli._run_worker(arguments)
    output_lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    output = output_lines[-1]

    assert result == 0
    assert output["processed_count"] == cli.MAX_REPORTED_RESULTS + 1
    assert len(output["results"]) == cli.MAX_REPORTED_RESULTS
    assert [line["event"] for line in output_lines[:-1]] == (
        ["worker_health"] + ["job_result"] * (cli.MAX_REPORTED_RESULTS + 1)
    )
    assert database.closed is True
    assert database.maintenance_calls == 1
    assert operating_snapshots[-1]["outcomes"]["processed"] == (
        cli.MAX_REPORTED_RESULTS + 1
    )
    assert operating_snapshots[-1]["process"]["id"]


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
        "operating_snapshot_file": "",
        "disable_player_discovery": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_operating_snapshot_refreshes_while_a_batch_is_blocked(
    monkeypatch,
) -> None:
    class FakeDatabase:
        closed = False

        def close(self) -> None:
            self.closed = True

        def maintain_queue(self, *, max_jobs: int) -> int:
            assert max_jobs == 100
            return 0

    database = FakeDatabase()

    class FakeArchive:
        @staticmethod
        def check_ready() -> bool:
            return True

    refreshed = Event()
    snapshots: list[dict[str, object]] = []

    class BlockingProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            assert refreshed.wait(1)
            stop_requested = kwargs["stop_requested"]
            assert isinstance(stop_requested, Event)
            stop_requested.set()
            return []

    def capture_snapshot(_path: object, snapshot: dict[str, object]) -> None:
        snapshots.append(snapshot)
        if len(snapshots) >= 2:
            refreshed.set()

    monkeypatch.setattr(cli, "Database", lambda _url, **_kwargs: database)
    monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: FakeArchive())
    monkeypatch.setattr(cli, "ObservationProcessor", BlockingProcessor)
    monkeypatch.setattr(cli, "write_private_snapshot", capture_snapshot)
    monkeypatch.setattr(cli, "WORKER_SNAPSHOT_INTERVAL_SECONDS", 0.01)

    result = cli._run_worker(
        _worker_namespace(
            run_forever=True,
            operating_snapshot_file="/tmp/clashlens-worker-operating.json",
        )
    )

    assert result == 0
    assert len(snapshots) >= 3
    assert snapshots[0]["captured_at"] != snapshots[1]["captured_at"]
    assert database.closed is True



def test_initial_operating_snapshot_failure_does_not_stop_work(
    monkeypatch, capsys
) -> None:
    class FakeDatabase:
        def close(self) -> None:
            return

        def maintain_queue(self, *, max_jobs: int) -> int:
            assert max_jobs == 100
            return 0

    class FakeArchive:
        @staticmethod
        def check_ready() -> bool:
            return True

    class OneBatchProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            stop_requested = kwargs["stop_requested"]
            assert isinstance(stop_requested, Event)
            stop_requested.set()
            return [ProcessResult(1, "processed")]

    original_snapshot = cli.WorkerMetrics.snapshot
    calls = 0

    def fail_once(self: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("unavailable")
        return original_snapshot(self, **kwargs)

    monkeypatch.setattr(cli, "Database", lambda _url, **_kwargs: FakeDatabase())
    monkeypatch.setattr(cli, "_archive", lambda _arguments, **_kwargs: FakeArchive())
    monkeypatch.setattr(cli, "ObservationProcessor", OneBatchProcessor)
    monkeypatch.setattr(cli.WorkerMetrics, "snapshot", fail_once)

    result = cli._run_worker(
        _worker_namespace(run_forever=True, operating_snapshot_file="")
    )

    assert result == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0] == {"event": "worker_health", "status": "unavailable"}
    assert output[-1]["processed_count"] == 1


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
        _url: str,
        *,
        max_size: int,
        expected_contract_version: int,
        player_discovery_enabled: bool,
    ) -> PoolRecordingDatabase:
        recorded["database_max_size"] = max_size
        recorded["expected_contract_version"] = expected_contract_version
        recorded["player_discovery_enabled"] = player_discovery_enabled
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
    assert recorded["expected_contract_version"] == 5
    assert recorded["player_discovery_enabled"] is True
    assert recorded["archive_pool_size"] == 4
    assert recorded["process_until_idle"]["owner"] == "cli-worker"
    assert recorded["process_until_idle"]["max_jobs"] == 3
    assert recorded["process_until_idle"]["lease_seconds"] == 30
    assert isinstance(recorded["process_until_idle"]["stop_requested"], Event)
    database = recorded["database"]
    assert isinstance(database, PoolRecordingDatabase)
    assert database.maintenance_limits == [100]


def test_run_worker_disables_player_discovery_when_flagged(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

        def process_until_idle(self, **kwargs: object) -> list[ProcessResult]:
            return []

    def fake_database(
        _url: str,
        *,
        max_size: int,
        expected_contract_version: int,
        player_discovery_enabled: bool,
    ) -> PoolRecordingDatabase:
        recorded["player_discovery_enabled"] = player_discovery_enabled
        return PoolRecordingDatabase(_url, max_size=max_size)

    def fake_archive(_arguments: object, *, pool_size: int = 4) -> PoolRecordingArchive:
        return PoolRecordingArchive(pool_size)

    monkeypatch.setattr(cli, "Database", fake_database)
    monkeypatch.setattr(cli, "_archive", fake_archive)
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)

    result = cli._run_worker(_worker_namespace(disable_player_discovery=True))

    assert result == 0
    assert recorded["player_discovery_enabled"] is False


def test_run_worker_concurrent_path_uses_explicit_pool_sizes(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return

    def fake_database(
        _url: str,
        *,
        max_size: int,
        expected_contract_version: int,
        player_discovery_enabled: bool,
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
    assert recorded["expected_contract_version"] == 5
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
        _url: str,
        *,
        max_size: int,
        expected_contract_version: int,
        player_discovery_enabled: bool,
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
    assert recorded["expected_contract_version"] == 5
    assert recorded["archive_pool_size"] == 12


def _terminal_namespace(tmp_path: Path, **overrides: object) -> Namespace:
    defaults = {
        "database_url": "postgresql://prototype@postgres/db",
        "database_url_file": "",
        "archive_endpoint": "archive.example:9000",
        "owner": "terminal-worker-1",
        "max_jobs": 1,
        "lease_seconds": 30,
        "run_forever": True,
        "poll_interval_seconds": 0.01,
        "concurrency": 1,
        "database_pool_size": None,
        "archive_pool_size": None,
        "operating_snapshot_file": str(tmp_path / "live.json"),
        "terminal_snapshot_file": str(tmp_path / "terminal.json"),
        "disable_player_discovery": False,
    }
    defaults.update(overrides)
    return Namespace(**defaults)


def test_worker_writes_terminal_snapshot_after_quiescence(
    monkeypatch, capsys, tmp_path
) -> None:
    """Final per-replica snapshot lands on its own path after the loop."""
    from clashlens.cli import ProcessResult

    class FakeDatabase:
        def __init__(self, _url: str, **kwargs: object) -> None:
            del _url, kwargs

        def close(self) -> None:
            return None

        def maintain_queue(self, *, max_jobs: int) -> int:
            return 0

    class FakeArchive:
        def check_ready(self) -> bool:
            return True

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return None

        def process_until_idle(self, **kwargs: object) -> list:
            kwargs["stop_requested"].set()
            return [ProcessResult(0, "processed")]

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: FakeDatabase(_url))
    monkeypatch.setattr(
        cli, "_archive", lambda _args, **kwargs: FakeArchive()
    )
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    result = cli._run_worker(_terminal_namespace(tmp_path))
    assert result == 0
    live = json.loads((tmp_path / "live.json").read_text(encoding="utf-8"))
    assert "terminal" not in live
    terminal = json.loads(
        (tmp_path / "terminal.json").read_text(encoding="utf-8")
    )
    assert terminal["schema"] == "clashlens-worker-terminal-v1"
    assert terminal["producer"] == "worker"
    assert terminal["terminal"] is True
    assert terminal["captured_at"]
    assert terminal["process"]["id"]
    assert terminal["process"]["started_at"]
    assert isinstance(terminal["archive"]["remote_attempts"], dict)
    output = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ][-1]
    assert output["status"] == "stopped"
    assert output["terminal_snapshot"] == "written"


def test_worker_terminal_write_failure_stays_incomplete(
    monkeypatch, capsys, tmp_path
) -> None:
    """An unwritable terminal path fails visibly, never silently complete."""
    from clashlens.cli import ProcessResult

    class FakeDatabase:
        def __init__(self, _url: str, **kwargs: object) -> None:
            del _url, kwargs

        def close(self) -> None:
            return None

        def maintain_queue(self, *, max_jobs: int) -> int:
            return 0

    class FakeArchive:
        def check_ready(self) -> bool:
            return True

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return None

        def process_until_idle(self, **kwargs: object) -> list:
            kwargs["stop_requested"].set()
            return [ProcessResult(0, "processed")]

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: FakeDatabase(_url))
    monkeypatch.setattr(
        cli, "_archive", lambda _args, **kwargs: FakeArchive()
    )
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    result = cli._run_worker(
        _terminal_namespace(
            tmp_path, terminal_snapshot_file="/proc/clashlens-no-dir/t.json"
        )
    )
    assert result == 1
    output = [
        json.loads(line) for line in capsys.readouterr().out.splitlines()
    ][-1]
    assert output["status"] == "stopped"
    assert output["terminal_snapshot"] == "unavailable"


def test_worker_terminal_refused_when_heartbeat_stuck(
    monkeypatch, capsys, tmp_path
) -> None:
    """A heartbeat wedged in readiness blocks any terminal claim."""
    import threading as _threading
    import time as _time

    from clashlens.cli import ProcessResult

    release = _threading.Event()
    calls = []

    class FakeDatabase:
        def __init__(self, _url: str, **kwargs: object) -> None:
            del _url, kwargs

        def close(self) -> None:
            return None

        def maintain_queue(self, *, max_jobs: int) -> int:
            return 0

    class FakeArchive:
        def check_ready(self) -> bool:
            return True

        def readiness(self) -> dict:
            calls.append(_time.monotonic())
            if len(calls) > 1:
                assert release.wait(timeout=30)
            return {"ready": True}

        @property
        def remote_attempts(self) -> dict:
            return {}

    class FakeProcessor:
        def __init__(self, _database: object, _archive: object) -> None:
            return None

        def process_until_idle(self, **kwargs: object) -> list:
            _time.sleep(0.3)
            kwargs["stop_requested"].set()
            return [ProcessResult(0, "processed")]

    monkeypatch.setattr(cli, "Database", lambda _url, **kwargs: FakeDatabase(_url))
    monkeypatch.setattr(
        cli, "_archive", lambda _args, **kwargs: FakeArchive()
    )
    monkeypatch.setattr(cli, "ObservationProcessor", FakeProcessor)
    monkeypatch.setattr(cli, "WORKER_SNAPSHOT_INTERVAL_SECONDS", 0.01)
    written: list = []
    real_write = cli.write_private_snapshot

    def record_write(path, snapshot):
        written.append(str(path))
        return real_write(path, snapshot)

    monkeypatch.setattr(cli, "write_private_snapshot", record_write)
    terminal = tmp_path / "terminal.json"
    result = cli._run_worker(
        _terminal_namespace(
            tmp_path, terminal_snapshot_file=str(terminal)
        )
    )
    try:
        assert result == 1
        assert not terminal.exists()
        assert str(terminal) not in written
        output = [
            json.loads(line) for line in capsys.readouterr().out.splitlines()
        ][-1]
        assert output["status"] == "stopped"
        assert output["terminal_snapshot"] == "heartbeat_unfinished"
    finally:
        release.set()
