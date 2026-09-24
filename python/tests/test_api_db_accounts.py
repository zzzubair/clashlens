from __future__ import annotations

from uuid import uuid4

from test_api_db_organization import account_binding, create_owner
from test_api_db_public_ops import NOW, seed_profile
from test_api_db_verification import verification_binding
from test_api_migration import migrated_production_database

from clashlens import api_accounts, api_verification
from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.verification import VerificationOutcome


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
            first = api_accounts.create_account(database,
                request,
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            replay = api_accounts.create_account(database,
                request,
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            account = api_accounts.resolve_account(database, "google", "google-subject-one")

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
            first = api_accounts.create_account(database,
                binding(request_id=request_id),
                username="PlayerOne",
                normalized_username="playerone",
                display_name="Player One",
            )
            conflict = api_accounts.create_account(database,
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
            first = api_accounts.create_account(database,
                binding(subject="google-subject-one"),
                username="PlayerOne",
                normalized_username="playerone",
                display_name="One",
            )
            same_username = api_accounts.create_account(database,
                binding(subject="google-subject-two"),
                username="PLAYERONE",
                normalized_username="playerone",
                display_name="Two",
            )
            same_provider = api_accounts.create_account(database,
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


def test_username_is_fixed_but_display_name_can_change(database_url: str) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            for username, expected_status in [("changedowner", 409), ("groupowner", 200)]:
                result = api_accounts.update_account(
                    database,
                    account_binding(account_id, "account.update", "/v1/account",
                                    {"username": username, "display_name": "New display name"},
                                    method="PATCH"),
                    username=username, normalized_username=username,
                    display_name="New display name", preferences={"timezone": "UTC"},
                )
                assert result.status_code == expected_status
                account = api_accounts.get_account(database, account_id)
                assert account["username"] == "groupowner"
                if expected_status == 409:
                    assert result.payload == {"error": "username_locked"}
                    assert account["display_name"] == "Group Owner"
                    assert account["preferences"] == {}
                else:
                    assert account["display_name"] == "New display name"
                    assert account["preferences"] == {"timezone": "UTC"}
            assert api_accounts.get_public_user(database, "changedowner") is None
            assert api_accounts.get_public_user(database, "groupowner")["display_name"] == "New display name"
        finally:
            database.close()


def test_public_search_finds_linked_players_without_exposing_private_lists(database_url: str) -> None:
    with migrated_production_database(database_url, include_compact_collector=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            owner_id = create_owner(database)
            for tag in ("#2PP", "#8PY", "#P0LQ"):
                seed_profile(database, tag, 6000)
            for tag in ("#2PP", "#8PY"):
                request = verification_binding(owner_id, "group-owner-subject", tag)
                api_verification.reserve_verification(database, request, normalized_tag=tag)
                api_verification.complete_verification(
                    database, request, normalized_tag=tag,
                    outcome=VerificationOutcome.VERIFIED,
                    account_id=owner_id, completed_at=NOW,
                )
            api_accounts.add_saved_player(
                database,
                account_binding(owner_id, "saved_tags.add", "/v1/account/saved-tags", {"tag": "#P0LQ"}),
                normalized_tag="#P0LQ",
            )
            expected = [{"username": "groupowner", "display_name": "Group Owner", "linked_player_count": 2}]
            for query in ("GROUPOWNER", "@GroupOwner", "Group Owner", "Player #2PP", "#8PY", "Player"):
                assert api_accounts.search_public_users(database, query) == expected
            for query in ("#P0LQ", "Player #P0LQ", "group-owner-subject", "%", "_", "@"):
                assert api_accounts.search_public_users(database, query) == []
            # A validated player identity remains useful when its Legend stats
            # cannot be published, such as a linked account in another league.
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE player_profile_versions SET source_contract_state = 'conflict' WHERE normalized_tag = '#2PP'"
                )
                connection.execute(
                    "UPDATE players SET current_profile_version_id = NULL WHERE normalized_tag = '#2PP'"
                )
            assert api_accounts.search_public_users(database, "Player #2PP") == expected
            public = api_accounts.get_public_user(database, "groupowner")
            assert public["verified_players"] == [
                {"tag": "#2PP", "name": "Player #2PP"},
                {"tag": "#8PY", "name": "Player #8PY"},
            ]
            # A newer parsed identity wins even when it is not the current
            # publishable profile. The old name must stop matching search.
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    INSERT INTO player_profile_versions (
                        player_id, observation_id, normalized_tag,
                        endpoint_version, schema_version, parser_version,
                        observed_at, source_http_status, name, trophies,
                        league_tier_id, league_tier_name, eligibility_state,
                        profile_json, source_contract_state
                    )
                    SELECT player_id, observation_id, normalized_tag,
                           endpoint_version, schema_version, 'renamed-profile-test',
                           observed_at + interval '1 minute', source_http_status,
                           'Renamed_#2PP', trophies, league_tier_id,
                           league_tier_name, eligibility_state, profile_json,
                           source_contract_state
                    FROM player_profile_versions
                    WHERE normalized_tag = '#2PP'
                    ORDER BY observed_at DESC, id DESC LIMIT 1
                    """
                )
            assert (
                api_accounts.search_public_users(database, "Renamed_#2PP") == expected
            )
            assert api_accounts.search_public_users(database, "Player #2PP") == []
            assert api_accounts.search_public_users(database, "Renamed%#2PP") == []
            assert api_accounts.get_public_user(database, "groupowner")[
                "verified_players"
            ][0] == {
                "tag": "#2PP",
                "name": "Renamed_#2PP",
            }
        finally:
            database.close()
