from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_api_migration import migrated_production_database

from clashlens import cli
from clashlens.hmac_proof import SigningInput, sign

SYNTHETIC_TOKEN = "synthetic-player-token-not-a-real-secret"
OFFICIAL_KEY_BYTES = b"clashlens-test-official-key-0123456789abcdef\n"


def _b64(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def _signed_request_headers(
    key: bytes,
    *,
    method: str,
    target: str,
    body: bytes,
    provider: str,
    provider_subject: str,
) -> dict[str, str]:
    request_id = str(uuid4())
    now = int(time.time())
    value = SigningInput(
        proof_version="clashlens-hmac-v1",
        caller_b64url=_b64("typescript-website"),
        key_id_b64url=_b64("current"),
        audience="clashlens-python-private-api",
        method=method,
        target_b64url=_b64(target),
        body_sha256=hashlib.sha256(body).hexdigest(),
        issued_at=str(now),
        expires_at=str(now + 10),
        request_id=request_id,
        provider_b64url=_b64(provider),
        provider_subject_b64url=_b64(provider_subject),
    )
    return {
        "X-ClashLens-Proof-Version": value.proof_version,
        "X-ClashLens-Caller": value.caller_b64url,
        "X-ClashLens-Key-Id": value.key_id_b64url,
        "X-ClashLens-Issued-At": value.issued_at,
        "X-ClashLens-Expires-At": value.expires_at,
        "X-ClashLens-Request-Id": value.request_id,
        "X-ClashLens-Provider": value.provider_b64url,
        "X-ClashLens-Provider-Subject": value.provider_subject_b64url,
        "X-ClashLens-Signature": sign(key, value),
        "Content-Type": "application/json",
    }


def test_serve_app_uses_api_database_and_wires_official_verification(
    database_url: str, tmp_path: Path
) -> None:
    with migrated_production_database(database_url) as connection_info:
        secret_file = tmp_path / "hmac.key"
        secret_file.write_text(
            base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
            + "\n",
            encoding="ascii",
        )
        key_file = tmp_path / "official.key"
        key_file.write_bytes(OFFICIAL_KEY_BYTES)
        arguments = cli.build_parser().parse_args(
            [
                "serve",
                "--database-url",
                connection_info,
                "--secret-file",
                str(secret_file),
                "--official-key-file",
                str(key_file),
                "--official-proxy-url",
                "http://127.0.0.1:9",
            ]
        )

        app, database = cli._serve_app(arguments)
        key = bytes(range(32))
        try:
            with TestClient(app) as client:
                target = "/v1/account"
                body = json.dumps(
                    {"username": "alice", "display_name": "Alice"},
                    separators=(",", ":"),
                ).encode()
                response = client.post(
                    target,
                    content=body,
                    headers=_signed_request_headers(
                        key,
                        method="POST",
                        target=target,
                        body=body,
                        provider="google",
                        provider_subject="google-user-1",
                    ),
                )
                assert response.status_code == 201, response.text

                verify_target = "/v1/players/%232PP/verifytoken"
                verify_body = json.dumps(
                    {"token": SYNTHETIC_TOKEN}, separators=(",", ":")
                ).encode()
                verify_response = client.post(
                    verify_target,
                    content=verify_body,
                    headers=_signed_request_headers(
                        key,
                        method="POST",
                        target=verify_target,
                        body=verify_body,
                        provider="google",
                        provider_subject="google-user-1",
                    ),
                )

            assert verify_response.status_code == 503
            assert verify_response.json()["status"] == "verification_unavailable"
            assert verify_response.json()["tag"] == "#2PP"
            assert SYNTHETIC_TOKEN not in verify_response.text

            with psycopg.connect(connection_info) as connection:
                fingerprint = connection.execute(
                    "SELECT credential_fingerprint FROM shared_api_credentials"
                ).fetchone()
                assert fingerprint is not None
                expected = hashlib.sha256(OFFICIAL_KEY_BYTES.rstrip(b"\n")).hexdigest()
                actual_fingerprint = (
                    fingerprint[0].decode("ascii")
                    if isinstance(fingerprint[0], bytes)
                    else fingerprint[0]
                )
                assert actual_fingerprint == expected
                leaked = connection.execute(
                    """
                    SELECT count(*)
                    FROM (
                        SELECT to_jsonb(private_api_requests) AS row
                        FROM private_api_requests
                        UNION ALL
                        SELECT to_jsonb(player_link_verification_audits)
                        FROM player_link_verification_audits
                        UNION ALL
                        SELECT to_jsonb(shared_api_credential_events)
                        FROM shared_api_credential_events
                    ) AS scanned
                    WHERE row::text LIKE %s
                    """,
                    (f"%{SYNTHETIC_TOKEN}%",),
                ).fetchone()
                assert leaked is not None and leaked[0] == 0
                links = connection.execute(
                    "SELECT count(*) FROM verified_player_links"
                ).fetchone()
                assert links is not None and links[0] == 0

            assert database.pool.closed is True
        finally:
            if not database.pool.closed:
                database.close()


def test_serve_app_closes_database_pool_when_startup_fails_after_registration(
    database_url: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    with migrated_production_database(database_url) as connection_info:
        secret_file = tmp_path / "hmac.key"
        secret_file.write_text(
            base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")
            + "\n",
            encoding="ascii",
        )
        key_file = tmp_path / "official.key"
        key_file.write_bytes(OFFICIAL_KEY_BYTES)
        arguments = cli.build_parser().parse_args(
            [
                "serve",
                "--database-url",
                connection_info,
                "--secret-file",
                str(secret_file),
                "--official-key-file",
                str(key_file),
                "--official-proxy-url",
                "http://127.0.0.1:9",
            ]
        )

        def fail_create_app(**kwargs):
            del kwargs
            raise RuntimeError("app construction exploded")

        monkeypatch.setattr(cli, "create_app", fail_create_app)
        closed: list[cli.ApiDatabase] = []
        original_close = cli.ApiDatabase.close

        def tracking_close(database: cli.ApiDatabase) -> None:
            closed.append(database)
            original_close(database)

        monkeypatch.setattr(cli.ApiDatabase, "close", tracking_close)

        with pytest.raises(RuntimeError, match="app construction exploded") as exc:
            cli._serve_app(arguments)

        # The pool opened and the official credential was registered before
        # app construction failed; the pool must be closed here because the
        # app lifespan never started.
        assert len(closed) == 1
        assert closed[0].pool.closed is True
        with psycopg.connect(connection_info) as connection:
            registered = connection.execute(
                "SELECT count(*) FROM shared_api_credentials"
            ).fetchone()
            assert registered is not None and registered[0] == 1
        captured = capsys.readouterr()
        assert OFFICIAL_KEY_BYTES.decode("ascii").strip() not in (
            captured.out + captured.err + str(exc.value)
        )
