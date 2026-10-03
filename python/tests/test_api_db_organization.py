from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from test_api_migration import migrated_production_database

from clashlens import api_accounts, job_outcomes
from clashlens.api_db import ApiDatabase, RequestBinding


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
    created = api_accounts.create_account(database,
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
    account = api_accounts.resolve_account(database, "google", "group-owner-subject")
    assert account is not None
    return account.internal_id


def test_saved_tags_groups_public_user_and_multi_account_stay_separate(
    database_url: str,
) -> None:
    # Saving a group can start a player check, which needs the collector tables.
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            account_id = create_owner(database)
            saved = api_accounts.add_saved_player(database,
                account_binding(
                    account_id,
                    "saved_tags.add",
                    "/v1/account/saved-tags",
                    {"tag": "#2PP"},
                ),
                normalized_tag="#2PP",
            )
            group = api_accounts.create_group(database,
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
            assert api_accounts.list_saved_players(database, account_id) == [
                {"tag": "#2PP", "name": None}
            ]
            assert group.status_code == 201
            group_id = group.payload["group_id"]
            assert isinstance(group_id, str)
            assert api_accounts.list_groups(database, account_id) == [
                {
                    "group_id": group_id,
                    "name": "My Accounts",
                    "tags": ["#2PP", "#8PY"],
                }
            ]
            assert api_accounts.get_public_user(database, "groupowner") == {
                "username": "groupowner",
                "display_name": "Group Owner",
                "verified_players": [],
            }
            assert api_accounts.get_multi_account_summary(database, account_id) == {
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

            public_user = api_accounts.get_public_user(database, "groupowner")
            summary = api_accounts.get_multi_account_summary(database, account_id)
            assert public_user["verified_players"] == [{"tag": "#2PP", "name": None}]
            assert summary["verified_players"] == [{"tag": "#2PP", "name": None}]
            assert "id" not in str(public_user).lower()
            assert "id" not in str(summary).lower()
        finally:
            database.close()


def test_group_update_and_delete_require_the_owning_account(
    database_url: str,
) -> None:
    # Saving a group can start a player check, which needs the collector tables.
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            owner_id = create_owner(database)
            other = api_accounts.create_account(database,
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
            other_account = api_accounts.resolve_account(database, "google", "other-owner-subject")
            assert other_account is not None
            created = api_accounts.create_group(database,
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

            denied = api_accounts.update_group(database,
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
            updated = api_accounts.update_group(database,
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
            deleted = api_accounts.delete_group(database,
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
            assert api_accounts.list_groups(database, owner_id) == []
        finally:
            database.close()


def test_group_creation_does_not_queue_behind_a_worker_transaction(
    database_url: str,
) -> None:
    # On 2026-10-03 a worker rebuild kept one transaction open for minutes.
    # Every Create group click waited on its player row locks until all API
    # connections were stuck and every page timed out.
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        worker = psycopg.connect(connection_info)
        try:
            owner_id = create_owner(database)
            job_outcomes._upsert_player(worker, "#2PP", active=True)
            job_outcomes._upsert_player(worker, "#8PY", active=False)
            worker.commit()
            # The worker updates one player and saves a row referencing the
            # other, which locks it the way a foreign key check does.
            job_outcomes._upsert_player(worker, "#2PP", active=True)
            worker.execute(
                "SELECT 1 FROM players WHERE normalized_tag = '#8PY' FOR KEY SHARE"
            )

            def create(name: str, binding: RequestBinding | None = None):
                binding = binding or account_binding(
                    owner_id,
                    "groups.create",
                    "/v1/account/groups",
                    {"name": name, "tags": ["#2PP", "#8PY"]},
                )
                return binding, api_accounts.create_group(database,
                    binding,
                    name=name,
                    normalized_name=name.lower(),
                    normalized_tags=["#2PP", "#8PY"],
                )

            # A wait here would block forever, so give up after 10 seconds.
            executor = ThreadPoolExecutor(max_workers=1)
            try:
                _, created = executor.submit(create, "Main").result(timeout=10)
            finally:
                executor.shutdown(wait=False)
            assert created.status_code == 201

            # A lock the API really must wait for, such as the per-tag check
            # lock, fails the request quickly instead of holding a connection.
            worker.execute("SELECT pg_advisory_xact_lock(hashtextextended('#2PP', 0))")
            binding = account_binding(
                owner_id,
                "groups.create",
                "/v1/account/groups",
                {"name": "Alts", "tags": ["#2PP", "#8PY"]},
            )
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable):
                create("Alts", binding)
            assert time.monotonic() - started < 4
            assert [group["name"] for group in api_accounts.list_groups(
                database, owner_id
            )] == ["Main"]

            worker.commit()
            _, retried = create("Alts", binding)
            assert retried.status_code == 201
            assert sorted(group["name"] for group in api_accounts.list_groups(
                database, owner_id
            )) == ["Alts", "Main"]
        finally:
            worker.close()
            database.close()
