from __future__ import annotations

import argparse
import base64
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from clashlens import api_verification, cli


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
        "service command failed" in captured.err
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
    assert "service command failed" in captured.err


def test_serve_rejects_credentialed_proxy_url(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(tmp_path)
    arguments.official_proxy_url = "http://operator:secret@proxy.example:8080"
    _skip_credential_registration(monkeypatch)
    closed = _track_pool_close(monkeypatch)

    with pytest.raises(
        ValueError, match="a non-credentialed fixed-egress proxy URL is required"
    ) as exc:
        cli._serve_app(arguments)
    assert len(closed) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err + str(exc.value)
    assert "operator" not in combined
    assert "secret" not in combined


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
        "service command failed" in captured.err
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
        api_verification,
        "register_official_credential",
        lambda database, fingerprint: None,
    )


def _assert_no_key_material(capsys, error: Exception) -> None:
    captured = capsys.readouterr()
    combined = captured.out + captured.err + str(error)
    assert "synthetic-official-key-bytes" not in combined


def test_serve_app_closes_database_pool_when_credential_registration_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    arguments = _serve_arguments(tmp_path)

    def fail_registration(database: cli.ApiDatabase, fingerprint: str) -> None:
        del database, fingerprint
        raise RuntimeError("credential registration exploded")

    monkeypatch.setattr(
        api_verification, "register_official_credential", fail_registration
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


def test_serve_never_writes_request_lines_to_the_log(tmp_path: Path, monkeypatch) -> None:
    started: list[dict] = []
    monkeypatch.setattr(cli, "_serve_app", lambda arguments: (object(), None))
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **options: started.append(options))

    assert cli.main([*_serve_argv(tmp_path), "--log-level", "info"]) == 0
    assert started and started[0]["access_log"] is False


def _serve_argv(tmp_path: Path) -> list[str]:
    arguments = _serve_arguments(tmp_path)
    return [
        "serve", "--database-url", arguments.database_url,
        "--secret-file", arguments.secret_file,
        "--official-key-file", arguments.official_key_file,
        "--official-proxy-url", arguments.official_proxy_url,
    ]


class _Recorder(BaseHTTPRequestHandler):
    status = 200
    body = b'{"ok": true}'

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        self.server.paths.append(self.path)
        self.send_response(self.status)
        if self.status == 302:
            self.send_header("Location", "/elsewhere")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


@contextmanager
def _server(status: int = 200, body: bytes = b'{"ok": true}'):
    handler = type("Handler", (_Recorder,), {"status": status, "body": body})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.paths = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _probe(tmp_path: Path, url: str) -> str:
    return cli._probe(cli.build_parser().parse_args([
        "probe", "--url", url, "--secret-file", _write_secret_file(tmp_path, bytes(range(32))),
    ]))


def test_probe_reads_only_the_address_it_is_given(tmp_path: Path, monkeypatch) -> None:
    with _server() as target, _server() as proxy:
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_port}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        url = f"http://127.0.0.1:{target.server_port}/v1/players/%232PP"
        assert _probe(tmp_path, url) == '{"ok": true}'
    assert target.paths == ["/v1/players/%232PP"]
    assert proxy.paths == []

    with _server(status=302) as moved:
        with pytest.raises(RuntimeError, match="probe failed"):
            _probe(tmp_path, f"http://127.0.0.1:{moved.server_port}/v1/players/%232PP")
    assert moved.paths == ["/v1/players/%232PP"]

    with _server(body=b"x" * (1024 * 1024 + 1)) as large:
        with pytest.raises(RuntimeError, match="too large"):
            _probe(tmp_path, f"http://127.0.0.1:{large.server_port}/v1/players/%232PP")

    for url in ("file:///etc/hostname", "ftp://127.0.0.1/x", "http:///v1/players"):
        with pytest.raises(RuntimeError, match="http or https"):
            _probe(tmp_path, url)
