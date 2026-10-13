"""Legend day summaries end to end: saved game data through the real worker to
a published Daily board, then the bot's alert loop, run as the API's database
role, sending to a fake Discord. See docs/testing.md."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from domain_test_support import as_api_role, domain_database
from test_discord_alerts_postgres import SITE, FakeDiscord, _switch
from test_discord_bot_postgres import DISCORD_ID, _account, _verify
from test_reset_scenarios_postgres import SEASON, Player, Story, attacks, day, defenses

from bot.alerts import AlertLoop, AlertStore
from bot.store import Store
from bot.summaries import Summaries
from clashlens import api_discord_alerts
from clashlens.api_db import ApiDatabase, RequestBinding

FULL, LATE = "#2PP", "#8PP"
NAME = "Synthetic Legend I"


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


def _loop(api: ApiDatabase, discord: FakeDiscord, at: datetime) -> AlertLoop:
    """A freshly started bot's alert loop at `at`, as after a restart."""
    store = Store(api)
    return AlertLoop(
        AlertStore(api, store), discord, SITE,
        summaries=Summaries(api, store, SITE, now=lambda: at), now=lambda: at,
    )


def test_one_summary_a_day_from_the_published_board_for_each_player_left_on(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The Season that started on 5 October 2026. FULL was tracked from Day 1;
    Clash Lens first saw LATE at the Day 3 Reset, so LATE's Day 2 has no
    start reading and is Uncertain. Nothing is sent for the Day 3 Reset until
    its Daily board is published; then one DM covers both players' Day 2,
    with their places on that board, and a restarted bot sends it again to
    no one. LATE's summary then goes off: the Day 4 summary covers FULL alone,
    and FULL's attack alerts and summary stay on."""
    day_1, day_2, day_3, day_4 = day(1), day(2), day(3), day(4)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Player(tag) for tag in (FULL, LATE)})
        # FULL, Day 2: 3 attacks gain 100 and 8 defenses lose 110, 5,070 to
        # 5,060. Day 3: 8 attacks gain 320 and 8 defenses lose 240: 5,140.
        story.players[FULL].battles = (
            attacks(day_1, 40, 40, 40, 30) + defenses(day_1, *[10] * 8)
            + attacks(day_2, 40, 40, 20) + defenses(day_2, 40, *[10] * 7)
            + attacks(day_3, *[40] * 8) + defenses(day_3, *[30] * 8)
        )
        # LATE, Day 2: 2 attacks gain 80 and 8 defenses lose 160: 4,920.
        story.players[LATE].battles = attacks(day_2, 40, 40) + defenses(day_2, *[20] * 8)
        story.reset(day_1, {FULL: (5000, SEASON)})
        story.reads(day_1 + timedelta(minutes=20), {FULL: 5000})
        story.run_worker()
        story.reads(day_2 - timedelta(minutes=10), {FULL: 5070})
        story.reset(day_2, {FULL: (5070, SEASON)})
        story.reads(day_2 + timedelta(minutes=20), {FULL: 5070})
        story.run_worker()
        story.reads(day_3 - timedelta(minutes=10), {FULL: 5060})

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        discord = FakeDiscord()
        try:
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            _verify(owner, mine, FULL)
            assert _switch(api, mine, FULL, True) == 200
            # FULL's attack alerts stay on, confirmed, but no defense saved
            # here sends one: attack alerts have their own tests.
            with owner.pool.connection() as connection:
                connection.execute(
                    "UPDATE discord_attack_alerts SET enabled_at = enabled_at + interval '1 day',"
                    " confirm_requested_at = confirm_requested_at - interval '1 hour'"
                )
            after_reset = day_3 + timedelta(minutes=30)
            # The Day 3 Reset's readings are saved, but the worker has not
            # published its board yet.
            story.reset(day_3, {FULL: (5060, SEASON), LATE: (4920, SEASON)}, run=False)
            _verify(owner, mine, LATE)
            before_board = asyncio.run(_loop(api, discord, after_reset).tick())
            story.reads(day_3 + timedelta(minutes=20), {FULL: 5060, LATE: 4920})
            story.run_worker()
            story.wait(timedelta(hours=1))
            board = story.served_board(day_3)
            first = _loop(api, discord, after_reset)
            sent = [asyncio.run(first.tick()) for _ in range(2)]
            # A restarted bot looks again at once and finds it logged.
            restarted = asyncio.run(_loop(api, discord, after_reset).tick())
            day_3_dms = list(discord.sent)

            assert _summary_switch(api, mine, LATE, False) == 200
            settings = api_discord_alerts.get_attack_alerts(api, mine)
            story.reads(day_4 - timedelta(minutes=10), {FULL: 5140})
            story.reset(day_4, {FULL: (5140, SEASON), LATE: (4920, SEASON)})
            story.reads(day_4 + timedelta(minutes=20), {FULL: 5140, LATE: 4920})
            story.run_worker()
            story.wait(timedelta(hours=1))
            discord.sent.clear()
            day_4_sent = asyncio.run(_loop(api, discord, day_4 + timedelta(minutes=30)).tick())
            with owner.pool.connection() as connection:
                logged = connection.execute(
                    "SELECT reset_at, state FROM discord_day_summary_deliveries ORDER BY id"
                ).fetchall()
        finally:
            api.close()
            owner.close()

    places = {entry["tag"]: entry["position"] for entry in board["entries"]}
    assert (before_board, sent, restarted) == (0, [1, 0], 0)
    assert [(user, reply.title) for user, reply in day_3_dms] == [
        (DISCORD_ID, "Legend day summary · Tue 6 Oct"),
    ]
    link = "https://clashlens.test/players/%23{}?day=2026-10-06#legend-day-2026-10-06"
    # LATE's Day 2 has no start reading, so it has no trophy numbers and is
    # left off the board, as on the player page.
    assert places == {FULL: 1}
    assert day_3_dms[0][1].body.split("\n\n") == [
        "\n".join([
            f"**{NAME} {FULL}** · Verified",
            "5,070 → 5,060 (−10)",
            "Attacks 3 (+100) · Defenses 8 (−110)",
            f"Daily board place 1 · [Day on Clash Lens]({link.format('2PP')})",
        ]),
        "\n".join([
            f"**{NAME} {LATE}** · Uncertain",
            "Trophies unavailable",
            "Attacks 2 (+80) · Defenses 8 (−160)",
            ("Uncertain: some of this day's battles or readings may be missing, so these "
             "numbers may be off. The day on Clash Lens says why."),
            f"Daily board place Unavailable · [Day on Clash Lens]({link.format('8PP')})",
        ]),
        "Places are from the Daily board at the 05:00 UTC Reset.",
    ]
    assert [link.label for link in day_3_dms[0][1].links] == ["Summary settings"]

    assert [(player["tag"], player["on"], player["summary_on"])
            for player in settings["players"]] == [(FULL, True, True), (LATE, False, False)]
    assert day_4_sent == 1
    assert [reply.title for _, reply in discord.sent] == ["Legend day summary · Wed 7 Oct"]
    assert discord.sent[0][1].body.split("\n\n")[0] == "\n".join([
        f"**{NAME} {FULL}** · Verified",
        "5,060 → 5,140 (+80)",
        "Attacks 8 (+320) · Defenses 8 (−240)",
        ("Daily board place 1 · [Day on Clash Lens](https://clashlens.test/players/%232PP"
         "?day=2026-10-07#legend-day-2026-10-07)"),
    ])
    assert LATE not in discord.sent[0][1].body
    assert logged == [(day_3, "sent"), (day_4, "sent")]
