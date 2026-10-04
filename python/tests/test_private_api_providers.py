from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_api_migration import ROOT, migrated_production_database
from test_private_api import signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

TS_CURRENT = bytes.fromhex("21" * 32)
NOW_SECONDS = 1_807_000_000
NOW = datetime.fromtimestamp(NOW_SECONDS, tz=UTC)


def migrate_login_tables(connection_info: str) -> None:
    """Add the logout and removed-connection tables that login checks and
    unlinking need."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        for name in (
            "0054_login_session_revocations.sql",
            "0069_provider_identity_removals.sql",
        ):
            connection.execute((ROOT / "deploy/migrations" / name).read_text())


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
        migrate_login_tables(connection_info)
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


SUBJECTS = {"google": "google-subject-1001", "discord": "discord-subject-2002"}


def _session_call(
    client: TestClient,
    action: str,
    session: str,
    *,
    provider: str = "google",
    issued_at_ms: int | None = None,
):
    """Check or log out one login. A check sends the login cookie's issue
    time in milliseconds, a minute ago unless given."""
    target = f"/v1/account/session/{action}"
    fields = {"session": session}
    if action == "check":
        fields["issued_at_ms"] = (
            int(time.time() * 1000) - 60_000 if issued_at_ms is None else issued_at_ms
        )
    body = json.dumps(fields).encode()
    return client.post(
        target,
        content=body,
        headers=signed_headers(
            target,
            method="POST",
            body=body,
            provider=provider,
            subject=SUBJECTS.get(provider, ""),
        ),
    )


def _change_provider(
    client: TestClient, method: str, provider: str, *, signed_in_with: str, session=None
):
    target = f"/v1/account/providers/{provider}"
    fields = {"provider_subject": SUBJECTS[provider]}
    if session is not None:
        fields["session"] = session
    body = json.dumps(fields).encode()
    return client.request(
        method,
        target,
        content=body,
        headers=signed_headers(
            target,
            method=method,
            body=body,
            provider=signed_in_with,
            subject=SUBJECTS[signed_in_with],
        ),
    )


def _removed_at(database: ApiDatabase, provider: str) -> int | None:
    with database.pool.connection() as connection:
        row = connection.execute(
            "SELECT floor(extract(epoch FROM removed_at) * 1000)::bigint"
            " FROM provider_identity_removals WHERE provider = %s",
            (provider,),
        ).fetchone()
    return None if row is None else int(row[0])


def test_logout_ends_only_that_login_and_old_logouts_are_dropped(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        migrate_login_tables(connection_info)
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


def test_unlinking_the_login_provider_ends_that_login_in_the_same_transaction(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        migrate_login_tables(connection_info)
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client,
                    provider="google",
                    subject="google-subject-1001",
                    username="googleuser",
                )
                session = "c" * 43
                unlink_target = "/v1/account/providers/google"
                unlink_body = (
                    b'{"provider_subject": "google-subject-1001", "session": "%s"}'
                    % session.encode()
                )

                def unlink():
                    return client.request(
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

                # A refused unlink changes nothing, so the login stays valid.
                assert unlink().json() == {"error": "final_provider"}
                assert _session_call(client, "check", session).json() == {"revoked": False}
                assert _removed_at(database, "google") is None

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

                removed = unlink()
                assert removed.status_code == 200
                assert removed.json() == {"providers": ["discord"]}
                assert _session_call(client, "check", session).json() == {"revoked": True}
        finally:
            database.close()


@pytest.mark.parametrize("removed", ["google", "discord"])
@pytest.mark.parametrize("signed_in_with", ["removed", "other"])
def test_removing_a_connection_ends_its_logins_on_every_browser(
    database_url: str, removed: str, signed_in_with: str
) -> None:
    other = "discord" if removed == "google" else "google"
    remover = removed if signed_in_with == "removed" else other
    with migrated_production_database(database_url) as connection_info:
        migrate_login_tables(connection_info)
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client, provider=other, subject=SUBJECTS[other], username="twologins"
                )
                assert (
                    _change_provider(client, "POST", removed, signed_in_with=other).status_code
                    == 200
                )
                # Two browsers signed in through the connection, and one through
                # the other connection.
                browser_a, browser_b, other_login = "a" * 43, "b" * 43, "o" * 43
                assert _session_call(client, "check", browser_b, provider=removed).json() == {
                    "revoked": False
                }

                ended = browser_a if remover == removed else None
                removal = _change_provider(
                    client, "DELETE", removed, signed_in_with=remover, session=ended
                )
                assert removal.status_code == 200, removal.text
                for browser in (browser_a, browser_b):
                    assert _session_call(client, "check", browser, provider=removed).json() == {
                        "revoked": True
                    }
                assert _session_call(client, "check", other_login, provider=other).json() == {
                    "revoked": False
                }

                # A login made after the removal works, and linking the
                # connection again never revives the older logins.
                removed_at = _removed_at(database, removed)
                assert removed_at is not None
                fresh = {"provider": removed, "issued_at_ms": removed_at + 1}
                assert _session_call(client, "check", "f" * 43, **fresh).json() == {
                    "revoked": False
                }
                assert (
                    _change_provider(client, "POST", removed, signed_in_with=other).status_code
                    == 200
                )
                assert _session_call(client, "check", browser_b, provider=removed).json() == {
                    "revoked": True
                }
                assert _session_call(client, "check", "f" * 43, **fresh).json() == {
                    "revoked": False
                }
                # Without the issue time, the check refuses to answer.
                target = "/v1/account/session/check"
                body = json.dumps({"session": browser_b}).encode()
                missing = client.post(
                    target,
                    content=body,
                    headers=signed_headers(
                        target,
                        method="POST",
                        body=body,
                        provider=removed,
                        subject=SUBJECTS[removed],
                    ),
                )
                assert missing.status_code == 422
        finally:
            database.close()


def test_removals_over_25_hours_old_are_deleted_and_end_nothing(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        migrate_login_tables(connection_info)
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client, provider="google", subject=SUBJECTS["google"], username="aged"
                )
                link = _change_provider(client, "POST", "discord", signed_in_with="google")
                assert link.status_code == 200
                unlink = _change_provider(client, "DELETE", "discord", signed_in_with="google")
                assert unlink.status_code == 200
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE provider_identity_removals"
                        " SET removed_at = now() - interval '26 hours'"
                    )
                old_login = {
                    "provider": "discord",
                    "issued_at_ms": int(time.time() * 1000) - 27 * 3_600_000,
                }
                assert _session_call(client, "check", "d" * 43, **old_login).json() == {
                    "revoked": True
                }

                # The next removal deletes the aged row, which then ends nothing.
                link = _change_provider(client, "POST", "discord", signed_in_with="google")
                assert link.status_code == 200
                unlink = _change_provider(client, "DELETE", "google", signed_in_with="discord")
                assert unlink.status_code == 200
                assert _removed_at(database, "discord") is None
                assert _removed_at(database, "google") is not None
                assert _session_call(client, "check", "d" * 43, **old_login).json() == {
                    "revoked": False
                }
        finally:
            database.close()


@pytest.mark.parametrize("removed", ["google", "discord"])
def test_a_login_in_the_same_second_as_a_removal_is_judged_by_millisecond(
    database_url: str, removed: str
) -> None:
    other = "discord" if removed == "google" else "google"
    with migrated_production_database(database_url) as connection_info:
        migrate_login_tables(connection_info)
        database = ApiDatabase(connection_info)
        try:
            with _app(database) as client:
                _create_account(
                    client, provider=other, subject=SUBJECTS[other], username="samesecond"
                )
                link = _change_provider(client, "POST", removed, signed_in_with=other)
                assert link.status_code == 200
                unlink = _change_provider(client, "DELETE", removed, signed_in_with=other)
                assert unlink.status_code == 200
                # Pin the removal to 200 milliseconds into a recent second.
                second = int(time.time()) - 10
                with database.pool.connection() as connection:
                    connection.execute(
                        "UPDATE provider_identity_removals"
                        " SET removed_at = to_timestamp(%s) + interval '200 milliseconds'",
                        (second,),
                    )
                assert _removed_at(database, removed) == second * 1000 + 200

                def check(issued_at_ms: int) -> bool:
                    response = _session_call(
                        client, "check", "s" * 43, provider=removed, issued_at_ms=issued_at_ms
                    )
                    assert response.status_code == 200, response.text
                    return response.json()["revoked"]

                assert check(second * 1000 + 800) is False
                assert check(second * 1000 + 201) is False
                assert check(second * 1000 + 200) is True
                assert check(second * 1000 + 100) is True
                assert check(second * 1000 - 1) is True
        finally:
            database.close()
