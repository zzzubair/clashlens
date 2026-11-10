"""View as against the real schema and the signed private API: only an
account recorded in site_owner_accounts gets another player's card, nobody
does while none is recorded, nothing any application role runs can record
one, and every other account gets one answer whatever tag it sends."""

from __future__ import annotations

import json
from urllib.parse import quote

import psycopg
import pytest
from discord_bot_postgres_support import _account, _verify
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from owner_test_support import _record_owner
from private_api_support import NOW, NOW_SECONDS, TS_CURRENT, signed_headers

from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.api_player_presentation import calendar_payload
from clashlens.domain import ranked_day_for

APPLICATION_ROLES = (
    "clashlens_archive_retention",
    "clashlens_collector",
    "clashlens_history_retention",
    "clashlens_python_api",
    "clashlens_python_worker",
    "clashlens_replay_request",
    "clashlens_support_transfer",
)
OWNER = ("google", "owner-subject")
OTHER = ("discord", "other-subject")
VIEWED = "#2PP8QLY0"
UNLINKED = "#8PY2QLY0"


def _ask(client: TestClient, who: tuple[str, str] | None, tag: str | None = None):
    target = "/v1/account/view-as" + ("" if tag is None else f"?tag={quote(tag, safe='')}")
    provider, subject = who or ("", "")
    headers = signed_headers(target, provider=provider, subject=subject)
    return client.get(target, headers=headers)


@pytest.fixture
def site(database_url: str):
    with domain_database(database_url) as connection_info:
        admin = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                for tag in (VIEWED, UNLINKED, "#9QQ"):
                    connection.execute(
                        "INSERT INTO players (normalized_tag, active) VALUES (%s, true)",
                        (tag,),
                    )
            owner = _account(admin, *OWNER, "owner")
            other = _account(admin, *OTHER, "other")
            # The viewed player belongs to the other account.
            _verify(admin, other, VIEWED)
            _verify(admin, owner, "#9QQ")
            app = create_app(
                database=api,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
            )
            with TestClient(app) as client:
                yield client, connection_info, owner
        finally:
            admin.close()
            api.close()


NOT_OWNER = {"kind": "view-as", "owner": False, "player": None}
ATTEMPTS = (None, VIEWED, UNLINKED, "#9999999", "not-a-tag", "#", "x" * 200)


def test_nobody_can_view_as_until_an_owner_is_recorded(site) -> None:
    client, _, _ = site
    for who in (OWNER, OTHER):
        for tag in ATTEMPTS:
            answer = _ask(client, who, tag)
            assert answer.status_code == 200
            assert answer.json() == NOT_OWNER


def test_only_the_recorded_owner_gets_any_players_card(site, capsys) -> None:
    client, connection_info, owner = site
    _record_owner(connection_info, owner)
    capsys.readouterr()

    # Another account's player and a player nobody linked, the second typed
    # in lower case: the same card a linked player gets, and nothing about
    # the account it belongs to.
    for tag, expected in ((VIEWED, VIEWED), (" #8py2qly0 ", UNLINKED)):
        answer = _ask(client, OWNER, tag).json()
        assert answer["owner"] is True
        assert answer["player"]["tag"] == expected
        assert answer["calendar"]["as_of"] == NOW.isoformat()
        assert answer["calendar"]["day_end"] == ranked_day_for(NOW).end.isoformat()
        assert set(answer["player"]) <= {
            "tag", "name", "clan", "state", "reason", "trophies",
            "season_reset_pending", "rank", "league", "today", "left_legend_i",
        }
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert logged == [
        {"event": "owner_view_as", "account_id": owner, "tag": tag, "at": NOW.isoformat()}
        for tag in (VIEWED, UNLINKED)
    ]

    # An unknown or malformed tag, or none: plainly no player.
    for tag in (None, "#9999999", "not-a-tag", "#", "8PY2QLY0", "x" * 200):
        assert _ask(client, OWNER, tag).json() == {
            "kind": "view-as", "owner": True, "player": None, "pinned": None,
            "follow": {"attack": False, "day_summary": False},
            "calendar": calendar_payload(NOW),
        }

    # Every other account: one answer whatever it sends, so it cannot learn
    # which tags exist or who the owner is.
    for tag in ATTEMPTS:
        assert _ask(client, OTHER, tag).json() == NOT_OWNER


def test_a_request_without_a_signed_in_account_is_refused(site) -> None:
    client, connection_info, owner = site
    _record_owner(connection_info, owner)
    assert _ask(client, None, VIEWED).status_code == 403
    assert _ask(client, ("google", "nobody-has-this"), VIEWED).status_code == 403
    unsigned = client.get(f"/v1/account/view-as?tag={quote(VIEWED, safe='')}")
    assert unsigned.status_code in (401, 403)


def test_no_application_role_can_record_or_change_an_owner(site) -> None:
    _, connection_info, owner = site
    with psycopg.connect(connection_info, autocommit=True) as connection:
        for role in APPLICATION_ROLES:
            for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES"):
                assert not connection.execute(
                    "SELECT has_table_privilege(%s, 'site_owner_accounts', %s)",
                    (role, privilege),
                ).fetchone()[0], (role, privilege)
            readable = connection.execute(
                "SELECT has_table_privilege(%s, 'site_owner_accounts', 'SELECT')", (role,)
            ).fetchone()[0]
            assert readable == (role == "clashlens_python_api"), role
    with psycopg.connect(as_api_role(connection_info), autocommit=True) as api:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            api.execute(
                "INSERT INTO site_owner_accounts (account_id) VALUES (%s)", (owner,)
            )
