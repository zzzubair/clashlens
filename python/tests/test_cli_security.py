from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clashlens.cli import (
    _archive,
    _file_value,
    _load_hmac_keys,
    _parse_api_keys,
    _run_collector,
    _run_ready,
    build_parser,
    main,
)
from clashlens.collector_http import ApiKey


def _secret_text(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_collector_api_keys_require_non_secret_labels() -> None:
    assert _parse_api_keys("regular-1=first,regular-2=second") == [
        ApiKey("regular-1", "first"),
        ApiKey("regular-2", "second"),
    ]
    with pytest.raises(ValueError, match="label=secret"):
        _parse_api_keys("unlabelled-secret")


@pytest.mark.parametrize("source", ["argument", "environment"])
@pytest.mark.parametrize("rate", ["0", "1", "25", "29", "30", "31", "1.5"])
def test_collector_rate_stays_under_thirty(monkeypatch, source: str, rate: str) -> None:
    monkeypatch.delenv("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY", raising=False)
    arguments = ["collector"]
    if source == "environment":
        monkeypatch.setenv("CLASHLENS_REQUESTS_PER_SECOND_PER_KEY", rate)
    else:
        arguments += ["--starts-per-second-per-key", rate]

    if rate in {"1", "25", "29"}:
        assert build_parser().parse_args(arguments).starts_per_second_per_key == int(rate)
    else:
        with pytest.raises(SystemExit) as error:
            build_parser().parse_args(arguments)
        assert error.value.code == 2


@pytest.mark.parametrize("count", [3, 4, 6, 9, 10])
def test_collector_loads_four_to_nine_regular_keys(monkeypatch, count: int) -> None:
    labels = ["normal-1", "normal-2", "normal-3", "normal-4", "normal-5", "normal-6"]
    labels += ["extra-1", "extra-2", "extra-3", "extra-4"]
    arguments = build_parser().parse_args(["collector"])
    arguments.regular_api_keys = ",".join(
        f"{label}=fixture-{label}" for label in labels[:count]
    )
    arguments.interactive_api_keys = "interactive-1=fixture-interactive"

    class KeysAccepted(Exception):
        pass

    def stop_after_key_checks(_database_url: str) -> None:
        raise KeysAccepted

    monkeypatch.setattr("clashlens.cli._database_url", lambda _arguments: "")
    monkeypatch.setattr("clashlens.cli.CollectorDatabase", stop_after_key_checks)

    assert arguments.starts_per_second_per_key == 25
    expected = KeysAccepted if 4 <= count <= 9 else ValueError
    with pytest.raises(expected):
        _run_collector(arguments)


@pytest.mark.parametrize(
    ("keys", "rate", "setting", "in_flight"),
    [(9, 28, None, 384), (7, 28, None, 384), (4, 25, None, 256), (7, 28, "300", 300)],
)
def test_collector_sizes_checks_in_flight_from_its_keys(
    monkeypatch, keys: int, rate: int, setting: str | None, in_flight: int
) -> None:
    # Two seconds of key starts keeps the keys, not the slots, setting the pace.
    monkeypatch.delenv("CLASHLENS_REGULAR_PARALLELISM", raising=False)
    if setting is not None:
        monkeypatch.setenv("CLASHLENS_REGULAR_PARALLELISM", setting)
    arguments = build_parser().parse_args(
        ["collector", "--starts-per-second-per-key", str(rate)]
    )
    arguments.regular_api_keys = ",".join(
        f"regular-{i}=fixture-{i}" for i in range(keys)
    )
    arguments.interactive_api_keys = "interactive-1=fixture-interactive"

    class Sized(Exception):
        pass

    def collector(**kwargs: object) -> None:
        raise Sized(kwargs["regular_parallelism"])

    database = SimpleNamespace(register_interactive_key=lambda *_args, **_kwargs: None)
    monkeypatch.setattr("clashlens.cli._database_url", lambda _arguments: "")
    monkeypatch.setattr("clashlens.cli.CollectorDatabase", lambda _url: database)
    monkeypatch.setattr(
        "clashlens.cli._archive",
        lambda *_args, **_kwargs: SimpleNamespace(spool=None, archive=None),
    )
    monkeypatch.setattr("clashlens.cli.SpoolFirstReader", SimpleNamespace)
    monkeypatch.setattr("clashlens.cli.Collector", collector)

    with pytest.raises(Sized) as sized:
        _run_collector(arguments)
    assert sized.value.args == (in_flight,)


@pytest.mark.parametrize(
    ("keys", "setting", "rate", "concurrency", "threads"),
    [
        (7, None, 25, 6, (350, 48)),
        (7, "300", 25, 6, (300, 48)),
        (7, None, 29, 6, (384, 48)),
        # Nine keys at 28 a second would size 504 checks; with 60 request threads
        # they still leave 64 of 512.
        (9, None, 28, 6, (384, 60)),
        # The widest request pool shrinks to what the save threads leave.
        (7, None, 28, 32, (384, 64)),
        (9, "256", 28, 32, (256, 192)),
    ],
)
def test_collector_threads_stay_under_the_container_limit(
    monkeypatch, keys: int, setting: str | None, rate: int, concurrency: int,
    threads: tuple[int, int],
) -> None:
    # Seven keys at 29 a second would size 406 checks; they get 384 save threads.
    monkeypatch.delenv("CLASHLENS_REGULAR_PARALLELISM", raising=False)
    if setting is not None:
        monkeypatch.setenv("CLASHLENS_REGULAR_PARALLELISM", setting)
    with pytest.raises(SystemExit):
        build_parser().parse_args(["collector", "--regular-parallelism", "385"])
    arguments = build_parser().parse_args(
        ["collector", "--starts-per-second-per-key", str(rate),
         "--concurrency-per-key", str(concurrency)]
    )
    arguments.regular_api_keys = ",".join(f"regular-{i}=fixture-{i}" for i in range(keys))
    requests: list[int] = []

    def client(*_args: object, max_connections: int, **_kwargs: object) -> object:
        requests.append(max_connections)
        return object()

    arguments.interactive_api_keys = "interactive-1=fixture-interactive"

    class Started(Exception):
        pass

    class Collector:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> None:
            executor = asyncio.get_running_loop()._default_executor
            raise Started(executor._max_workers, *requests)

    closed = SimpleNamespace(close=lambda: None)
    database = SimpleNamespace(
        register_interactive_key=lambda *_args, **_kwargs: None, close=lambda: None
    )
    monkeypatch.setattr("clashlens.cli._database_url", lambda _arguments: "")
    monkeypatch.setattr("clashlens.cli.CollectorDatabase", lambda _url: database)
    monkeypatch.setattr(
        "clashlens.cli._archive",
        lambda *_args, **_kwargs: SimpleNamespace(spool=closed, archive=None),
    )
    monkeypatch.setattr("clashlens.cli.SpoolFirstReader", SimpleNamespace)
    monkeypatch.setattr("clashlens.cli.Collector", Collector)
    monkeypatch.setattr("clashlens.cli.OfficialApiClient", client)

    with pytest.raises(Started) as started:
        _run_collector(arguments)
    assert started.value.args == threads
    # 32 upload threads and 64 spare fill the rest of the container's 544.
    assert sum(started.value.args) <= 448


@pytest.mark.parametrize(
    ("setting", "forwarded"),
    [
        (None, None),
        ("300", "300"),
        ("384", "384"),
        ("0", None),
        ("385", None),
        ("3x", None),
    ],
)
def test_ops_rejects_a_bad_check_limit_before_stopping_and_forwards_a_good_one(
    tmp_path: Path, setting: str | None, forwarded: str | None
) -> None:
    ops = Path(__file__).resolve().parents[2] / "ops"
    # The optional promotion re-check rate is forwarded only when set.
    promotion_rate = "0" if setting == "384" else ""
    # `./ops up` runs with host checks stubbed; stopping the running services
    # is recorded, then the environment is written in its place.
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1" help >/dev/null
STATE_DIR="$2"
MODE=fixture
RELEASE=([POSTGRES_IMAGE]=postgres [COLLECTOR_IMAGE]=collector [PYTHON_IMAGE]=python [WEBSITE_IMAGE]=website)
[[ -z "$3" ]] || CONFIG[CLASHLENS_REGULAR_PARALLELISM]=$3
[[ -z "$4" ]] || CONFIG[CLASHLENS_PROMOTION_RECHECK_PER_SECOND]=$4
for step in require_host load_release guard_generated_units guard_existing_resources \
    guard_trusted_proxy_ip guard_network_subnet cleanup_stale_admin_state ensure_linger \
    migrate_legacy_units guard_systemd_units write_alert_intent; do
  eval "$step() { :; }"
done
stop_units() { touch "$STATE_DIR/stopped"; write_environment; exit 0; }
up_stack
""",
            "test-ops-environment",
            str(ops),
            str(tmp_path),
            setting or "",
            promotion_rate,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if setting is not None and forwarded is None:
        assert result.returncode != 0
        assert "CLASHLENS_REGULAR_PARALLELISM must be a whole number" in result.stderr
        assert not (tmp_path / "stopped").exists()
        return
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "stopped").exists()
    collector = dict(
        line.split("=", 1)
        for line in (tmp_path / "env/collector.env").read_text().splitlines()
    )
    assert collector.get("CLASHLENS_REGULAR_PARALLELISM") == forwarded
    assert collector.get("CLASHLENS_PROMOTION_RECHECK_PER_SECOND") == (promotion_rate or None)


@pytest.mark.parametrize(
    ("limits", "threads", "pool", "refusal"),
    [
        (("6", "8"), "12", "", None),
        (("", ""), "12", "", None),
        (("0", ""), "12", "", "CLASHLENS_BACKGROUND_JOB_LIMIT must be a whole number"),
        (("", "65"), "12", "", "CLASHLENS_DAY_RECHECK_JOB_LIMIT must be a whole number"),
        (("", ""), "12", "8", "CLASHLENS_WORKER_DATABASE_POOL_SIZE must be at least"),
        (("", ""), "20", "", "allows at most 16 database connections"),
    ],
)
def test_ops_forwards_background_limits_and_keeps_a_connection_a_thread(
    tmp_path: Path, limits: tuple[str, str], threads: str, pool: str, refusal: str | None
) -> None:
    # The deploy settings tune background work without a code change; a pool
    # smaller than the worker's 12 threads would make jobs wait for connections.
    ops = Path(__file__).resolve().parents[2] / "ops"
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1" help >/dev/null
STATE_DIR="$2"
MODE=fixture
load_fixture_config
[[ -z "$3" ]] || CONFIG[CLASHLENS_BACKGROUND_JOB_LIMIT]=$3
[[ -z "$4" ]] || CONFIG[CLASHLENS_DAY_RECHECK_JOB_LIMIT]=$4
WORKER_CONCURRENCY=$5; WORKER_DB_POOL=${6:-$5}
validate_runtime_values
write_environment
""",
            "test-ops-background",
            str(ops),
            str(tmp_path),
            *limits,
            threads,
            pool,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if refusal is not None:
        assert result.returncode != 0
        assert refusal in result.stderr
        assert not (tmp_path / "env/worker.env").exists()
        return
    assert result.returncode == 0, result.stderr
    worker = dict(
        line.split("=", 1) for line in (tmp_path / "env/worker.env").read_text().splitlines()
    )
    assert worker.get("CLASHLENS_BACKGROUND_JOB_LIMIT") == (limits[0] or None)
    assert worker.get("CLASHLENS_DAY_RECHECK_JOB_LIMIT") == (limits[1] or None)


@pytest.mark.parametrize(
    ("rate", "accepted"),
    [("20", True), ("0", True), ("2.5", True), ("20/s", False), ("-1", False), ("inf", False)],
)
def test_ops_rejects_a_bad_promotion_rate_before_stopping(
    tmp_path: Path, rate: str, accepted: bool
) -> None:
    ops = Path(__file__).resolve().parents[2] / "ops"
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1" help >/dev/null
STATE_DIR="$2"
MODE=fixture
RELEASE=([POSTGRES_IMAGE]=postgres [COLLECTOR_IMAGE]=collector [PYTHON_IMAGE]=python [WEBSITE_IMAGE]=website)
CONFIG[CLASHLENS_PROMOTION_RECHECK_PER_SECOND]=$3
for step in require_host load_release guard_generated_units guard_existing_resources \
    guard_trusted_proxy_ip guard_network_subnet cleanup_stale_admin_state ensure_linger \
    migrate_legacy_units guard_systemd_units write_alert_intent; do
  eval "$step() { :; }"
done
stop_units() { touch "$STATE_DIR/stopped"; exit 0; }
up_stack
""",
            "test-ops-promotion-rate",
            str(ops),
            str(tmp_path),
            rate,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "stopped").exists() is accepted, result.stderr
    if not accepted:
        assert result.returncode != 0
        assert "CLASHLENS_PROMOTION_RECHECK_PER_SECOND must be a number" in result.stderr


@pytest.mark.parametrize(
    ("pids", "setting", "keys", "accepted"),
    [
        # Six keys at 25 a second: 300 save, 42 request and 32 upload
        # threads, plus 64.
        ("512", None, 6, True),
        ("437", None, 6, False),
        ("394", "256", 6, True),
        # Nine keys at 28 a second: 384 save, 60 request and 32 upload
        # threads, plus 64.
        ("540", None, 9, True),
        ("539", None, 9, False),
    ],
)
def test_ops_refuses_collector_threads_beyond_its_process_limit(
    tmp_path: Path, pids: str, setting: str | None, keys: int, accepted: bool
) -> None:
    ops = Path(__file__).resolve().parents[2] / "ops"
    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1" help >/dev/null
STATE_DIR="$2"
load_fixture_config
COLLECTOR_PIDS=$3
[[ -z "$4" ]] || CONFIG[CLASHLENS_REGULAR_PARALLELISM]=$4
(( $5 == 6 )) || REGULAR_KEY_NAMES=n-1,n-2,n-3,n-4,n-5,n-6,n-7,n-8,n-9 KEY_RATE=28
validate_runtime_values
""",
            "test-ops-threads",
            str(ops),
            str(tmp_path),
            pids,
            setting or "",
            str(keys),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert f"exceed CLASHLENS_COLLECTOR_PIDS={pids}" in result.stderr


def test_cli_loads_current_and_previous_hmac_keys_from_files(tmp_path: Path) -> None:
    current = tmp_path / "current.key"
    previous = tmp_path / "previous.key"
    current.write_text(_secret_text(bytes(range(32))) + "\n", encoding="ascii")
    previous.write_text(_secret_text(bytes(range(32, 64))), encoding="ascii")
    arguments = Namespace(
        caller="typescript-website",
        key_id="current",
        secret_file=str(current),
        previous_key_id="previous",
        previous_secret_file=str(previous),
    )

    keys = _load_hmac_keys(arguments)

    assert keys[("typescript-website", "current")] == bytes(range(32))
    assert keys[("typescript-website", "previous")] == bytes(range(32, 64))


def test_file_backed_database_value_accepts_one_final_lf(tmp_path: Path) -> None:
    value_file = tmp_path / "database-url"
    value_file.write_text("postgresql://prototype@postgres/db\n", encoding="utf-8")

    assert (
        _file_value(str(value_file), "", "database URL")
        == "postgresql://prototype@postgres/db"
    )


def test_cli_error_output_does_not_include_database_url_or_secret_path(
    tmp_path: Path, capsys
) -> None:
    secret_path = tmp_path / "missing-secret.key"
    database_url = "postgresql://user:password-that-must-not-print@db/prototype"

    result = main(
        [
            "serve",
            "--database-url",
            database_url,
            "--secret-file",
            str(secret_path),
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert database_url not in captured.err
    assert str(secret_path) not in captured.err


class PoisonedQueueError(Exception):
    """Unexpected exception whose message carries credential-like detail."""

    def __init__(self, database_url: str) -> None:
        super().__init__(
            f"poison detail: {database_url} job=42 archive=s3://evidence/obs-7f3a"
        )


def test_unexpected_exception_prints_stable_error_but_never_details(
    monkeypatch, capsys
) -> None:
    secret_password = "hunter2"
    database_url = f"postgresql://user:{secret_password}@db/production"

    class FakeDatabase:
        def __init__(self, _database_url: str) -> None:
            return

        def queue_health(self) -> dict[str, int | float | None]:
            raise PoisonedQueueError(database_url)

        def close(self) -> None:
            return

    monkeypatch.setattr("clashlens.cli.Database", FakeDatabase)

    result = main(["queue-status", "--database-url", database_url])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.err == "service command failed: internal_error\n"
    assert "poison detail" not in captured.err
    assert secret_password not in captured.err
    assert database_url not in captured.err
    assert "job=42" not in captured.err
    assert "s3://evidence/obs-7f3a" not in captured.err
    assert "PoisonedQueueError" not in captured.err
    assert "Traceback" not in captured.err


def test_value_error_boundary_still_prints_only_the_class_name(
    monkeypatch, capsys
) -> None:
    secret_password = "hunter2"
    database_url = f"postgresql://user:{secret_password}@db/production"

    class FakeDatabase:
        def __init__(self, _database_url: str) -> None:
            raise ValueError(f"cannot connect to {_database_url}")

        def close(self) -> None:
            return

    monkeypatch.setattr("clashlens.cli.Database", FakeDatabase)

    result = main(["queue-status", "--database-url", database_url])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.err == "service command failed: ValueError\n"
    assert secret_password not in captured.err
    assert database_url not in captured.err
    assert "Traceback" not in captured.err


def test_unexpected_exception_with_attacker_controlled_class_name_is_normalized(
    monkeypatch, capsys
) -> None:
    secret = "attacker-secret-9b41"
    EvilError = type(f"EvilError\nleaks {secret}", (Exception,), {})

    class FakeDatabase:
        def __init__(self, _database_url: str) -> None:
            return

        def queue_health(self) -> dict[str, int | float | None]:
            raise EvilError(f"leaks {secret}")

        def close(self) -> None:
            return

    monkeypatch.setattr("clashlens.cli.Database", FakeDatabase)

    result = main(["queue-status", "--database-url", "postgresql://user@db/production"])
    captured = capsys.readouterr()

    assert result == 1
    assert captured.err == "service command failed: internal_error\n"
    assert secret not in captured.err
    assert "EvilError" not in captured.err
    assert "Traceback" not in captured.err


def test_archive_requires_file_backed_credentials() -> None:
    arguments = build_parser().parse_args(
        [
            "worker",
            "--database-url",
            "postgresql://prototype@postgres/db",
            "--archive-endpoint",
            "archive.example:9000",
        ]
    )

    with pytest.raises(ValueError, match="archive access and secret key are required"):
        _archive(arguments)


def _worker_arguments(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        [
            "worker",
            "--database-url",
            "postgresql://prototype@postgres/db",
            "--archive-endpoint",
            "archive.example:9000",
            *extra,
        ]
    )


def test_worker_concurrency_defaults_to_one_and_pools_are_auto() -> None:
    arguments = _worker_arguments()

    assert arguments.concurrency == 1
    assert arguments.database_pool_size is None
    assert arguments.archive_pool_size is None


@pytest.mark.parametrize("value", ["0", "-1", "33", "abc", "1.5", ""])
def test_worker_concurrency_rejects_invalid_bounds(value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _worker_arguments("--concurrency", value)
    assert excinfo.value.code == 2


def test_worker_concurrency_accepts_the_maximum_bound() -> None:
    arguments = _worker_arguments("--concurrency", "32")
    assert arguments.concurrency == 32


@pytest.mark.parametrize("value", ["0", "-1", "65", "many"])
def test_worker_pool_sizes_reject_invalid_bounds(value: str) -> None:
    for flag in ("--database-pool-size", "--archive-pool-size"):
        with pytest.raises(SystemExit) as excinfo:
            _worker_arguments(flag, value)
        assert excinfo.value.code == 2


def test_worker_pool_size_flags_accept_valid_bounds() -> None:
    arguments = _worker_arguments(
        "--database-pool-size", "8", "--archive-pool-size", "20"
    )
    assert arguments.database_pool_size == 8
    assert arguments.archive_pool_size == 20


def test_worker_discovery_defaults_enabled_and_disables_explicitly() -> None:
    assert _worker_arguments().disable_player_discovery is False
    assert (
        _worker_arguments("--disable-player-discovery").disable_player_discovery is True
    )


def test_worker_invalid_concurrency_output_does_not_expose_archive_credentials(
    capsys,
) -> None:
    secret_key = "fixture-archive-secret-key-9f2c"
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            [
                "worker",
                "--database-url",
                "postgresql://user:***@db/prototype",
                "--archive-endpoint",
                "archive.example:9000",
                "--archive-access-key",
                "fixture-access-key",
                "--archive-secret-key",
                secret_key,
                "--concurrency",
                "0",
            ]
        )
    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert secret_key not in captured.err
    assert secret_key not in captured.out


def test_queue_status_reports_existing_queue_health(monkeypatch, capsys) -> None:
    expected = {
        "pending": 3,
        "waiting_retry": 2,
        "leased": 1,
        "failed": 0,
        "oldest_due_seconds": 4.5,
    }

    class FakeDatabase:
        def __init__(self, database_url: str) -> None:
            assert database_url == "postgresql://worker@postgres/clashlens"

        def queue_health(self) -> dict[str, int | float | None]:
            return expected

        def close(self) -> None:
            pass

    monkeypatch.setattr("clashlens.cli.Database", FakeDatabase)

    result = main(
        [
            "queue-status",
            "--database-url",
            "postgresql://worker@postgres/clashlens",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == expected


def test_current_season_republication_command_is_bounded_and_reports_jobs(
    monkeypatch,
    capsys,
) -> None:
    class FakeDatabase:
        def __init__(self, database_url: str) -> None:
            assert database_url == "postgresql://worker@postgres/clashlens"

        def close(self) -> None:
            pass

    def fake_enqueue(
        database: FakeDatabase,
        *,
        max_jobs: int,
    ) -> dict[str, object]:
        assert max_jobs == 7
        return {"job_ids": [41, 42], "evaluated_count": 0, "failure_reasons": {}}

    monkeypatch.setattr("clashlens.battle_day_repair.Database", FakeDatabase)
    monkeypatch.setattr(
        "clashlens.battle_day_repair.enqueue_current_season_republication",
        fake_enqueue,
    )

    result = main(
        [
            "republish-current-season",
            "--database-url",
            "postgresql://worker@postgres/clashlens",
            "--max-jobs",
            "7",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "enqueued_count": 2,
        "job_ids": [41, 42],
        "evaluated_count": 0,
        "failure_reasons": {},
    }


@pytest.mark.parametrize("extra,action", [
    (["--repair", "preview", "--season", "1791176400"], "preview"),
    (["--repair", "queue", "--season", "1791176400"], "queue"),
    (["--repair", "receipt", "--season", "1791176400"], "receipt"),
    (["--repair", "queue"], None),
    (["--repair", "queue", "--boards", "queue", "--season", "1791176400"], None),
    (["--repair", "queue", "--armies", "queue", "--season", "1791176400"], None),
])
def test_season_repair_needs_a_season_and_runs_alone(
    monkeypatch, extra: list[str], action: str | None
) -> None:
    calls = []
    monkeypatch.setattr("clashlens.battle_day_repair.Database",
                        lambda url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        "clashlens.domain_repair.season_repair",
        lambda database, season, action, *, max_jobs: calls.append(
            (season, action, max_jobs)) or {},
    )
    arguments = ["republish-current-season", "--database-url",
                 "postgresql://worker@postgres/clashlens", *extra]
    if action is None:
        with pytest.raises(SystemExit):
            main(arguments)
        assert calls == []
    else:
        assert main(arguments) == 0
        assert calls == [("1791176400", action, 100)]


@pytest.mark.parametrize("report,status", [({"resets": []}, 0), ({"refused": "window"}, 1)])
def test_army_corrections_command_passes_its_cap_and_fails_when_refused(
    monkeypatch, report: dict[str, object], status: int
) -> None:
    calls = []
    monkeypatch.setattr("clashlens.battle_day_repair.Database",
                        lambda url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        "clashlens.boundary.queue_army_corrections",
        lambda database, season, *, queue, max_jobs: calls.append(
            (season, queue, max_jobs)) or report,
    )
    assert main(["republish-current-season", "--database-url",
                 "postgresql://worker@postgres/clashlens", "--armies", "queue",
                 "--max-jobs", "2", "--season", "1791176400"]) == status
    assert calls == [("1791176400", True, 2)]


@pytest.mark.parametrize("value", ["0", "1001", "many"])
def test_current_season_republication_command_rejects_unbounded_batches(
    value: str,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            [
                "republish-current-season",
                "--database-url",
                "postgresql://worker@postgres/clashlens",
                "--max-jobs",
                value,
            ]
        )
    assert excinfo.value.code == 2


def _ready_namespace() -> Namespace:
    return Namespace(
        database_url="postgresql://stub",
        database_url_file="",
        expected_contract_version=3,
        archive_endpoint="archive.example.test:9000",
        archive_bucket="evidence",
        archive_region="us-east-1",
        archive_access_key="access",
        archive_secret_key="secret",
        archive_insecure_test_only=False,
    )


class _ReadyDatabaseStub:
    def __init__(self, _url: str) -> None:
        pass

    def is_ready(self, *, expected_contract_version: int) -> bool:
        del expected_contract_version
        return True

    def close(self) -> None:
        return None


class _ReadyArchiveStub:
    def __init__(self, remote_health: str) -> None:
        self._remote_health = remote_health

    def check_ready(self) -> bool:
        return True

    def readiness(self) -> dict[str, object]:
        return {"ready": True}

    def check_marker_health(self) -> str:
        return self._remote_health


@pytest.mark.parametrize(
    ("remote_health", "expected_exit"),
    [("terminal", 1), ("degraded", 0), ("ready", 0)],
)
def test_cli_ready_fails_on_terminal_marker_mismatch_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    remote_health: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr("clashlens.cli.Database", _ReadyDatabaseStub)
    monkeypatch.setattr(
        "clashlens.cli._archive",
        lambda _arguments, **_kwargs: _ReadyArchiveStub(remote_health),
    )

    exit_code = _run_ready(_ready_namespace())

    assert exit_code == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["remote_health"] == remote_health
    assert payload["status"] == ("ready" if expected_exit == 0 else "not_ready")


def _materialize_arguments(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        [
            "materialize-season-summaries",
            "--database-url",
            "postgresql://prototype@postgres/db",
            "--season-id",
            "1785714000",
            *extra,
        ]
    )


def test_materialize_cursor_defaults_to_zero() -> None:
    arguments = _materialize_arguments()

    assert arguments.after_player_id == 0
    assert arguments.max_players == 100
    assert arguments.apply is False


def test_materialize_cursor_accepts_a_valid_id() -> None:
    arguments = _materialize_arguments("--after-player-id", "12345")

    assert arguments.after_player_id == 12345


@pytest.mark.parametrize("value", ["-1", "abc", "1.5", ""])
def test_materialize_cursor_rejects_invalid_bounds(value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _materialize_arguments("--after-player-id", value)
    assert excinfo.value.code == 2


def _retire_arguments(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        [
            "retire-season-detail",
            "--database-url",
            "postgresql://prototype@postgres/db",
            "--season-id",
            "1785714000",
            *extra,
        ]
    )


def test_retire_batch_defaults_and_requires_season() -> None:
    arguments = _retire_arguments()

    assert arguments.max_rows == 500
    assert arguments.apply is False
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(
            [
                "retire-season-detail",
                "--database-url",
                "postgresql://prototype@postgres/db",
            ]
        )
    assert excinfo.value.code == 2


@pytest.mark.parametrize("value", ["0", "1001", "abc"])
def test_retire_batch_rejects_out_of_range(value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _retire_arguments("--max-rows", value)
    assert excinfo.value.code == 2


def test_finalize_and_measure_commands_parse() -> None:
    parser = build_parser()
    finalize = parser.parse_args(
        [
            "finalize-season-detail",
            "--database-url",
            "postgresql://prototype@postgres/db",
            "--season-id",
            "1785714000",
        ]
    )
    assert finalize.apply is False
    measure = parser.parse_args(
        [
            "measure-season-storage",
            "--database-url",
            "postgresql://prototype@postgres/db",
        ]
    )
    assert measure.season_id == ""
    assert measure.players == 12500
