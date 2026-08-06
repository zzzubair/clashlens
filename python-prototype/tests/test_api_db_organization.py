from __future__ import annotations

from uuid import uuid4

from clashlens_prototype.api_db import ApiDatabase, RequestBinding
from test_api_migration import migrated_production_database


def account_binding(
    account_id: int,
    operation: str,
    target: str,
    identity: dict[str, object],
    *,
    method: str = "POST",
    subject: str = "group-owner-subject",
) -> RequestBinding:
    return RequestBinding(
        request_id=str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=subject,
        account_id=account_id,
        operation=operation,
        method=method,
        request_target=target,
        identity=identity,
    )


def create_owner(database: ApiDatabase) -> int:
    created = database.create_account(
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="google",
            provider_subject="group-owner-subject",
            account_id=None,
            operation="account.create",
            method="POST",
            request_target="/v1/account",
            identity={"username": "groupowner"},
        ),
        username="groupowner",
        normalized_username="groupowner",
        display_name="Group Owner",
    )
    assert created.status_code == 201
    account = database.resolve_account("google", "group-owner-subject")
    assert account is not None
    return account.internal_id


def test_saved_tags_groups_public_user_and_multi_account_stay_separate(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            saved = database.add_saved_player(
                account_binding(
                    account_id,
                    "saved_tags.add",
                    "/v1/account/saved-tags",
                    {"tag": "#2PP"},
                ),
                normalized_tag="#2PP",
            )
            group = database.create_group(
                account_binding(
                    account_id,
                    "groups.create",
                    "/v1/account/groups",
                    {"name": "My Accounts", "tags": ["#2PP", "#8PY"]},
                ),
                name="My Accounts",
                normalized_name="my accounts",
                normalized_tags=["#2PP", "#8PY"],
            )

            assert saved.payload == {"tag": "#2PP", "saved": True}
            assert database.list_saved_players(account_id) == [
                {"tag": "#2PP", "name": None}
            ]
            assert group.status_code == 201
            group_id = group.payload["group_id"]
            assert isinstance(group_id, str)
            assert database.list_groups(account_id) == [
                {
                    "group_id": group_id,
                    "name": "My Accounts",
                    "tags": ["#2PP", "#8PY"],
                }
            ]
            assert database.get_public_user("groupowner") == {
                "username": "groupowner",
                "display_name": "Group Owner",
                "verified_players": [],
            }
            assert database.get_multi_account_summary(account_id) == {
                "username": "groupowner",
                "display_name": "Group Owner",
                "verified_players": [],
            }

            request_id = str(uuid4())
            with database.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO private_api_requests (
                        request_id, caller, provider, provider_subject, account_id,
                        operation, method, request_target, identity_json, state,
                        response_status, response_json, completed_at
                    ) VALUES (
                        %s, 'typescript-website', 'google', 'group-owner-subject', %s,
                        'player_links.verify', 'POST', '/v1/players/#2PP/verifytoken',
                        '{"tag":"#2PP"}'::jsonb, 'complete', 200,
                        '{"status":"linked","tag":"#2PP"}'::jsonb, clock_timestamp()
                    )
                    """,
                    (request_id, account_id),
                )
                connection.execute(
                    """
                    INSERT INTO verified_player_links (
                        player_id, account_id, verification_request_id
                    ) VALUES (%s, %s, %s)
                    """,
                    (player_id, account_id, request_id),
                )
                connection.commit()

            public_user = database.get_public_user("groupowner")
            summary = database.get_multi_account_summary(account_id)
            assert public_user["verified_players"] == [{"tag": "#2PP", "name": None}]
            assert summary["verified_players"] == [{"tag": "#2PP", "name": None}]
            assert "id" not in str(public_user).lower()
            assert "id" not in str(summary).lower()
        finally:
            database.close()


def test_group_update_and_delete_require_the_owning_account(
    database_url: str,
) -> None:
    with migrated_production_database(database_url) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            owner_id = create_owner(database)
            other = database.create_account(
                RequestBinding(
                    request_id=str(uuid4()),
                    caller="typescript-website",
                    provider="google",
                    provider_subject="other-owner-subject",
                    account_id=None,
                    operation="account.create",
                    method="POST",
                    request_target="/v1/account",
                    identity={"username": "otherowner"},
                ),
                username="otherowner",
                normalized_username="otherowner",
                display_name="Other Owner",
            )
            assert other.status_code == 201
            other_account = database.resolve_account("google", "other-owner-subject")
            assert other_account is not None
            created = database.create_group(
                account_binding(
                    owner_id,
                    "groups.create",
                    "/v1/account/groups",
                    {"name": "Main", "tags": ["#2PP"]},
                ),
                name="Main",
                normalized_name="main",
                normalized_tags=["#2PP"],
            )
            group_id = created.payload["group_id"]

            denied = database.update_group(
                account_binding(
                    other_account.internal_id,
                    "groups.update",
                    f"/v1/account/groups/{group_id}",
                    {"group_id": group_id, "name": "Changed", "tags": []},
                    method="PATCH",
                    subject="other-owner-subject",
                ),
                group_id=group_id,
                name="Changed",
                normalized_name="changed",
                normalized_tags=[],
            )
            updated = database.update_group(
                account_binding(
                    owner_id,
                    "groups.update",
                    f"/v1/account/groups/{group_id}",
                    {"group_id": group_id, "name": "Changed", "tags": ["#8PY"]},
                    method="PATCH",
                ),
                group_id=group_id,
                name="Changed",
                normalized_name="changed",
                normalized_tags=["#8PY"],
            )
            deleted = database.delete_group(
                account_binding(
                    owner_id,
                    "groups.delete",
                    f"/v1/account/groups/{group_id}",
                    {"group_id": group_id},
                    method="DELETE",
                ),
                group_id=group_id,
            )

            assert denied.status_code == 404
            assert denied.payload == {"error": "group_not_found"}
            assert updated.payload == {
                "group_id": group_id,
                "name": "Changed",
                "tags": ["#8PY"],
            }
            assert deleted.payload == {"deleted": True, "group_id": group_id}
            assert database.list_groups(owner_id) == []
        finally:
            database.close()
