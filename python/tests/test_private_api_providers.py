from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from fastapi.testclient import TestClient
from test_api_migration import ROOT, migrated_production_database
from test_private_api import signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

TS_CURRENT = bytes.fromhex("21" * 32)
NOW_SECONDS = 1_807_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, tz=UTC)


def _app(database: ApiDatabase) -> TestClient:
    app = create_app(
        database=database,
        keys={("typescript-website", "current"): TS_CURRENT},
        clock=lambda: NOW_SECONDS,
        now=lambda: NOW,
    )
    return TestClient(app)


def _create_account(client: TestClient, *, provider: str, subject: str, username: str):
    target = "/v1/account"
    response = client.post(
        target,
        content=b'{"username": "%s", "display_name": "%s"}'
        % (username.encode(), username.title().encode()),
        headers=signed_headers(
            target,
            method="POST",
            body=b'{"username": "%s", "display_name": "%s"}'
            % (username.encode(), username.title().encode()),
            provider=provider,
            subject=subject,
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_discord_identity_creates_and_resolves_an_account(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                created = _create_account(
                    client,
                    provider="discord",
                    subject="discord-subject-1001",
                    username="discorduser",
                )
                assert created["providers"] == ["discord"]

                target = "/v1/account"
                resolved = client.get(
                    target,
                    headers=signed_headers(
                        target, provider="discord", subject="discord-subject-1001"
                    ),
                )
                assert resolved.status_code == 200
                assert resolved.json()["username"] == "discorduser"
        finally:
            database.close()


def test_link_then_unlink_through_the_private_api_endpoints(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client,
                    provider="google",
                    subject="google-subject-1001",
                    username="googleuser",
                )

                link_target = "/v1/account/providers/discord"
                link_body = b'{"provider_subject": "discord-subject-2002"}'
                linked = client.post(
                    link_target,
                    content=link_body,
                    headers=signed_headers(
                        link_target,
                        method="POST",
                        body=link_body,
                        provider="google",
                        subject="google-subject-1001",
                    ),
                )
                assert linked.status_code == 200
                assert linked.json() == {"providers": ["discord", "google"]}

                unlink_target = "/v1/account/providers/discord"
                unlink_body = b'{"provider_subject": "discord-subject-2002"}'
                unlinked = client.request(
                    "DELETE",
                    unlink_target,
                    content=unlink_body,
                    headers=signed_headers(
                        unlink_target,
                        method="DELETE",
                        body=unlink_body,
                        provider="google",
                        subject="google-subject-1001",
                    ),
                )
                assert unlinked.status_code == 200
                assert unlinked.json() == {"providers": ["google"]}
        finally:
            database.close()


def test_reused_request_id_with_another_subject_conflicts(
    database_url: str,
) -> None:
    """The fresh provider subject joins the idempotency binding, so a reused
    request ID carrying another subject conflicts instead of replaying."""
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client,
                    provider="google",
                    subject="google-subject-idem",
                    username="idemuser",
                )

                link_target = "/v1/account/providers/discord"
                link_body = b'{"provider_subject": "discord-subject-idem-a"}'
                linked = client.post(
                    link_target,
                    content=link_body,
                    headers=signed_headers(
                        link_target,
                        method="POST",
                        body=link_body,
                        provider="google",
                        subject="google-subject-idem",
                        request_id="00000000-0000-4000-8000-0000000abcde",
                    ),
                )
                assert linked.status_code == 200

                replay_other_subject = client.post(
                    link_target,
                    content=b'{"provider_subject": "discord-subject-idem-b"}',
                    headers=signed_headers(
                        link_target,
                        method="POST",
                        body=b'{"provider_subject": "discord-subject-idem-b"}',
                        provider="google",
                        subject="google-subject-idem",
                        request_id="00000000-0000-4000-8000-0000000abcde",
                    ),
                )
                assert replay_other_subject.status_code == 409
                assert replay_other_subject.json() == {"error": "request_id_conflict"}
        finally:
            database.close()


def test_collision_final_provider_and_unknown_provider_fail_safely(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client,
                    provider="google",
                    subject="google-subject-a",
                    username="usera",
                )
                _create_account(
                    client,
                    provider="discord",
                    subject="discord-subject-b",
                    username="userb",
                )

                # usera cannot claim the Discord identity owned by userb.
                link_target = "/v1/account/providers/discord"
                link_body = b'{"provider_subject": "discord-subject-b"}'
                collision = client.post(
                    link_target,
                    content=link_body,
                    headers=signed_headers(
                        link_target,
                        method="POST",
                        body=link_body,
                        provider="google",
                        subject="google-subject-a",
                    ),
                )
                assert collision.status_code == 409
                assert collision.json() == {"error": "provider_identity_conflict"}

                # The final identity cannot be removed.
                unlink_target = "/v1/account/providers/google"
                unlink_body = b'{"provider_subject": "google-subject-a"}'
                final = client.request(
                    "DELETE",
                    unlink_target,
                    content=unlink_body,
                    headers=signed_headers(
                        unlink_target,
                        method="DELETE",
                        body=unlink_body,
                        provider="google",
                        subject="google-subject-a",
                    ),
                )
                assert final.status_code == 409
                assert final.json() == {"error": "final_provider"}

                # Only Google and Discord exist.
                unknown_target = "/v1/account/providers/email"
                unknown_body = b'{"provider_subject": "whatever"}'
                unknown = client.post(
                    unknown_target,
                    content=unknown_body,
                    headers=signed_headers(
                        unknown_target,
                        method="POST",
                        body=unknown_body,
                        provider="google",
                        subject="google-subject-a",
                    ),
                )
                assert unknown.status_code == 404
                assert unknown.json() == {"error": "provider_not_found"}
        finally:
            database.close()


def _session_call(client: TestClient, action: str, session: str, *, provider: str = "google"):
    target = f"/v1/account/session/{action}"
    body = b'{"session": "%s"}' % session.encode()
    return client.post(
        target,
        content=body,
        headers=signed_headers(
            target,
            method="POST",
            body=body,
            provider=provider,
            subject="google-subject-1001" if provider else "",
        ),
    )


def test_logout_ends_only_that_login_and_old_logouts_are_dropped(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as connection:
            connection.execute(
                (ROOT / "deploy/migrations/0054_login_session_revocations.sql").read_text()
            )
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                ended, other = "a" * 43, "b" * 43
                assert _session_call(client, "check", ended).json() == {"revoked": False}
                for _ in range(2):  # Logging out twice is harmless.
                    revoked = _session_call(client, "revoke", ended)
                    assert revoked.status_code == 200
                    assert revoked.json() == {"revoked": True}
                assert _session_call(client, "check", ended).json() == {"revoked": True}
                assert _session_call(client, "check", other).json() == {"revoked": False}

                # A login without a provider identity, or a malformed one, is refused.
                assert _session_call(client, "check", ended, provider="").status_code == 403
                assert _session_call(client, "revoke", other, provider="").status_code == 403
                assert _session_call(client, "check", "short").status_code == 422

                # A login cookie lasts 24 hours, so a logout over 25 hours old is
                # deleted the next time someone logs out.
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE login_session_revocations"
                        " SET revoked_at = now() - interval '26 hours'"
                    )
                assert _session_call(client, "revoke", other).status_code == 200
                assert _session_call(client, "check", ended).json() == {"revoked": False}
                assert _session_call(client, "check", other).json() == {"revoked": True}
        finally:
            database.close()
