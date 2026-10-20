"""Unlinking a player from Settings, run as the private API's database role."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import domain_database
from test_crews_postgres import Site, _while_held

from clashlens import api_accounts, api_crews, api_discord_alerts, api_overlays
from clashlens.api_db import RequestBinding


@pytest.fixture
def site(database_url: str):
    with domain_database(database_url) as connection_info:
        built = Site(connection_info)
        try:
            yield built
        finally:
            built.close()


def _account_call(
    site: Site, username: str, function: Any, tag: str, method: str, target: str,
    request_id: str | None = None, **values: Any,
) -> Any:
    binding = RequestBinding(
        request_id=request_id or str(uuid4()),
        caller="typescript-website",
        provider="google",
        provider_subject=f"subject-{username}",
        account_id=site.ids[username],
        operation="account.update",
        method=method,
        request_target=target,
        identity={"tag": tag, **values},
    )
    return function(site.api, binding, normalized_tag=tag, **values)


def _unlink(site: Site, username: str, tag: str, request_id: str | None = None) -> Any:
    return _account_call(
        site, username, api_accounts.unlink_player, tag,
        "DELETE", f"/v1/account/players/{quote(tag)}", request_id,
    )


def test_unlinking_a_player_takes_it_off_only_that_account(site: Site) -> None:
    for name in ("first", "host", "other"):
        site.account(name)
    site.link("first", "#2PP")
    site.link("first", "#8PY")
    site.link("host", "#9QQ")
    hosted = site.make("host", ["#9QQ"])
    assert site.accept("first", site.invite("host", hosted), ["#2PP"]).status_code == 200
    own = site.make("first", ["#2PP", "#8PY"])
    first = site.ids["first"]
    assert _account_call(
        site, "first", api_accounts.set_default_player, "#2PP",
        "POST", "/v1/account/default-player",
    ).status_code == 200
    for switch in (api_discord_alerts.set_attack_alert, api_discord_alerts.set_day_summary):
        assert _account_call(
            site, "first", switch, "#2PP", "POST", "/v1/account/discord-alerts", on=True
        ).status_code == 200
    api_overlays.get_overlay(site.api, first)
    api_overlays.save_overlay(site.api, first, tag="#2PP", settings={})

    # Nobody else can unlink it, and nothing changes when they try.
    assert _unlink(site, "other", "#2PP").payload == {"error": "player_not_verified"}
    assert _unlink(site, "host", "#2PP").status_code == 404

    request_id = str(uuid4())
    unlinked = _unlink(site, "first", "#2PP", request_id)
    assert (unlinked.status_code, unlinked.payload) == (200, {"unlinked": "#2PP"})
    # The same request again gets the same answer; a new one finds nothing.
    assert _unlink(site, "first", "#2PP", request_id).payload == {"unlinked": "#2PP"}
    assert _unlink(site, "first", "#2PP").status_code == 404

    summary = api_accounts.get_multi_account_summary(site.api, first)
    assert summary is not None
    assert [player["tag"] for player in summary["verified_players"]] == ["#8PY"]
    assert summary["default_player"] == "#8PY"
    alerts = api_discord_alerts.get_attack_alerts(site.api, first)
    assert [player["tag"] for player in alerts["players"]] == ["#8PY"]
    assert site.sql(
        """
        SELECT (SELECT count(*) FROM discord_attack_alerts WHERE account_id = %s),
               (SELECT count(*) FROM discord_day_summary_on WHERE account_id = %s)
        """,
        (first, first),
    ) == [(0, 0)]
    assert api_overlays.get_overlay(site.api, first)["tag"] is None
    # Out of the hosted crew, where it was the only place; still the owner of
    # its own crew with the player it kept.
    assert site.crew("first", hosted) is None
    kept = site.crew("first", own)
    assert (kept["my_role"], kept["used"]) == ("owner", 1)
    # The game data stays, and the player can be linked again.
    assert site.sql("SELECT count(*) FROM players WHERE normalized_tag = '#2PP'") == [(1,)]
    site.link("first", "#2PP")
    relinked = api_accounts.get_multi_account_summary(site.api, first)
    assert relinked is not None
    assert sorted(player["tag"] for player in relinked["verified_players"]) == [
        "#2PP", "#8PY",
    ]
    assert relinked["default_player"] == "#8PY"


def test_an_owner_hands_over_before_unlinking_their_only_player_in_their_crew(
    site: Site,
) -> None:
    site.account("owner")
    site.account("guest")
    site.link("owner", "#2PP")
    site.link("owner", "#8PY")
    site.link("guest", "#9QQ")
    crew_id = site.make("owner", ["#2PP"])
    assert site.accept("guest", site.invite("owner", crew_id), ["#9QQ"]).status_code == 200
    owner = site.ids["owner"]

    request_id = str(uuid4())
    refused = _unlink(site, "owner", "#2PP", request_id)
    assert (refused.status_code, refused.payload) == (
        409,
        {
            "error": "owner_must_hand_over",
            "crews": [{"crew_id": crew_id, "name": "Night Owls"}],
        },
    )
    assert _unlink(site, "owner", "#2PP", request_id).payload == refused.payload
    # Nothing changed: still linked, still in the crew, still its owner.
    summary = api_accounts.get_multi_account_summary(site.api, owner)
    assert summary is not None
    assert [player["tag"] for player in summary["verified_players"]] == ["#2PP", "#8PY"]
    crew = site.crew("owner", crew_id)
    assert (crew["my_role"], crew["used"]) == ("owner", 2)
    # A player outside the crew still unlinks.
    assert _unlink(site, "owner", "#8PY").status_code == 200

    handed = site.call("owner", api_crews.hand_over, crew_id=crew_id, username="guest")
    assert handed.status_code == 200
    assert _unlink(site, "owner", "#2PP").status_code == 200
    assert site.crew("owner", crew_id) is None


def test_unlinking_waits_for_a_crew_being_made_with_that_player(site: Site) -> None:
    site.account("owner")
    site.link("owner", "#2PP")
    crew_id = str(uuid4())

    def make(held: psycopg.Connection) -> None:
        crew = held.execute(
            "INSERT INTO crews (public_id, name, size) VALUES (%s, 'Night Owls', 50)"
            " RETURNING id",
            (crew_id,),
        ).fetchone()[0]
        held.execute(
            "INSERT INTO crew_accounts (crew_id, account_id, role) VALUES (%s, %s, 'owner')",
            (crew, site.ids["owner"]),
        )
        held.execute(
            """
            INSERT INTO crew_players (crew_id, account_id, player_id)
            SELECT %s, %s, id FROM players WHERE normalized_tag = '#2PP'
            """,
            (crew, site.ids["owner"]),
        )

    with psycopg.connect(site.info, autocommit=True) as held:
        refused = _while_held(site, held, make, lambda: _unlink(site, "owner", "#2PP"))
    assert (refused.status_code, refused.payload) == (
        409,
        {
            "error": "owner_must_hand_over",
            "crews": [{"crew_id": crew_id, "name": "Night Owls"}],
        },
    )
    crew = site.crew("owner", crew_id)
    assert (crew["my_role"], crew["used"]) == ("owner", 1)
