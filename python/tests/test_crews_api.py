from __future__ import annotations

from uuid import uuid4

import pytest
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_api_security import FakeDatabase
from test_crews_postgres import Site
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, json_body, signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase

CREW = "00000000-0000-4000-8000-000000000001"
CODE = "A" * 22
ROUTES = [
    ("GET", "/v1/account/crews", None),
    ("POST", "/v1/account/crews", {"name": "x", "size": 50, "tags": ["#2PP"]}),
    ("GET", f"/v1/account/crews/{CREW}", None),
    ("PATCH", f"/v1/account/crews/{CREW}", {"name": "x"}),
    ("DELETE", f"/v1/account/crews/{CREW}", None),
    ("POST", f"/v1/account/crews/{CREW}/players", {"tags": ["#2PP"]}),
    ("DELETE", f"/v1/account/crews/{CREW}/players/%232PP", None),
    ("PATCH", f"/v1/account/crews/{CREW}/members/akira", {"role": "admin"}),
    ("POST", f"/v1/account/crews/{CREW}/owner", {"username": "akira"}),
    ("DELETE", f"/v1/account/crews/{CREW}/members/me", None),
    ("POST", f"/v1/account/crews/{CREW}/invites", {}),
    ("DELETE", f"/v1/account/crews/{CREW}/invites/{CREW}", None),
    ("GET", f"/v1/account/crew-invites/{CODE}", None),
    ("POST", f"/v1/account/crew-invites/{CODE}/accept", {"tags": ["#2PP"]}),
]


def call(client, username, method, target, value=None):
    body = b"" if value is None else json_body(value)
    headers = signed_headers(
        target, method=method, body=body, provider="google", subject=f"subject-{username}"
    )
    return client.request(method, target, content=body, headers=headers)


@pytest.mark.parametrize("bad_body", [False, True])
def test_every_crew_route_is_missing_while_crews_are_off(bad_body: bool) -> None:
    app = create_app(
        FakeDatabase(),
        keys={("typescript-website", "current"): TS_CURRENT},
        clock=lambda: NOW_SECONDS,
    )
    with TestClient(app) as client:
        for method, target, value in ROUTES:
            # The switch answers before the request itself is checked.
            sent = {"unexpected": True} if bad_body and value is not None else value
            response = call(client, "akira", method, target, sent)
            assert (response.status_code, response.json()) == (
                404,
                {"error": "crews_disabled"},
            ), (method, target)


@pytest.fixture
def served(database_url: str):
    with domain_database(database_url) as connection_info:
        site = Site(connection_info)
        database = ApiDatabase(as_api_role(connection_info))
        app = create_app(
            database,
            keys={("typescript-website", "current"): TS_CURRENT},
            clock=lambda: NOW_SECONDS,
            now=lambda: NOW,
            dashboard_enabled=True,
        )
        try:
            with TestClient(app) as client:
                yield site, client
        finally:
            site.close()


def test_a_crew_made_and_joined_through_the_api(served) -> None:
    site, client = served
    for name, tags in (("akira", ["#2PP"]), ("bea", ["#8PY", "#9QQ"]), ("cleo", ["#PPP"])):
        site.account(name)
        for tag in tags:
            site.link(name, tag)

    def refused(value: dict[str, object]) -> str:
        response = call(client, "akira", "POST", "/v1/account/crews", value)
        assert response.status_code == 422
        return response.json()["error"]

    assert refused({"name": "Owls", "size": 1, "tags": ["#2PP"]}) == "invalid_crew_size"
    assert refused({"name": "Owls", "size": 101, "tags": ["#2PP"]}) == "invalid_crew_size"
    assert refused({"name": "  ", "tags": ["#2PP"]}) == "invalid_crew_name"
    assert refused({"name": "Owls", "tags": []}) == "invalid_request"

    created = call(client, "akira", "POST", "/v1/account/crews", {"name": " Owls ", "tags": ["#2PP"]})
    assert created.status_code == 201
    crew_id = created.json()["crew_id"]
    assert created.json() == {"crew_id": crew_id, "name": "Owls", "size": 50}

    invite = call(client, "akira", "POST", f"/v1/account/crews/{crew_id}/invites", {})
    assert invite.status_code == 200
    code = invite.json()["code"]
    assert len(code) == 22 and invite.json()["open_places"] == 49

    preview = call(client, "bea", "GET", f"/v1/account/crew-invites/{code}").json()
    assert (preview["state"], preview["name"], preview["owner_display_name"]) == (
        "ok", "Owls", "Akira",
    )
    assert call(client, "bea", "GET", "/v1/account/crew-invites/short").status_code == 422
    accepted = call(
        client, "bea", "POST", f"/v1/account/crew-invites/{code}/accept", {"tags": ["#8PY", "#9QQ"]}
    )
    assert accepted.json() == {"crew_id": crew_id}

    crew = call(client, "bea", "GET", f"/v1/account/crews/{crew_id}").json()
    assert (crew["kind"], crew["used"], crew["my_role"]) == ("crew", 3, "member")
    assert [(member["username"], member["role"], member["you"]) for member in crew["members"]] == [
        ("akira", "owner", False),
        ("bea", "member", True),
    ]

    # A clasher outside the crew reads it as missing.
    assert call(client, "cleo", "GET", f"/v1/account/crews/{crew_id}").json() == {
        "error": "crew_not_found"
    }
    assert call(client, "cleo", "GET", f"/v1/account/crews/{uuid4()}").status_code == 404

    promoted = call(
        client, "akira", "PATCH", f"/v1/account/crews/{crew_id}/members/bea", {"role": "admin"}
    )
    assert promoted.json() == {"username": "bea", "display_name": "Bea", "role": "admin"}
    kicked = call(client, "akira", "DELETE", f"/v1/account/crews/{crew_id}/players/%238PY")
    assert kicked.json() == {"removed": True, "tag": "#8PY", "left_crew": False}
    resized = call(client, "bea", "PATCH", f"/v1/account/crews/{crew_id}", {"size": 1})
    assert resized.json() == {"error": "invalid_crew_size"}
    resized = call(client, "bea", "PATCH", f"/v1/account/crews/{crew_id}", {"size": 2})
    assert resized.json()["size"] == 2
    assert call(
        client, "bea", "PATCH", f"/v1/account/crews/{crew_id}", {"name": "A", "size": 3}
    ).json() == {"error": "invalid_request"}
    assert call(client, "bea", "DELETE", f"/v1/account/crews/{crew_id}/members/me").json() == {
        "left": True, "crew_id": crew_id,
    }
    listed = call(client, "akira", "GET", "/v1/account/crews").json()
    assert [(crew["used"], crew["size"]) for crew in listed["crews"]] == [(1, 2)]
