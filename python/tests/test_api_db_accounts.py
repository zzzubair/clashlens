from __future__ import annotations

from uuid import uuid4

from clashlens.api_db import ApiDatabase, RequestBinding
from test_api_migration import migrated_production_database


def binding(
    *,
    request_id: str | None = None,
    subject: str = "google-subject-one",
    operation: str = "account.create",
    identity: dict[str, str] | None = None,
) -> RequestBinding:
    return RequestBinding(
        request_id=request_id or str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=subject,
        account_id=None,
        operation=operation,
        method="POST",
        request_target="/v1/account",
        identity=identity or {"username": "playerone"},
    )


def test_account_creation_resolves_google_identity_and_replays_once(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        request = binding(request_id="00000000-0000-4000-8000-000000000101")
        try:
            first = database.create_account(
                request,
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            replay = database.create_account(
                request,
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            account = database.resolve_account("google", "google-subject-one")

            assert first.status_code == 201
            assert first.payload == {
                "username": "playerone",
                "display_name": "Player One",
                "preferences": {},
                "providers": ["google"],
            }
            assert replay.status_code == 201
            assert replay.payload == first.payload
            assert replay.replayed is True
            assert account is not None
            assert account.username == "playerone"
            assert database.scalar("SELECT count(*) FROM clash_lens_accounts") == 1
            assert database.scalar("SELECT count(*) FROM private_api_requests") == 1
        finally:
            database.close()


def test_request_id_reuse_with_changed_non_secret_binding_is_a_conflict(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        request_id = "00000000-0000-4000-8000-000000000102"
        try:
            first = database.create_account(
                binding(request_id=request_id),
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            conflict = database.create_account(
                binding(
                    request_id=request_id,
                    identity={"username": "playertwo"},
                ),
                username="PlayerTwo",
                normalized_username="playertwo",
                display_name="Player Two",
            )

            assert first.status_code == 201
            assert conflict.status_code == 409
            assert conflict.payload == {"error": "request_id_conflict"}
            assert database.scalar("SELECT count(*) FROM clash_lens_accounts") == 1
        finally:
            database.close()


def test_username_and_google_provider_uniqueness_fail_safely(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            first = database.create_account(
                binding(subject="google-subject-one"),
                username="PlayerOne",
                normalized_username="playerone",
                display_name="One",
            )
            same_username = database.create_account(
                binding(subject="google-subject-two"),
                username="PLAYERONE",
                normalized_username="playerone",
                display_name="Two",
            )
            same_provider = database.create_account(
                binding(subject="google-subject-one"),
                username="PlayerTwo",
                normalized_username="playertwo",
                display_name="Two",
            )

            assert first.status_code == 201
            assert same_username.payload == {"error": "username_unavailable"}
            assert same_provider.payload == {"error": "provider_identity_conflict"}
            assert database.scalar("SELECT count(*) FROM clash_lens_accounts") == 1
        finally:
            database.close()
