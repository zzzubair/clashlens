from __future__ import annotations

import json
from uuid import uuid4

import psycopg
import pytest
from test_api_migration import migrated_production_database

from clashlens import cli
from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.verification import OfficialVerificationResponse


class FakeVerificationClient:
    def __init__(self, http_status: int, body: bytes) -> None:
        self.http_status = http_status
        self.body = body
        self.seen_tokens: list[str] = []

    def verify(self, normalized_tag: str, player_token: str):
        self.seen_tokens.append(player_token)
        return OfficialVerificationResponse(self.http_status, self.body)


def _seed_account_with_verified_player(
    database: ApiDatabase,
    connection_info: str,
    *,
    google_subject: str,
    username: str,
) -> str:
    result = database.create_account(
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject=google_subject,
            account_id=None,
            operation="account.create",
            method="POST",
            request_target="/v1/account",
            identity={"username": username},
        ),
        username=username,
        normalized_username=username,
        display_name=username.title(),
    )
    assert result.status_code == 201
    account = database.resolve_account("google", google_subject)
    assert account is not None
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            """
            INSERT INTO players (normalized_tag, active)
            VALUES ('#2PP', false) ON CONFLICT (normalized_tag) DO NOTHING
            """
        )
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        request_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO private_api_requests (
                request_id, caller, provider, provider_subject, account_id,
                operation, method, request_target, identity_json, state,
                response_status, response_json, completed_at
            ) VALUES (
                %s, 'typescript-website', 'google', %s, %s,
                'player_links.verify', 'POST', '/v1/players/#2PP/verifytoken',
                '{"tag":"#2PP"}'::jsonb, 'complete', 200,
                '{"status":"linked","tag":"#2PP"}'::jsonb, clock_timestamp()
            )
            """,
            (request_id, google_subject, account.internal_id),
        )
        connection.execute(
            """
            INSERT INTO verified_player_links (player_id, account_id, verification_request_id)
            VALUES (%s, %s, %s)
            """,
            (player_id, account.internal_id, request_id),
        )
        public_id = connection.execute(
            "SELECT public_id FROM clash_lens_accounts WHERE id = %s",
            (account.internal_id,),
        ).fetchone()[0]
    return str(public_id)


def _run_recovery(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeVerificationClient,
    token: str,
    *,
    account_public_id: str,
    database_url: str,
    discord_user_id: str = "1234567890123456789",
    player_tag: str = "#2PP",
) -> int:
    monkeypatch.setattr(cli, "OfficialVerificationClient", lambda **_: client)
    monkeypatch.setattr(cli, "load_official_api_key_file", lambda _: b"k" * 32)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _prompt: token)
    code = cli.main(
        [
            "recover-discord",
            "--database-url",
            database_url,
            "--target-account-public-id",
            account_public_id,
            "--player-tag",
            player_tag,
            "--discord-user-id",
            discord_user_id,
            "--operator",
            "maintainer",
            "--reason",
            "recovery proved with the current in-game token",
            "--official-key-file",
            "/unused/key",
            "--official-proxy-url",
            "http://127.0.0.1:1",
        ]
    )
    return code


def test_recovery_attaches_discord_after_token_verification(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            public_id = _seed_account_with_verified_player(
                database,
                connection_info,
                google_subject="recover-google-subject",
                username="recoveruser",
            )
            client = FakeVerificationClient(200, b'{"status":"ok"}')
            code = _run_recovery(
                monkeypatch,
                client,
                "secret-current-token",
                account_public_id=public_id,
                database_url=connection_info,
            )
            assert code == 0
            assert client.seen_tokens == ["secret-current-token"]
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert payload["status"] == "attached"

            resolved = database.resolve_account("discord", "1234567890123456789")
            assert resolved is not None
            assert resolved.username == "recoveruser"

            with psycopg.connect(connection_info) as connection:
                audit = connection.execute(
                    """
                    SELECT provider, action, result, operator_identity
                    FROM provider_identity_audits
                    """
                ).fetchall()
                # The token must not be stored anywhere.
                hits = connection.execute(
                    """
                    SELECT count(*) FROM provider_identity_audits
                    WHERE reason LIKE '%secret-current-token%'
                       OR operator_identity LIKE '%secret-current-token%'
                    """
                ).fetchone()[0]
            assert audit == [
                ("discord", "support_recovery", "succeeded", "maintainer")
            ]
            assert hits == 0
        finally:
            database.close()


def test_recovery_refuses_an_invalid_current_token(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            public_id = _seed_account_with_verified_player(
                database,
                connection_info,
                google_subject="recover-google-subject",
                username="recoveruser",
            )
            client = FakeVerificationClient(200, b'{"status":"invalid"}')
            code = _run_recovery(
                monkeypatch,
                client,
                "wrong-token",
                account_public_id=public_id,
                database_url=connection_info,
            )
            assert code == 1
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert payload["status"] == "invalid_token"
            assert database.resolve_account("discord", "1234567890123456789") is None
        finally:
            database.close()


def test_recovery_refuses_a_discord_identity_owned_elsewhere(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            public_id = _seed_account_with_verified_player(
                database,
                connection_info,
                google_subject="recover-google-subject",
                username="recoveruser",
            )
            other = database.create_account(
                RequestBinding(
                    request_id=str(uuid4()),
                    caller="typescript-website",
                    provider="discord",
                    provider_subject="1111111111111111111",
                    account_id=None,
                    operation="account.create",
                    method="POST",
                    request_target="/v1/account",
                    identity={"username": "other"},
                ),
                username="other",
                normalized_username="other",
                display_name="Other",
            )
            assert other.status_code == 201

            client = FakeVerificationClient(200, b'{"status":"ok"}')
            code = _run_recovery(
                monkeypatch,
                client,
                "good-token",
                account_public_id=public_id,
                database_url=connection_info,
                discord_user_id="1111111111111111111",
            )
            assert code == 1
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert payload["status"] == "refused_collision"
            owner = database.resolve_account("discord", "1111111111111111111")
            assert owner is not None
            assert owner.username == "other"

            # The refused recovery attempt is audited on the target account
            # without moving or exposing either identity.
            with psycopg.connect(connection_info) as connection:
                audits = connection.execute(
                    """
                    SELECT action, result, operator_identity FROM provider_identity_audits
                    """
                ).fetchall()
            assert audits == [
                ("support_recovery", "refused_collision", "maintainer")
            ]
        finally:
            database.close()


def test_recovery_audits_a_failed_player_account_mismatch(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            public_id = _seed_account_with_verified_player(
                database,
                connection_info,
                google_subject="recover-google-subject",
                username="recoveruser",
            )
            other = database.create_account(
                RequestBinding(
                    request_id=str(uuid4()),
                    caller="typescript-website",
                    provider="google",
                    provider_subject="other-google-subject",
                    account_id=None,
                    operation="account.create",
                    method="POST",
                    request_target="/v1/account",
                    identity={"username": "otherplayer"},
                ),
                username="otherplayer",
                normalized_username="otherplayer",
                display_name="Otherplayer",
            )
            assert other.status_code == 201
            other_account = database.resolve_account("google", "other-google-subject")
            assert other_account is not None
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    INSERT INTO players (normalized_tag, active)
                    VALUES ('#8PP', false) ON CONFLICT (normalized_tag) DO NOTHING
                    """
                )
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                ).fetchone()[0]
                request_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO private_api_requests (
                        request_id, caller, provider, provider_subject, account_id,
                        operation, method, request_target, identity_json, state,
                        response_status, response_json, completed_at
                    ) VALUES (
                        %s, 'typescript-website', 'google', 'other-google-subject', %s,
                        'player_links.verify', 'POST', '/v1/players/#8PP/verifytoken',
                        '{"tag":"#8PP"}'::jsonb, 'complete', 200,
                        '{"status":"linked","tag":"#8PP"}'::jsonb, clock_timestamp()
                    )
                    """,
                    (request_id, other_account.internal_id),
                )
                connection.execute(
                    """
                    INSERT INTO verified_player_links (player_id, account_id, verification_request_id)
                    VALUES (%s, %s, %s)
                    """,
                    (player_id, other_account.internal_id, request_id),
                )

            # The token proof succeeds, but the proven player belongs to a
            # different account than the target.
            client = FakeVerificationClient(200, b'{"status":"ok"}')
            code = _run_recovery(
                monkeypatch,
                client,
                "good-token",
                account_public_id=public_id,
                database_url=connection_info,
                discord_user_id="1234567890123456789",
                player_tag="#8PP",
            )
            assert code == 1
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert payload["status"] == "player_not_verified_on_account"
            assert database.resolve_account("discord", "1234567890123456789") is None

            with psycopg.connect(connection_info) as connection:
                audits = connection.execute(
                    """
                    SELECT action, result, operator_identity FROM provider_identity_audits
                    """
                ).fetchall()
            assert audits == [("support_recovery", "failed", "maintainer")]
        finally:
            database.close()
