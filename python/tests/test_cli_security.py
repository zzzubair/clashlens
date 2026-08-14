from __future__ import annotations

import argparse
import base64
import json
from argparse import Namespace
from pathlib import Path

import pytest

from clashlens.cli import (
    _archive,
    _file_value,
    _load_hmac_keys,
    build_parser,
    main,
)


def _secret_text(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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

        def enqueue_current_season_republication(
            self,
            *,
            max_jobs: int,
        ) -> list[int]:
            assert max_jobs == 7
            return [41, 42]

        def close(self) -> None:
            pass

    monkeypatch.setattr("clashlens.cli.Database", FakeDatabase)

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
    }


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
