"""Attack alerts end to end: saved game data through the real worker, then the
bot's alert loop, run as the API's database role, sending to a fake Discord.
See docs/testing.md."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from aiohttp import web
from domain_test_support import as_api_role, domain_database, seed_battles
from test_discord_alerts import _rate_limited, delivered, stand_in_discord
from test_discord_bot_postgres import DISCORD_ID, _account, _verify
from test_reset_scenarios_postgres import (
    RESULTS,
    SEASON,
    Battle,
    Player,
    Story,
    _tag,
    day,
    defenses,
    multiplayer,
)

from bot.alerts import AlertLoop, AlertStore, Refused, Unreachable
from bot.commands import Commands
from bot.discord_app import DiscordMessenger
from bot.replies import Reply, Site
from bot.store import Store
from clashlens import api_accounts, api_discord_alerts
from clashlens.api_db import ApiDatabase, RequestBinding

SITE = Site("https://clashlens.test")
DEFENDER = "#2PP"
NAME = "Synthetic Legend I"


class FakeDiscord:
    """Records each try by its nonce and each DM delivered; refuses users in
    `refusing` as Discord would, and cannot be reached for the next `outages`
    tries, asking to wait `retry_after`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Reply]] = []
        self.tries: list[int] = []
        self.refusing: dict[str, str] = {}
        self.outages = 0
        self.retry_after = 0.0

    async def send_dm(
        self, discord_user_id: str, reply: Reply, nonce: int, _time_left: float
    ) -> None:
        self.tries.append(nonce)
        if discord_user_id in self.refusing:
            raise Refused(self.refusing[discord_user_id])
        if self.outages:
            self.outages -= 1
            raise Unreachable(self.retry_after)
        self.sent.append((discord_user_id, reply))


def _switch(database: ApiDatabase, account_id: int, tag: str, on: bool) -> int:
    """Turn alerts on or off as the website does, through the private API."""
    return api_discord_alerts.set_attack_alert(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="discord",
            provider_subject=DISCORD_ID,
            account_id=account_id,
            operation="account.update",
            method="POST",
            request_target="/v1/account/discord-alerts",
            identity={"attack_alert": tag, "on": on},
        ),
        normalized_tag=tag,
        on=on,
    ).status_code


def _summary_switch(database: ApiDatabase, account_id: int, tag: str, on: bool) -> int:
    """Turn a player's Legend day summary on or off as the website does,
    through the private API."""
    return api_discord_alerts.set_day_summary(
        database,
        RequestBinding(
            request_id=str(uuid4()),
            caller="typescript-website",
            provider="discord",
            provider_subject=DISCORD_ID,
            account_id=account_id,
            operation="account.update",
            method="POST",
            request_target="/v1/account/discord-alerts",
            identity={"day_summary": tag, "on": on},
        ),
        normalized_tag=tag,
        on=on,
    ).status_code


def _defend(connection_info: str, player_id: int, *, saved_ago: timedelta = timedelta(0)) -> None:
    """A defense of the player by a new untracked opponent, saved `saved_ago` ago."""
    with psycopg.connect(connection_info, autocommit=True) as connection:
        seed_battles(connection, player_id, [(SEASON + timedelta(hours=3), -16)])
        connection.execute(
            "UPDATE legend_battles SET created_at = clock_timestamp() - %s"
            " WHERE id = (SELECT max(id) FROM legend_battles)",
            (saved_ago,),
        )


@contextmanager
def _alerts_on(database_url: str) -> Iterator[tuple[str, ApiDatabase, int, int]]:
    """The account's one verified player, #2PP, with alerts on for an hour and
    no confirmation due: (connection info, API database, account, player id)."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                player = connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true)"
                    " RETURNING id"
                ).fetchone()[0]
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, "#2PP")
            assert _switch(api, mine, "#2PP", True) == 200
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "UPDATE discord_attack_alerts SET enabled_at = enabled_at - interval '1 hour',"
                    " confirm_requested_at = confirm_requested_at - interval '1 hour'"
                )
            yield connection_info, api, mine, player
        finally:
            api.close()
            owner.close()


def test_an_attack_the_worker_saves_sends_one_dm_with_the_attackers_hit_rate(
    database_url: str, archive_server, monkeypatch
) -> None:
    """DEFENDER's alerts go on after Clash Lens saved their Day 1 defense, so
    the bot confirms that, then only the Day 3 defenses saved later send a
    DM each, once however often the loop looks. The attacker's hit rate is
    the one /stats shows for them; an attacker Clash Lens never read is
    named as DEFENDER's log names them, with no hit rate tracked yet."""
    day_1, day_2, day_3 = day(1), day(2), day(3)
    # The fixture names each log row's opponent by its place in the log, so
    # DEFENDER's second defense is by the player tagged _tag(2), and the
    # third by _tag(3), whose profile Clash Lens never read.
    attacker, stranger = f"#{_tag(2)}", f"#{_tag(3)}"
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Player(tag) for tag in (DEFENDER, attacker)})
        # The attacker's own log: Day 1 three attacks, two three-stars, and a
        # multiplayer battle in the row whose opponent would be themselves;
        # Day 2 one three-star attack. 5,000 to 5,110 to 5,150.
        story.players[attacker].battles = [
            Battle(day_1 + timedelta(hours=1), 40, True),
            *multiplayer(day_1 + timedelta(hours=1, minutes=10), 1),
            Battle(day_1 + timedelta(hours=2), 40, True),
            Battle(day_1 + timedelta(hours=3), 30, True),
            Battle(day_2 + timedelta(hours=1), 40, True),
        ]
        story.players[DEFENDER].battles = defenses(day_1, 20)
        story.reset(day_1, {attacker: (5000, SEASON)})
        story.reads(day_1 + timedelta(minutes=20), {attacker: 5000})
        story.run_worker()
        story.reads(day_2 - timedelta(minutes=10), {attacker: 5110})
        story.reset(day_2, {attacker: (5110, SEASON)})
        story.reads(day_2 + timedelta(minutes=20), {attacker: 5110})
        story.run_worker()
        story.reads(day_3 - timedelta(minutes=10), {attacker: 5150})
        story.reset(day_3, {attacker: (5150, SEASON), DEFENDER: (4980, SEASON)})
        story.reads(day_3 + timedelta(minutes=20), {attacker: 5150, DEFENDER: 4980})
        story.run_worker()

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, DEFENDER)
            assert _switch(api, mine, DEFENDER, True) == 200
            store = Store(api)
            discord = FakeDiscord()
            loop = AlertLoop(
                AlertStore(api, store), discord, SITE,
                now=lambda: day_3 - timedelta(minutes=1),
            )
            before = asyncio.run(loop.tick())

            # Attacked twice during Day 3, then the collector reads DEFENDER's log.
            story.players[DEFENDER].battles += [
                Battle(day_3 + timedelta(hours=1), -30, False),
                Battle(day_3 + timedelta(hours=1, minutes=30), -20, False),
            ]
            story.read(DEFENDER, day_3 + timedelta(hours=2))
            story.run_worker()
            sent = [asyncio.run(loop.tick()) for _ in range(3)]

            account = store.account(DISCORD_ID)
            assert account is not None
            stats = Commands(store, SITE, now=lambda: day_3 - timedelta(minutes=1)).stats(
                account, attacker)
        finally:
            api.close()
            owner.close()

    assert (before, sent) == (1, [2, 0, 0])
    stars, destruction = RESULTS[30]
    stranger_stars, stranger_destruction = RESULTS[20]
    assert len(discord.sent) == 3
    assert discord.sent[0][1].title == f"Attack alerts are on for {NAME} {DEFENDER}"
    alerts = [reply for _, reply in discord.sent[1:]]
    assert {user for user, _ in discord.sent} == {DISCORD_ID}
    assert {reply.title for reply in alerts} == {f"{NAME} {DEFENDER} was attacked"}
    assert sorted(reply.body.splitlines() for reply in alerts) == sorted([
        [
            f"{NAME} {attacker}: {stars}★ · {destruction}% · −30 trophies",
            f"{NAME}'s hit rate in this attack's Season: 75%",
        ],
        [
            f"Opponent {stranger}: {stranger_stars}★ · {stranger_destruction}% · −20 trophies",
            "Opponent's hit rate in this attack's Season: not tracked yet",
        ],
    ])
    assert "hit rate 75% (3 three-stars)" in stats.body
    assert not any(reply.private for reply in alerts)


def test_alerts_follow_the_switch_skip_stale_battles_and_retry_only_outages(
    database_url: str, monkeypatch
) -> None:
    """Battles saved more than 15 minutes ago, battles saved before the switch
    went on and players whose switch is off send nothing. A DM Discord refuses
    is tried once, logged, and shown on the website with its reason. A DM
    Discord can't take just then is tried again with the same nonce and
    delivered once."""
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                ids = {
                    tag: connection.execute(
                        "INSERT INTO players (normalized_tag, active) VALUES (%s, true)"
                        " RETURNING id", (tag,),
                    ).fetchone()[0]
                    for tag in ("#2PP", "#8QQ")
                }
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, "#2PP")
            _verify(owner, mine, "#8QQ")
            assert _switch(api, mine, "#2PP", True) == 200
            assert _switch(api, mine, "#9RR", True) == 404
            now = SEASON + timedelta(hours=3)

            def defend(tag: str, *, saved_ago: timedelta = timedelta(0)) -> None:
                _defend(connection_info, ids[tag], saved_ago=saved_ago)

            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "UPDATE discord_attack_alerts SET enabled_at = enabled_at - interval '1 hour',"
                    " confirm_requested_at = confirm_requested_at - interval '1 hour'"
                )
            defend("#2PP", saved_ago=timedelta(minutes=20))
            defend("#2PP", saved_ago=timedelta(minutes=70))
            defend("#8QQ")
            discord = FakeDiscord()
            loop = AlertLoop(AlertStore(api, Store(api)), discord, SITE, now=lambda: now)
            quiet = asyncio.run(loop.tick())

            discord.refusing[DISCORD_ID] = "no_shared_server"
            defend("#2PP")
            refused = [asyncio.run(loop.tick()) for _ in range(2)]
            shown = api_discord_alerts.get_attack_alerts(api, mine)

            del discord.refusing[DISCORD_ID]
            defend("#2PP")
            delivered = asyncio.run(loop.tick())
            cleared = api_discord_alerts.get_attack_alerts(api, mine)

            discord.outages = 1
            defend("#2PP")
            retried = [asyncio.run(loop.tick()) for _ in range(3)]

            assert _switch(api, mine, "#2PP", False) == 200
            defend("#2PP")
            off = asyncio.run(loop.tick())
            with psycopg.connect(connection_info, autocommit=True) as connection:
                log = connection.execute(
                    "SELECT id, state, reason FROM discord_attack_alert_deliveries"
                    " ORDER BY battle_id"
                ).fetchall()
        finally:
            api.close()
            owner.close()

    assert (quiet, refused, delivered, retried, off) == (0, [0, 0], 1, [0, 1, 0], 0)
    assert [(state, reason, discord.tries.count(id)) for id, state, reason in log] == [
        ("refused", "no_shared_server", 1),
        ("sent", None, 1),
        ("sent", None, 2),
    ]
    assert len(discord.sent) == 2
    assert shown["undelivered"]["tag"] == "#2PP"
    assert shown["undelivered"]["reason"] == "no_shared_server"
    assert [(player["tag"], player["on"]) for player in shown["players"]] == [
        ("#2PP", True), ("#8QQ", False)]
    assert cleared["undelivered"] is None
    assert discord.sent[0][1].body.splitlines()[0].endswith(": 1★ · 33% · −16 trophies")


def test_turning_alerts_on_confirms_once_and_reconnecting_tries_again(
    database_url: str,
) -> None:
    """Turning alerts on sends one confirmation. Discord refusing it shows on
    the website, and it is not retried; reconnecting Discord, which adds the
    bot, clears that and confirms again, and still sends an attack saved
    before the reconnect."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                player = connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true)"
                    " RETURNING id"
                ).fetchone()[0]
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, "#2PP")
            discord = FakeDiscord()
            discord.refusing[DISCORD_ID] = "no_shared_server"
            loop = AlertLoop(AlertStore(api, Store(api)), discord, SITE)
            assert _switch(api, mine, "#2PP", True) == 200
            refused = [asyncio.run(loop.tick()) for _ in range(2)]
            shown = api_discord_alerts.get_attack_alerts(api, mine)["undelivered"]

            del discord.refusing[DISCORD_ID]
            assert _switch(api, mine, "#2PP", True) == 200
            still_on = asyncio.run(loop.tick())
            # Attacked, then Discord connected again before the bot looks.
            _defend(connection_info, player)
            reconnected = api_accounts.link_provider(
                api,
                RequestBinding(
                    request_id=str(uuid4()),
                    caller="typescript-website",
                    provider="discord",
                    provider_subject=DISCORD_ID,
                    account_id=mine,
                    operation="account.update",
                    method="POST",
                    request_target="/v1/account/providers/discord/link",
                    identity={"provider": "discord"},
                ),
                account_id=mine,
                provider="discord",
                provider_subject=DISCORD_ID,
            ).status_code
            cleared = api_discord_alerts.get_attack_alerts(api, mine)["undelivered"]
            confirmed = [asyncio.run(loop.tick()) for _ in range(2)]
        finally:
            api.close()
            owner.close()

    assert (refused, still_on, reconnected, confirmed) == ([0, 0], 0, 200, [2, 0])
    assert shown is not None and (shown["tag"], shown["reason"]) == ("#2PP", "no_shared_server")
    assert cleared is None
    assert [reply.title for _, reply in discord.sent] == [
        "Attack alerts are on for Unnamed #2PP",
        "Unnamed #2PP was attacked",
    ]


def test_a_dm_discord_did_not_take_is_tried_again_only_within_two_minutes(
    database_url: str, monkeypatch
) -> None:
    """A DM Discord did not take is tried again with the same nonce only while
    Discord's nonce check still stops a repeat: 2 minutes from its first try,
    here shortened to 1 second. After that it is logged and shown as not
    delivered, without another try, even while Discord asked to wait
    longer."""
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    monkeypatch.setattr("bot.alerts.NONCE_SECONDS", 1.0)
    with _alerts_on(database_url) as (connection_info, api, mine, player):
        discord = FakeDiscord()
        loop = AlertLoop(AlertStore(api, Store(api)), discord, SITE)
        discord.outages = 100
        _defend(connection_info, player)
        within = [asyncio.run(loop.tick()) for _ in range(2)]
        time.sleep(1.1)
        after = [asyncio.run(loop.tick()) for _ in range(2)]

        discord.retry_after = 60.0
        _defend(connection_info, player)
        waiting = asyncio.run(loop.tick())
        time.sleep(1.1)
        waited = asyncio.run(loop.tick())
        gave_up = api_discord_alerts.get_attack_alerts(api, mine)["undelivered"]
        with psycopg.connect(connection_info, autocommit=True) as connection:
            log = connection.execute(
                "SELECT id, state FROM discord_attack_alert_deliveries ORDER BY battle_id"
            ).fetchall()

    assert (within, after, waiting, waited) == ([0, 0], [0, 0], 0, 0)
    assert [(state, discord.tries.count(id)) for id, state in log] == [
        ("failed", 2), ("failed", 1)]
    assert len(discord.tries) == 3 and not discord.sent
    assert gave_up is not None and gave_up["reason"] == "unreachable"


def test_a_rate_limit_longer_than_a_dms_time_left_fails_it_at_once(
    database_url: str, monkeypatch
) -> None:
    """The bot's real client against a local stand-in for Discord that rate
    limits the first DM for 60 seconds, 100 seconds into its 2 minutes, here
    by shortening them to 20 seconds. The DM is not waited on or sent again:
    it is logged as not delivered at once, and the next alert goes straight
    out."""
    monkeypatch.setattr("bot.alerts.NONCE_SECONDS", 20.0)

    def answer(n: int) -> web.Response:
        return _rate_limited(60.0) if n == 1 else delivered(n)

    with _alerts_on(database_url) as (connection_info, api, _mine, player):

        async def scenario() -> tuple[int, float, list[dict]]:
            async with stand_in_discord(monkeypatch, answer) as (client, sends):
                loop = AlertLoop(AlertStore(api, Store(api)), DiscordMessenger(client), SITE)
                await asyncio.to_thread(_defend, connection_info, player)
                await asyncio.to_thread(_defend, connection_info, player)
                started = time.monotonic()
                sent = await loop.tick()
                return sent, time.monotonic() - started, sends

        sent, took, sends = asyncio.run(scenario())
        with psycopg.connect(connection_info, autocommit=True) as connection:
            log = connection.execute(
                "SELECT id, state FROM discord_attack_alert_deliveries ORDER BY battle_id"
            ).fetchall()

    assert sent == 1 and took < 10
    assert [(state, [body["nonce"] for body in sends].count(str(id))) for id, state in log] == [
        ("failed", 1), ("sent", 1)]


def test_dms_waiting_for_discord_outlast_a_failed_round(
    database_url: str, monkeypatch
) -> None:
    """Two DMs wait for Discord when a database error ends the round that
    would try them again; the next round still tries and delivers both."""
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    with _alerts_on(database_url) as (connection_info, api, _mine, player):
        discord = FakeDiscord()
        alerts = AlertStore(api, Store(api))
        loop = AlertLoop(alerts, discord, SITE)
        discord.outages = 2
        _defend(connection_info, player)
        _defend(connection_info, player)
        waiting = asyncio.run(loop.tick())

        def database_restarting(_delivery: object) -> bool:
            raise psycopg.OperationalError("the database is restarting")

        alerts.still_due = database_restarting  # type: ignore[method-assign]
        with pytest.raises(psycopg.OperationalError):
            asyncio.run(loop.tick())
        del alerts.still_due
        delivered = asyncio.run(loop.tick())

    assert (waiting, delivered) == (0, 2)
    assert len(discord.sent) == 2


def test_the_page_lists_every_verified_player(database_url: str) -> None:
    """A player with alerts on stays on the page, to be turned off, however
    many players sort before it."""
    with _alerts_on(database_url) as (connection_info, api, mine, _player):
        with psycopg.connect(connection_info, autocommit=True) as connection:
            tags = [
                row[0]
                for row in connection.execute(
                    "INSERT INTO players (normalized_tag, active)"
                    " SELECT '#0' || translate(lpad(n::text, 4, '0'), '0123456789',"
                    " '0289PYLQGR'), true FROM generate_series(1, 500) AS n"
                    " RETURNING normalized_tag"
                ).fetchall()
            ]
        owner = ApiDatabase(connection_info)
        try:
            for tag in tags:
                _verify(owner, mine, tag)
        finally:
            owner.close()
        players = api_discord_alerts.get_attack_alerts(api, mine)["players"]

    assert len(players) == 501
    assert players[-1] == {"tag": "#2PP", "name": None, "on": True, "summary_on": False}


def test_moving_a_player_away_forgets_its_alerts(database_url: str) -> None:
    """Moving the player to another account and back does not turn its
    attack alerts or its Legend day summary on again."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true)")
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            theirs = _account(owner, "google", "google-subject", "other")
            _verify(owner, mine, "#2PP")
            assert _switch(api, mine, "#2PP", True) == 200
            assert _summary_switch(api, mine, "#2PP", True) == 200
            _verify(owner, theirs, "#2PP")
            _verify(owner, mine, "#2PP")
            players = api_discord_alerts.get_attack_alerts(api, mine)["players"]
        finally:
            api.close()
            owner.close()
    assert players == [{"tag": "#2PP", "name": None, "on": False, "summary_on": False}]
