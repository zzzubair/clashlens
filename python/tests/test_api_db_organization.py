from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice, product
from uuid import uuid4

import psycopg
import pytest
from test_api_db_public_ops import seed_profile
from test_api_db_verification import NOW, verification_binding
from test_api_migration import migrated_production_database

from clashlens import api_accounts, api_player_lookup, api_verification, job_outcomes
from clashlens.api_db import ApiDatabase, RequestBinding
from clashlens.verification import VerificationOutcome


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
                    "players": [
                        {"tag": "#2PP", "name": None, "trophies": None, "state": "checking"},
                        {"tag": "#8PY", "name": None, "trophies": None, "state": "checking"},
                    ],
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


def test_simultaneous_group_creates_naming_one_new_player_all_succeed(
    database_url: str,
) -> None:
    # Failing at once on a busy player is for worker locks. Site requests about
    # the same player at the same moment wait for each other instead.
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info, min_size=8, max_size=8)
        try:
            owner_id = create_owner(database)

            def create(name: str):
                return api_accounts.create_group(database,
                    account_binding(
                        owner_id,
                        "groups.create",
                        "/v1/account/groups",
                        {"name": name, "tags": ["#9QQ"]},
                    ),
                    name=name,
                    normalized_name=name.lower(),
                    normalized_tags=["#9QQ"],
                )

            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(create, [f"Group {n}" for n in range(6)]))
            assert [result.status_code for result in results] == [201] * 6
            assert len(api_accounts.list_groups(database, owner_id)) == 6
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
        database = ApiDatabase(connection_info, min_size=8, max_size=8)
        worker = psycopg.connect(connection_info)
        try:
            owner_id = create_owner(database)
            job_outcomes._upsert_player(worker, "#2PP", active=True)
            job_outcomes._upsert_player(worker, "#8PY", active=False)
            worker.commit()
            verifications = [
                verification_binding(owner_id, "group-owner-subject", "#2PP")
                for _ in range(2)
            ]
            for binding in verifications:
                assert api_verification.reserve_verification(
                    database, binding, normalized_tag="#2PP"
                ).fresh
            # The worker updates one player and saves a row referencing the
            # other, which locks it the way a foreign key check does.
            job_outcomes._upsert_player(worker, "#2PP", active=True)
            worker.execute(
                "SELECT 1 FROM players WHERE normalized_tag = '#8PY' FOR KEY SHARE"
            )

            def create(name: str, tags: list[str], binding: RequestBinding | None = None):
                binding = binding or account_binding(
                    owner_id,
                    "groups.create",
                    "/v1/account/groups",
                    {"name": name, "tags": tags},
                )
                return api_accounts.create_group(database,
                    binding,
                    name=name,
                    normalized_name=name.lower(),
                    normalized_tags=tags,
                )

            def verify(binding: RequestBinding):
                return api_verification.complete_verification(database,
                    binding,
                    normalized_tag="#2PP",
                    outcome=VerificationOutcome.VERIFIED,
                    account_id=owner_id,
                    completed_at=NOW,
                )

            def lookup(tag: str):
                return api_player_lookup.submit_lookup(database,
                    account_binding(owner_id, "lookup.submit", f"/v1/players/{tag}/lookup", {"tag": tag}),
                    normalized_tag=tag,
                )

            def refresh(tag: str):
                return api_accounts.submit_refresh(database,
                    account_binding(owner_id, "refresh.submit", f"/v1/players/{tag}/refresh", {"tag": tag}),
                    normalized_tag=tag,
                    cooldown_seconds=30,
                )

            # A wait here would block forever, so give up after 10 seconds.
            executor = ThreadPoolExecutor(max_workers=9)
            try:
                created = executor.submit(create, "Main", ["#2PP", "#8PY"])
                assert created.result(timeout=10).status_code == 201

                # The worker also holds the per-tag check lock and has saved
                # new players it has not committed yet. Eight site requests
                # that need those fill every API connection, and each must
                # fail at once so a page read still answers.
                for tag in ("#9QQ", "#LQG", "#RJC"):
                    job_outcomes._upsert_player(worker, tag, active=True)
                worker.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('#2PP', 0))"
                )
                blocked = [
                    *(executor.submit(create, f"Tag {n}", ["#2PP"]) for n in range(2)),
                    *(executor.submit(create, f"New {n}", ["#9QQ"]) for n in range(2)),
                    *(executor.submit(verify, binding) for binding in verifications),
                    executor.submit(lookup, "#LQG"),
                    executor.submit(refresh, "#RJC"),
                ]
                started = time.monotonic()
                groups = executor.submit(api_accounts.list_groups, database, owner_id)
                assert [group["name"] for group in groups.result(timeout=10)] == [
                    "Main"
                ]
                assert time.monotonic() - started < 1
                for request in blocked:
                    with pytest.raises(psycopg.errors.LockNotAvailable):
                        request.result(timeout=10)
                assert time.monotonic() - started < 1
            finally:
                executor.shutdown(wait=False)

            binding = account_binding(
                owner_id,
                "groups.create",
                "/v1/account/groups",
                {"name": "Alts", "tags": ["#2PP", "#9QQ"]},
            )
            with pytest.raises(psycopg.errors.LockNotAvailable):
                create("Alts", ["#2PP", "#9QQ"], binding)
            worker.commit()
            assert create("Alts", ["#2PP", "#9QQ"], binding).status_code == 201
            assert sorted(group["name"] for group in api_accounts.list_groups(
                database, owner_id
            )) == ["Alts", "Main"]
        finally:
            worker.close()
            database.close()


def add_player(database: ApiDatabase, account_id: int, group_id: str, tag: str, **kw):
    return api_accounts.add_group_player(database,
        account_binding(
            account_id,
            "groups.add_player",
            f"/v1/account/groups/{group_id}/players",
            {"group_id": group_id, "tag": tag},
            **kw,
        ),
        group_id=group_id,
        normalized_tag=tag,
    )


def test_group_players_join_one_at_a_time_only_once_the_game_confirms_them(
    database_url: str,
) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            owner_id = create_owner(database)
            group_id = api_accounts.create_group(database,
                account_binding(
                    owner_id, "groups.create", "/v1/account/groups",
                    {"name": "Main", "tags": []},
                ),
                name="Main",
                normalized_name="main",
                normalized_tags=[],
            ).payload["group_id"]
            seed_profile(database, "#2PP", 5400)
            seed_profile(database, "#8PY", 4100)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE players SET active = false, eligibility_state = 'ineligible'
                    WHERE normalized_tag = '#8PY'
                    """
                )
                # The game answered that this tag belongs to no player.
                api_player_lookup.admit(connection, "#9PY")
                connection.execute(
                    """
                    UPDATE collector_work
                    SET status = 'failed', failure_category = 'player_not_found'
                    WHERE normalized_tag = '#9PY'
                    """
                )

            tracked = add_player(database, owner_id, group_id, "#2PP")
            again = add_player(database, owner_id, group_id, "#2PP")
            outside_legend = add_player(database, owner_id, group_id, "#8PY")
            missing = add_player(database, owner_id, group_id, "#9PY")
            unchecked = add_player(database, owner_id, group_id, "#QQQ")

            assert tracked.status_code == 200
            assert tracked.payload == {
                "group_id": group_id,
                "tag": "#2PP",
                "name": "Player #2PP",
                "trophies": 5400,
                "state": "tracking",
            }
            assert again.status_code == 409
            assert again.payload == {"error": "group_player_exists"}
            assert outside_legend.payload["state"] == "not_in_legend"
            assert missing.status_code == 422
            assert missing.payload == {"error": "player_not_found"}
            assert unchecked.status_code == 409
            assert unchecked.payload == {"error": "player_not_checked", "state": "unknown"}
            [group] = api_accounts.list_groups(database, owner_id)
            assert group["players"] == [
                {"tag": "#2PP", "name": "Player #2PP", "trophies": 5400, "state": "tracking"},
                {
                    "tag": "#8PY",
                    "name": "Player #8PY",
                    "trophies": 4100,
                    "state": "not_in_legend",
                },
            ]

            removed = api_accounts.remove_group_player(database,
                account_binding(
                    owner_id,
                    "groups.remove_player",
                    f"/v1/account/groups/{group_id}/players/%232PP",
                    {"group_id": group_id, "tag": "#2PP"},
                    method="DELETE",
                ),
                group_id=group_id,
                normalized_tag="#2PP",
            )
            assert removed.payload == {"group_id": group_id, "tag": "#2PP", "removed": True}
            assert api_accounts.list_groups(database, owner_id)[0]["tags"] == ["#8PY"]

            # Another account's group reads as missing, and nothing joins it.
            assert add_player(
                database, owner_id + 1, group_id, "#2PP", subject="other-subject"
            ).payload == {"error": "group_not_found"}
            assert api_accounts.list_groups(database, owner_id)[0]["tags"] == ["#8PY"]
        finally:
            database.close()


def test_a_full_group_refuses_the_twenty_first_player(database_url: str) -> None:
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            owner_id = create_owner(database)
            tags = [
                "#" + "".join(chars) for chars in islice(product("289LYQ", repeat=3), 20)
            ]
            group_id = api_accounts.create_group(database,
                account_binding(
                    owner_id, "groups.create", "/v1/account/groups",
                    {"name": "Clan", "tags": tags},
                ),
                name="Clan",
                normalized_name="clan",
                normalized_tags=tags,
            ).payload["group_id"]
            seed_profile(database, "#2PP", 5400)

            full = add_player(database, owner_id, group_id, "#2PP")

            assert full.status_code == 422
            assert full.payload == {"error": "group_full"}
            assert "#2PP" not in api_accounts.list_groups(database, owner_id)[0]["tags"]
        finally:
            database.close()
