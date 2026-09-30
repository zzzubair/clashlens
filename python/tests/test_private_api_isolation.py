from __future__ import annotations

from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_api_migration import migrated_production_database
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, json_body, signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase


@pytest.fixture(params=[("google", "google"), ("google", "discord")])
def accounts(database_url, request):
    """Two independently authenticated subjects, backed by the real schema/API."""
    with migrated_production_database(
        database_url, include_compact_collector=True
    ) as connection_info:
        database = ApiDatabase(connection_info)
        app = create_app(
            database=database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
        )
        try:
            with TestClient(app) as client:
                owners = []
                for index, provider in enumerate(request.param):
                    owner = {
                        "provider": provider,
                        "subject": f"isolation-subject-{index}",
                        "username": f"privateowner{index}",
                        "tag": ("#2PP", "#8PY")[index],
                        "name": f"Secret group {index}",
                    }
                    created = call(
                        client,
                        owner,
                        "POST",
                        "/v1/account",
                        {
                            "username": owner["username"],
                            "display_name": owner["username"],
                        },
                    )
                    assert created.status_code == 201
                    assert (
                        call(client, owner, "GET", "/v1/account").json()["username"]
                        == owner["username"]
                    )
                    assert (
                        call(
                            client,
                            owner,
                            "POST",
                            "/v1/account/saved-tags",
                            {"tag": owner["tag"]},
                        ).status_code
                        == 200
                    )
                    owner["request_id"] = str(uuid4())
                    group = call(
                        client,
                        owner,
                        "POST",
                        "/v1/account/groups",
                        {
                            "name": owner["name"],
                            "tags": [owner["tag"]],
                        },
                        request_id=owner["request_id"],
                    )
                    assert group.status_code == 201
                    owner["group"] = group.json()
                    owners.append(owner)
                yield client, owners
        finally:
            database.close()


def call(client, owner, method, target, value=None, *, request_id=None, headers=None):
    body = b"" if value is None else json_body(value)
    proof = signed_headers(
        target,
        method=method,
        body=body,
        provider=owner["provider"],
        subject=owner["subject"],
        request_id=request_id,
    )
    return client.request(
        method, target, content=body, headers={**proof, **(headers or {})}
    )


def assert_private_state(client, owner):
    assert call(client, owner, "GET", "/v1/account/saved-tags").json() == {
        "players": [{"tag": owner["tag"], "name": None}],
    }
    assert call(client, owner, "GET", "/v1/account/groups").json() == {
        "groups": [owner["group"]],
    }


def test_direct_reads_ignore_guessed_owner_and_do_not_publish_membership(accounts):
    client, owners = accounts
    for owner, other in (owners, owners[::-1]):
        assert_private_state(client, owner)
        query = f"?account_id=1&username={other['username']}&group_id={other['group']['group_id']}"
        for target in (
            "/v1/account",
            "/v1/account/saved-tags",
            "/v1/account/groups",
            "/v1/account/summary",
        ):
            expected = call(client, owner, "GET", target)
            forged = call(
                client,
                owner,
                "GET",
                target + query,
                headers={
                    "X-Account-Id": "1",
                    "X-User-Id": other["username"],
                },
            )
            assert expected.status_code == forged.status_code == 200
            assert forged.json() == expected.json()
        public = call(client, owner, "GET", f"/v1/users/{other['username']}")
        assert public.json() == {
            "username": other["username"],
            "display_name": other["username"],
            "verified_players": [],
        }
        assert call(client, owner, "GET", "/v1/account/summary").json() == {
            "username": owner["username"],
            "display_name": owner["username"],
            "verified_players": [],
        }
        # Searching a public username must not disclose private counts or labels.
        search = call(client, owner, "GET", f"/v1/players/search?q={other['username']}")
        assert search.json()["users"] == [
            {
                "username": other["username"],
                "display_name": other["username"],
                "linked_player_count": 0,
            }
        ]
        for query in (other["tag"], other["name"]):
            search = call(client, owner, "GET", f"/v1/players/search?q={quote(query)}")
            assert search.status_code == 200
            assert search.json()["users"] == []


def test_guessed_group_ids_and_replayed_requests_cannot_read_or_change_other_account(
    accounts,
):
    client, owners = accounts
    for owner, other in (owners, owners[::-1]):
        unknown_id = str(uuid4())
        for method in ("GET", "PATCH", "DELETE"):
            body = (
                {"name": "Stolen", "tags": [owner["tag"]]}
                if method == "PATCH"
                else None
            )
            responses = [
                call(client, owner, method, f"/v1/account/groups/{group_id}", body)
                for group_id in (other["group"]["group_id"], unknown_id)
            ]
            assert (
                responses[0].status_code
                == responses[1].status_code
                == (405 if method == "GET" else 404)
            )
            assert responses[0].json() == responses[1].json()
        replay = call(
            client,
            owner,
            "POST",
            "/v1/account/groups",
            {
                "name": other["name"],
                "tags": [other["tag"]],
            },
            request_id=other["request_id"],
        )
        assert replay.status_code == 409
        assert replay.json() == {"error": "request_id_conflict"}
        assert_private_state(client, other)

        # A name is unique only within one account, so conflicts cannot reveal
        # the names that another account has used.
        own_copy = call(
            client,
            owner,
            "POST",
            "/v1/account/groups",
            {
                "name": other["name"],
                "tags": [owner["tag"]],
            },
        )
        assert own_copy.status_code == 201
        own_id = own_copy.json()["group_id"]
        assert (
            call(
                client,
                owner,
                "PATCH",
                f"/v1/account/groups/{own_id}",
                {
                    "name": "Renamed",
                    "tags": [],
                },
            ).status_code
            == 200
        )
        assert (
            call(client, owner, "DELETE", f"/v1/account/groups/{own_id}").status_code
            == 200
        )
        assert_private_state(client, owner)
        assert_private_state(client, other)


def test_saved_tag_mutations_cannot_reveal_or_change_other_account(accounts):
    client, owners = accounts
    for owner, other in (owners, owners[::-1]):
        # Removing a tag saved only by the other account has the same result as
        # removing a tag saved by nobody. Both leave the other account intact.
        for tag in (other["tag"], "#9PY"):
            removed = call(
                client, owner, "DELETE", f"/v1/account/saved-tags/{quote(tag)}"
            )
            assert removed.status_code == 200
            assert removed.json() == {"tag": tag, "saved": False}
            added = call(client, owner, "POST", "/v1/account/saved-tags", {"tag": tag})
            assert added.status_code == 200
            assert added.json() == {"tag": tag, "saved": True}
            assert (
                call(
                    client, owner, "DELETE", f"/v1/account/saved-tags/{quote(tag)}"
                ).status_code
                == 200
            )
        for target, body in (
            ("/v1/account/saved-tags", {"tag": other["tag"]}),
            ("/v1/account/groups", {"name": "Injected", "tags": [other["tag"]]}),
        ):
            injected = call(client, owner, "POST", target, {**body, "account_id": 1})
            assert injected.status_code == 422
        assert_private_state(client, owner)
        assert_private_state(client, other)


def test_private_endpoints_require_signed_identity_and_exports_stay_disabled(accounts):
    client, owners = accounts
    for owner, other in (owners, owners[::-1]):
        group_path = f"/v1/account/groups/{other['group']['group_id']}"
        endpoints = [
            ("GET", "/v1/account", None),
            ("GET", "/v1/account/summary", None),
            ("GET", "/v1/account/saved-tags", None),
            ("POST", "/v1/account/saved-tags", {"tag": other["tag"]}),
            ("DELETE", f"/v1/account/saved-tags/{quote(other['tag'])}", None),
            ("GET", "/v1/account/groups", None),
            ("POST", "/v1/account/groups", {"name": "Intruder", "tags": []}),
            ("PATCH", group_path, {"name": "Intruder", "tags": []}),
            ("DELETE", group_path, None),
            ("POST", "/v1/account/exports", {"format": "google_sheets_scaffold"}),
            ("GET", f"/v1/account/exports/{uuid4()}", None),
        ]
        for method, target, value in endpoints:
            body = b"" if value is None else json_body(value)
            unsigned = client.request(method, target, content=body)
            assert unsigned.status_code == 401
            anonymous = client.request(
                method,
                target,
                content=body,
                headers=signed_headers(target, method=method, body=body),
            )
            assert anonymous.status_code == 403
            proof = signed_headers(
                target,
                method=method,
                body=body,
                provider=owner["provider"],
                subject=owner["subject"],
            )
            other_proof = signed_headers(
                target,
                method=method,
                body=body,
                provider=other["provider"],
                subject=other["subject"],
            )
            proof["X-ClashLens-Provider-Subject"] = other_proof[
                "X-ClashLens-Provider-Subject"
            ]
            assert (
                client.request(method, target, content=body, headers=proof).status_code
                == 401
            )
            if "/exports" in target:
                denied = call(client, owner, method, target, value)
                assert denied.status_code == 403
                assert denied.json() == {"error": "caller_operation_not_authorized"}
        assert_private_state(client, other)
