"""Attack alerts end to end: saved game data through the real worker, then the
bot's alert loop, run as the API's database role, sending to a fake Discord.
See docs/testing.md."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import psycopg
from domain_test_support import as_api_role, domain_database, seed_battles
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

from bot.alerts import AlertLoop, AlertStore, Refused
from bot.commands import Commands
from bot.replies import Reply, Site
from bot.store import Store
from clashlens import api_accounts, api_discord_alerts
from clashlens.api_db import ApiDatabase, RequestBinding

SITE = Site("https://clashlens.test")
DEFENDER = "#2PP"
NAME = "Synthetic Legend I"


class FakeDiscord:
    """Records each DM; refuses users in `refusing` as Discord would."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Reply]] = []
        self.refusing: dict[str, str] = {}

    async def send_dm(self, discord_user_id: str, reply: Reply) -> None:
        if discord_user_id in self.refusing:
            raise Refused(self.refusing[discord_user_id])
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


def test_an_attack_the_worker_saves_sends_one_dm_with_the_attackers_hit_rate(
    database_url: str, archive_server, monkeypatch
) -> None:
    """DEFENDER's alerts go on after Clash Lens saved their Day 1 defense, so
    the bot confirms that, then only the Day 3 defense saved later sends a DM,
    once however often the loop looks. The attacker's hit rate is the one
    /stats shows for them."""
    day_1, day_2, day_3 = day(1), day(2), day(3)
    # The fixture names each log row's opponent by its place in the log, so
    # DEFENDER's second defense is by the player tagged _tag(2).
    attacker = f"#{_tag(2)}"
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

            # Attacked during Day 3, then the collector reads DEFENDER's log.
            story.players[DEFENDER].battles.append(
                Battle(day_3 + timedelta(hours=1), -30, False))
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

    assert (before, sent) == (1, [1, 0, 0])
    stars, destruction = RESULTS[30]
    assert len(discord.sent) == 2
    assert discord.sent[0][1].title == f"Attack alerts are on for {NAME} {DEFENDER}"
    user, reply = discord.sent[1]
    assert user == DISCORD_ID
    assert reply.title == f"{NAME} {DEFENDER} was attacked"
    assert reply.body.splitlines() == [
        f"{NAME} {attacker}: {stars}★ · {destruction}% · −30 trophies",
        f"{NAME}'s hit rate this Season: 75%",
    ]
    assert "hit rate 75% (3 three-stars)" in stats.body
    assert not reply.private


def test_alerts_follow_the_switch_skip_stale_battles_and_record_refusals(
    database_url: str,
) -> None:
    """Battles saved more than 15 minutes ago, battles saved before the switch
    went on and players whose switch is off send nothing. A DM Discord refuses
    is logged once, shown on the website with its reason, and not retried."""
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
                with psycopg.connect(connection_info, autocommit=True) as connection:
                    seed_battles(connection, ids[tag], [(now, -16)])
                    connection.execute(
                        "UPDATE legend_battles SET created_at = clock_timestamp() - %s"
                        " WHERE id = (SELECT max(id) FROM legend_battles)",
                        (saved_ago,),
                    )

            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "UPDATE discord_attack_alerts SET enabled_at = enabled_at - interval '1 hour'"
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

            assert _switch(api, mine, "#2PP", False) == 200
            defend("#2PP")
            off = asyncio.run(loop.tick())
            with psycopg.connect(connection_info, autocommit=True) as connection:
                log = connection.execute(
                    "SELECT state, reason FROM discord_attack_alert_deliveries ORDER BY battle_id"
                ).fetchall()
        finally:
            api.close()
            owner.close()

    assert (quiet, refused, delivered, off) == (0, [0, 0], 1, 0)
    assert log == [("refused", "no_shared_server"), ("sent", None)]
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
    bot, clears that and confirms again."""
    with domain_database(database_url) as connection_info:
        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        try:
            with psycopg.connect(connection_info, autocommit=True) as connection:
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true)")
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

    assert (refused, still_on, reconnected, confirmed) == ([0, 0], 0, 200, [1, 0])
    assert shown is not None and (shown["tag"], shown["reason"]) == ("#2PP", "no_shared_server")
    assert cleared is None
    assert [reply.title for _, reply in discord.sent] == ["Attack alerts are on for Unnamed #2PP"]


def test_moving_a_player_away_forgets_its_alerts(database_url: str) -> None:
    """Moving the player to another account and back does not turn alerts on."""
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
            _verify(owner, theirs, "#2PP")
            _verify(owner, mine, "#2PP")
            players = api_discord_alerts.get_attack_alerts(api, mine)["players"]
        finally:
            api.close()
            owner.close()
    assert players == [{"tag": "#2PP", "name": None, "on": False}]
