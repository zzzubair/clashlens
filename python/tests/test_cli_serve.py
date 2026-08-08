from __future__ import annotations

import argparse
import base64
from pathlib import Path

import pytest

from clashlens import cli


def _secret_text(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _write_secret_file(tmp_path: Path, value: bytes) -> str:
    secret_file = tmp_path / "hmac.key"
    secret_file.write_text(_secret_text(value) + "\n", encoding="ascii")
    return str(secret_file)


def _write_official_key_file(tmp_path: Path, value: bytes) -> str:
    key_file = tmp_path / "official.key"
    key_file.write_bytes(value)
    return str(key_file)


def _serve_arguments(
    tmp_path: Path, *, secret_path: str | None = None
) -> argparse.Namespace:
    key_file = _write_official_key_file(tmp_path, b"synthetic-official-key-bytes\n")
    secret_file = secret_path or _write_secret_file(tmp_path, bytes(range(32)))
    return cli.build_parser().parse_args(
        [
            "serve",
            "--database-url",
            "postgresql://user:***@127.0.0.1:1/production",
            "--secret-file",
            secret_file,
            "--official-key-file",
            key_file,
            "--official-proxy-url",
            "http://proxy.example:8080",
        ]
    )


def test_serve_requires_file_backed_official_key(tmp_path: Path, capsys) -> None:
    secret_file = _write_secret_file(tmp_path, bytes(range(32)))

    result = cli.main(
        [
            "serve",
            "--database-url",
            "postgresql://user:***@db/production",
            "--secret-file",
            secret_file,
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "postgresql://user" not in captured.err
    assert (
        "official API key" in captured.err or "prototype command failed" in captured.err
    )


def test_serve_requires_fixed_egress_proxy(tmp_path: Path, capsys) -> None:
    secret_file = _write_secret_file(tmp_path, bytes(range(32)))
    key_file = _write_official_key_file(tmp_path, b"synthetic-official-key-bytes\n")

    result = cli.main(
        [
            "serve",
            "--database-url",
            "postgresql://user:***@db/production",
            "--secret-file",
            secret_file,
            "--official-key-file",
            key_file,
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "proxy" in captured.err or "prototype command failed" in captured.err


def test_serve_rejects_credentialed_proxy_url(tmp_path: Path, capsys) -> None:
    secret_file = _write_secret_file(tmp_path, bytes(range(32)))
    key_file = _write_official_key_file(tmp_path, b"synthetic-official-key-bytes\n")

    result = cli.main(
        [
            "serve",
            "--database-url",
            "postgresql://user:***@db/production",
            "--secret-file",
            secret_file,
            "--official-key-file",
            key_file,
            "--official-proxy-url",
            "http://operator:secret@proxy.example:8080",
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "operator" not in captured.err
    assert "secret" not in captured.err


def test_serve_rejects_invalid_official_key_file_bytes(tmp_path: Path, capsys) -> None:
    secret_file = _write_secret_file(tmp_path, bytes(range(32)))
    key_file = _write_official_key_file(tmp_path, b"not ascii \xff bytes\n")

    result = cli.main(
        [
            "serve",
            "--database-url",
            "postgresql://user:***@db/production",
            "--secret-file",
            secret_file,
            "--official-key-file",
            key_file,
            "--official-proxy-url",
            "http://proxy.example:8080",
        ]
    )
    captured = capsys.readouterr()

    assert result == 1
    assert (
        "official API key file" in captured.err
        or "prototype command failed" in captured.err
    )


def _track_pool_close(monkeypatch) -> list[cli.ApiDatabase]:
    closed: list[cli.ApiDatabase] = []
    original_close = cli.ApiDatabase.close

    def tracking_close(database: cli.ApiDatabase) -> None:
        closed.append(database)
        original_close(database)

    monkeypatch.setattr(cli.ApiDatabase, "close", tracking_close)
    return closed


def _skip_credential_registration(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.ApiDatabase,
        "register_official_credential",
        lambda self, fingerprint: None,
    )


def _assert_no_key_material(capsys, error: Exception) -> None:
    captured = capsys.readouterr()
    combined = captured.out + captured.err + str(error)
    assert "synthetic-official-key-bytes" not in combined


def test_serve_app_closes_database_pool_when_credential_registration_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(tmp_path)

    def fail_registration(self, fingerprint: str) -> None:
        del self, fingerprint
        raise RuntimeError("credential registration exploded")

    monkeypatch.setattr(
        cli.ApiDatabase, "register_official_credential", fail_registration
    )
    closed = _track_pool_close(monkeypatch)

    with pytest.raises(RuntimeError, match="credential registration exploded") as exc:
        cli._serve_app(arguments)
    assert len(closed) == 1
    assert closed[0].pool.closed is True
    _assert_no_key_material(capsys, exc.value)


def test_serve_app_closes_database_pool_when_verification_client_construction_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(tmp_path)
    _skip_credential_registration(monkeypatch)

    def fail_construction(self, **kwargs) -> None:
        del self, kwargs
        raise RuntimeError("verification client exploded")

    monkeypatch.setattr(cli.OfficialVerificationClient, "__init__", fail_construction)
    closed = _track_pool_close(monkeypatch)

    with pytest.raises(RuntimeError, match="verification client exploded") as exc:
        cli._serve_app(arguments)
    assert len(closed) == 1
    assert closed[0].pool.closed is True
    _assert_no_key_material(capsys, exc.value)


def test_serve_app_closes_database_pool_when_hmac_loading_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(
        tmp_path, secret_path=str(tmp_path / "missing-hmac.key")
    )
    _skip_credential_registration(monkeypatch)
    closed = _track_pool_close(monkeypatch)

    with pytest.raises(ValueError, match="HMAC secret file could not be read") as exc:
        cli._serve_app(arguments)
    assert len(closed) == 1
    assert closed[0].pool.closed is True
    _assert_no_key_material(capsys, exc.value)


def test_serve_app_closes_database_pool_when_create_app_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(tmp_path)
    _skip_credential_registration(monkeypatch)

    def fail_create_app(**kwargs):
        del kwargs
        raise RuntimeError("app construction exploded")

    monkeypatch.setattr(cli, "create_app", fail_create_app)
    closed = _track_pool_close(monkeypatch)

    with pytest.raises(RuntimeError, match="app construction exploded") as exc:
        cli._serve_app(arguments)
    assert len(closed) == 1
    assert closed[0].pool.closed is True
    _assert_no_key_material(capsys, exc.value)
