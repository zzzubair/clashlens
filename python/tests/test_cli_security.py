from __future__ import annotations

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
