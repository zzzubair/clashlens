"""The site owner's pin and follow against the real schema, the signed private
API and the real bot: only an account recorded in site_owner_accounts can pin
a player or follow one it is not linked to, every other account gets one
answer whatever tag it sends and writes nothing, the owner's switches never
move with the player's link nor touch the linked account's, removing the
owner stops the owner's DMs, and the bot sends the owner the same attack
alert and Legend day summary a linked account gets. See docs/testing.md."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from test_discord_alerts_postgres import (
    SITE,
    FakeDiscord,
    _defend,
    _summary_switch,
    _switch,
)
from test_discord_bot_postgres import _account, _verify
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, json_body, signed_headers
from test_reset_scenarios_postgres import (
    SEASON,
    Battle,
    Player,
    Story,
    attacks,
    day,
    defenses,
)

from bot.alerts import AlertLoop, AlertStore
from bot.replies import GOLD, PLAYER_COLOURS
from bot.store import Store
from bot.summaries import Summaries, Summary, SummaryLoop
from clashlens import api_view_as
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase, RequestBinding

OWNER_DISCORD = "100000000000000007"
OTHER_DISCORD = "100000000000000008"
OWNER = ("discord", OWNER_DISCORD)
OTHER = ("discord", OTHER_DISCORD)
THIRD = ("google", "third-subject")
FOLLOWED = "#2PP8QLY0"
UNLINKED = "#8PY2QLY0"
TAGS = (FOLLOWED, UNLINKED, "#9999999", "not-a-tag", "#", "x" * 64)


def _post(client: TestClient, who: tuple[str, str] | None, target: str, body: dict):
    payload = json_body(body)
    provider, subject = who or ("", "")
    return client.post(target, content=payload, headers=signed_headers(
        target, method="POST", body=payload, provider=provider, subject=subject,
        request_id=str(uuid4()),
    ))


def _ask(client: TestClient, who: tuple[str, str], tag: str | None = None):
    target = "/v1/account/view-as" + ("" if tag is None else f"?tag={tag.replace('#', '%23')}")
    return client.get(target, headers=signed_headers(target, provider=who[0], subject=who[1]))


def _record_owner(connection_info: str, account_id: int) -> None:
    """The one-time production step, run by the operator outside the app."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO site_owner_accounts (account_id) VALUES (%s)", (account_id,)
        )


def _remove_owner(connection_info: str, account_id: int) -> None:
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute("DELETE FROM site_owner_accounts WHERE account_id = %s", (account_id,))


def _rows(connection_info: str) -> dict[str, set[tuple]]:
    """Every pin and Discord switch row, by table."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        return {
            table: set(connection.execute(
                f"SELECT account_id, player_id FROM {table}"
            ).fetchall())
            for table in ("site_owner_pins", "discord_attack_alerts", "discord_day_summary_on")
        }


def _player(connection_info: str, tag: str) -> int:
    with psycopg.connect(connection_info, autocommit=True) as connection:
        return connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone()[0]


@pytest.fixture
def site(database_url: str):
    """The owner, another account FOLLOWED is linked to with both its own
    switches on, and a third account; UNLINKED is linked to nobody."""
    with domain_database(database_url) as connection_info:
        admin = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                for tag in (FOLLOWED, UNLINKED):
                    connection.execute(
                        "INSERT INTO players (normalized_tag, active) VALUES (%s, true)", (tag,)
                    )
            owner = _account(admin, *OWNER, "owner")
            other = _account(admin, *OTHER, "other")
            third = _account(admin, *THIRD, "third")
            _verify(admin, other, FOLLOWED)
            assert _switch(api, other, FOLLOWED, True) == 200
            assert _summary_switch(api, other, FOLLOWED, True) == 200
            app = create_app(
                database=api,
                keys={("typescript-website", "current"): TS_CURRENT},
                clock=lambda: NOW_SECONDS,
                now=lambda: NOW,
            )
            with TestClient(app) as client:
                yield client, connection_info, api, {"owner": owner, "other": other,
                                                      "third": third}
        finally:
            admin.close()
            api.close()


def _attempts(client: TestClient, who: tuple[str, str] | None):
    """Every pin and follow a caller could send, for every tag."""
    answers = []
    for tag in TAGS:
        answers.append(_post(client, who, "/v1/account/view-as/pin", {"tag": tag}))
        for alert in ("attack", "day_summary"):
            for on in (True, False):
                answers.append(_post(client, who, "/v1/account/view-as/follow",
                                     {"tag": tag, "alert": alert, "on": on}))
    answers.append(_post(client, who, "/v1/account/view-as/pin", {"tag": None}))
    return answers


def test_no_other_account_can_pin_or_follow_and_each_gets_one_answer(site) -> None:
    client, connection_info, _, accounts = site
    before = _rows(connection_info)
    # Before any owner is recorded, the owner-to-be is refused the same way.
    for who in (OWNER, OTHER, THIRD):
        answers = _attempts(client, who)
        assert {(answer.status_code, answer.text) for answer in answers} == {
            (404, '{"error":"not_found"}')
        }, who
    _record_owner(connection_info, accounts["owner"])
    for who in (OTHER, THIRD):
        answers = _attempts(client, who)
        assert {(answer.status_code, answer.text) for answer in answers} == {
            (404, '{"error":"not_found"}')
        }, who
        # Nor can they read the owner's pin or switches, or any card.
        for tag in (None, FOLLOWED, UNLINKED):
            assert _ask(client, who, tag).json() == {
                "kind": "view-as", "owner": False, "player": None,
            }
    # Signed out, or signed in with no account: refused before anything.
    for who in (None, ("google", "nobody-has-this")):
        assert {answer.status_code for answer in _attempts(client, who)} == {403}
    assert _rows(connection_info) == before


def test_a_non_owner_still_follows_only_its_own_linked_players(site) -> None:
    client, connection_info, _, accounts = site
    _record_owner(connection_info, accounts["owner"])
    before = _rows(connection_info)
    for tag in (FOLLOWED, UNLINKED):
        for alert in ("attack", "day_summary"):
            answer = _post(client, THIRD, "/v1/account/discord-alerts",
                           {"tag": tag, "alert": alert, "on": True})
            assert (answer.status_code, answer.json()) == (
                404, {"error": "player_not_verified"}
            )
    assert _rows(connection_info) == before
    # And the API role cannot give a non-owner a pin.
    with psycopg.connect(as_api_role(connection_info), autocommit=True) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            connection.execute(
                "INSERT INTO site_owner_pins (account_id, player_id) VALUES (%s, %s)",
                (accounts["third"], _player(connection_info, UNLINKED)),
            )


def test_the_owner_pins_and_follows_only_on_the_owners_own_rows(site) -> None:
    client, connection_info, _, accounts = site
    owner, other = accounts["owner"], accounts["other"]
    followed, unlinked = _player(connection_info, FOLLOWED), _player(connection_info, UNLINKED)
    _record_owner(connection_info, owner)
    before = _rows(connection_info)

    pinned = _post(client, OWNER, "/v1/account/view-as/pin", {"tag": " #2pp8qly0 "})
    assert (pinned.status_code, pinned.json()) == (200, {"pinned": FOLLOWED})
    assert _post(client, OWNER, "/v1/account/view-as/pin", {"tag": UNLINKED}).json() == {
        "pinned": UNLINKED
    }
    for tag in ("#9999999", "not-a-tag"):
        assert _post(client, OWNER, "/v1/account/view-as/pin", {"tag": tag}).json() == {
            "error": "player_not_found"
        }
        assert _post(client, OWNER, "/v1/account/view-as/follow",
                     {"tag": tag, "alert": "attack", "on": True}).status_code == 404
    for alert in ("attack", "day_summary"):
        assert _post(client, OWNER, "/v1/account/view-as/follow",
                     {"tag": FOLLOWED, "alert": alert, "on": True}).json() == {
            "tag": FOLLOWED, "alert": alert, "on": True,
        }
    answer = _ask(client, OWNER, FOLLOWED).json()
    assert (answer["pinned"], answer["follow"]) == (
        UNLINKED, {"attack": True, "day_summary": True}
    )
    assert _ask(client, OWNER, UNLINKED).json()["follow"] == {
        "attack": False, "day_summary": False,
    }
    rows = _rows(connection_info)
    assert rows["site_owner_pins"] == {(owner, unlinked)}
    # The linked account's own switches are where they were.
    for table in ("discord_attack_alerts", "discord_day_summary_on"):
        assert rows[table] == before[table] | {(owner, followed)}
        assert (other, followed) in rows[table]

    assert _post(client, OWNER, "/v1/account/view-as/follow",
                 {"tag": FOLLOWED, "alert": "attack", "on": False}).status_code == 200
    assert _post(client, OWNER, "/v1/account/view-as/pin", {"tag": None}).json() == {
        "pinned": None
    }
    rows = _rows(connection_info)
    assert rows["site_owner_pins"] == set()
    assert rows["discord_attack_alerts"] == before["discord_attack_alerts"]
    assert _ask(client, OWNER).json()["pinned"] is None


def test_the_owners_switches_stay_while_the_link_moves_between_other_accounts(site) -> None:
    client, connection_info, api, accounts = site
    owner, other, third = accounts["owner"], accounts["other"], accounts["third"]
    followed = _player(connection_info, FOLLOWED)
    _record_owner(connection_info, owner)
    for alert in ("attack", "day_summary"):
        assert _post(client, OWNER, "/v1/account/view-as/follow",
                     {"tag": FOLLOWED, "alert": alert, "on": True}).status_code == 200
    with psycopg.connect(connection_info, autocommit=True) as connection:
        enabled = connection.execute(
            "SELECT enabled_at FROM discord_attack_alerts WHERE account_id = %s", (owner,)
        ).fetchone()
    admin = ApiDatabase(connection_info)
    try:
        # Moved to the third account, which turns its own alerts on, then back.
        _verify(admin, third, FOLLOWED)
        assert _switch(api, third, FOLLOWED, True) == 200
        moved = _rows(connection_info)
        _verify(admin, other, FOLLOWED)
        back = _rows(connection_info)
        with admin.pool.connection() as connection:
            connection.execute(
                "DELETE FROM verified_player_links WHERE player_id = %s", (followed,)
            )
    finally:
        admin.close()
    gone = _rows(connection_info)
    for table in ("discord_attack_alerts", "discord_day_summary_on"):
        # Each move forgets only the switches of the account it leaves.
        assert moved[table] == {(owner, followed)} | (
            {(third, followed)} if table == "discord_attack_alerts" else set()
        )
        assert back[table] == {(owner, followed)}
        assert gone[table] == {(owner, followed)}
    with psycopg.connect(connection_info, autocommit=True) as connection:
        assert connection.execute(
            "SELECT enabled_at FROM discord_attack_alerts WHERE account_id = %s", (owner,)
        ).fetchone() == enabled


def test_removing_the_owner_drops_the_pin_and_follows_and_the_bot_sends_nothing(
    site,
) -> None:
    client, connection_info, api, accounts = site
    owner, other = accounts["owner"], accounts["other"]
    followed, unlinked = _player(connection_info, FOLLOWED), _player(connection_info, UNLINKED)
    admin = ApiDatabase(connection_info)
    try:
        # The owner also links a player of their own, whose switch stays.
        with psycopg.connect(connection_info, autocommit=True) as connection:
            mine = connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES ('#9QQ', true) RETURNING id"
            ).fetchone()[0]
        _verify(admin, owner, "#9QQ")
    finally:
        admin.close()
    assert _switch(api, owner, "#9QQ", True) == 200
    _record_owner(connection_info, owner)
    assert _post(client, OWNER, "/v1/account/view-as/pin", {"tag": FOLLOWED}).status_code == 200
    for tag in (FOLLOWED, UNLINKED):
        for alert in ("attack", "day_summary"):
            assert _post(client, OWNER, "/v1/account/view-as/follow",
                         {"tag": tag, "alert": alert, "on": True}).status_code == 200
    with psycopg.connect(connection_info, autocommit=True) as connection:
        connection.execute(
            "UPDATE discord_attack_alerts SET enabled_at = enabled_at - interval '1 hour',"
            " confirm_requested_at = confirm_requested_at - interval '1 hour'"
        )
    alerts = AlertStore(api, Store(api))
    summary = Summary(owner, OWNER_DISCORD, day(3), [])
    summaries = Summaries(api, Store(api), SITE, now=lambda: day(3) + timedelta(minutes=30))
    _defend(connection_info, unlinked)
    # While the owner is recorded, a defense of a followed player is due to the
    # owner, and so is the followed player's summary.
    assert [row["account_id"] for row in alerts.due()] == [owner]
    assert summaries.still_due(summary, (UNLINKED,))

    _remove_owner(connection_info, owner)
    rows = _rows(connection_info)
    assert rows["site_owner_pins"] == set()
    assert rows["discord_attack_alerts"] == {(other, followed), (owner, mine)}
    assert rows["discord_day_summary_on"] == {(other, followed)}

    # Switches left behind, as ones saved while the owner was being removed
    # would be, send nothing either: the bot checks the owner record itself.
    with psycopg.connect(connection_info, autocommit=True) as connection:
        for table in ("discord_attack_alerts", "discord_day_summary_on"):
            connection.execute(
                f"INSERT INTO {table} (account_id, player_id) VALUES (%s, %s)",
                (owner, unlinked),
            )
        connection.execute(
            "UPDATE discord_attack_alerts SET enabled_at = enabled_at - interval '1 hour'"
            " WHERE account_id = %s",
            (owner,),
        )
    _defend(connection_info, unlinked)
    assert alerts.due() == []
    assert not summaries.still_due(summary, (UNLINKED,))
    assert {row["tag"] for row in alerts.confirmations_due()} == set()
    discord = FakeDiscord()
    assert asyncio.run(AlertLoop(alerts, discord, SITE).tick()) == 0
    assert discord.sent == []
    assert _ask(client, OWNER, FOLLOWED).json() == {
        "kind": "view-as", "owner": False, "player": None,
    }


def _follow(database: ApiDatabase, account_id: int, tag: str, alert: str) -> int:
    """Turn the owner's own switch on as the View as bar does."""
    return api_view_as.set_follow(
        database,
        RequestBinding(
            request_id=str(uuid4()), caller="typescript-website", provider="discord",
            provider_subject=OWNER_DISCORD, account_id=account_id,
            operation="account.update", method="POST",
            request_target="/v1/account/view-as/follow",
            identity={"owner_follow": tag, "alert": alert, "on": True},
        ),
        tag=tag, alert=alert, on=True,  # type: ignore[arg-type]
    ).status_code


def test_the_bot_sends_the_owner_the_alerts_and_summary_the_linked_account_gets(
    database_url: str, archive_server, monkeypatch
) -> None:
    """FOLLOWED is linked to the other account, with its attack alerts and
    summary on. The owner follows both. Once the worker publishes the Day 3
    Reset's board, the bot sends each Discord user the same summary of
    FOLLOWED's Day 2; after the owner's confirmation, a Day 3 defense the
    worker saves sends each the same attack alert. Only the colour differs:
    the linked account's first player's colour, the owner's gold."""
    day_1, day_2, day_3 = day(1), day(2), day(3)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch, {FOLLOWED: Player(FOLLOWED)})
        story.players[FOLLOWED].battles = (
            attacks(day_1, 40, 40, 40, 30) + defenses(day_1, *[10] * 8)
            + attacks(day_2, 40, 40, 20) + defenses(day_2, 40, *[10] * 7)
        )
        story.reset(day_1, {FOLLOWED: (5000, SEASON)})
        story.reads(day_1 + timedelta(minutes=20), {FOLLOWED: 5000})
        story.run_worker()
        story.reads(day_2 - timedelta(minutes=10), {FOLLOWED: 5070})
        story.reset(day_2, {FOLLOWED: (5070, SEASON)})
        story.reads(day_2 + timedelta(minutes=20), {FOLLOWED: 5070})
        story.run_worker()
        story.reads(day_3 - timedelta(minutes=10), {FOLLOWED: 5060})
        story.reset(day_3, {FOLLOWED: (5060, SEASON)})
        story.reads(day_3 + timedelta(minutes=20), {FOLLOWED: 5060})
        story.run_worker()
        story.wait(timedelta(hours=1))

        admin = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            owner = _account(admin, *OWNER, "owner")
            other = _account(admin, *OTHER, "other")
            _verify(admin, other, FOLLOWED)
            assert _switch(api, other, FOLLOWED, True) == 200
            assert _summary_switch(api, other, FOLLOWED, True) == 200
            # Refused until the operator records the owner.
            assert _follow(api, owner, FOLLOWED, "attack") == 404
            _record_owner(connection_info, owner)
            for alert in ("attack", "day_summary"):
                assert _follow(api, owner, FOLLOWED, alert) == 200
            store = Store(api)
            summaries = FakeDiscord()
            summary_loop = SummaryLoop(
                Summaries(api, store, SITE, now=lambda: day_3 + timedelta(minutes=30)),
                summaries,
            )
            summaries_sent = asyncio.run(summary_loop.tick())

            discord = FakeDiscord()
            loop = AlertLoop(AlertStore(api, store), discord, SITE,
                             now=lambda: day_3 + timedelta(hours=2))
            confirmed = asyncio.run(loop.tick())
            story.players[FOLLOWED].battles += [Battle(day_3 + timedelta(hours=1), -30, False)]
            story.read(FOLLOWED, day_3 + timedelta(hours=2))
            story.run_worker()
            alerted = [asyncio.run(loop.tick()) for _ in range(2)]
        finally:
            api.close()
            admin.close()

    assert summaries_sent == 2
    by_user = {user: reply for user, reply in summaries.sent}
    assert set(by_user) == {OWNER_DISCORD, OTHER_DISCORD}
    assert by_user[OWNER_DISCORD].notice == by_user[OTHER_DISCORD].notice == "📅 Day 2 (06-10-2026)"
    assert [card.parts for card in by_user[OWNER_DISCORD].cards] == [
        card.parts for card in by_user[OTHER_DISCORD].cards
    ]
    assert [card.colour for card in by_user[OWNER_DISCORD].cards] == [GOLD]
    assert [card.colour for card in by_user[OTHER_DISCORD].cards] == [PLAYER_COLOURS[0]]

    assert (confirmed, alerted) == (2, [2, 0])
    confirmations, alerts = discord.sent[:2], discord.sent[2:]
    assert {user for user, _ in confirmations} == {OWNER_DISCORD, OTHER_DISCORD}
    assert all(reply.text.splitlines()[0] == "### Attack alerts are on"
               for _, reply in confirmations)
    by_user = {user: reply for user, reply in alerts}
    assert set(by_user) == {OWNER_DISCORD, OTHER_DISCORD}
    assert by_user[OWNER_DISCORD].text == by_user[OTHER_DISCORD].text
    assert by_user[OWNER_DISCORD].text.splitlines()[0].endswith("−30")
    assert [card.colour for card in by_user[OWNER_DISCORD].cards] == [GOLD]


def test_the_owner_turning_a_follow_twice_lands_where_one_turn_does(site) -> None:
    client, connection_info, _, accounts = site
    owner = accounts["owner"]
    followed = _player(connection_info, FOLLOWED)
    _record_owner(connection_info, owner)
    before = _rows(connection_info)

    def follow(alert: str, on: bool):
        return _post(client, OWNER, "/v1/account/view-as/follow",
                     {"tag": FOLLOWED, "alert": alert, "on": on}).json()

    for on in (True, True, False, False, True):
        for alert in ("attack", "day_summary"):
            assert follow(alert, on) == {"tag": FOLLOWED, "alert": alert, "on": on}
        rows = _rows(connection_info)
        for table in ("discord_attack_alerts", "discord_day_summary_on"):
            assert rows[table] == before[table] | ({(owner, followed)} if on else set())
