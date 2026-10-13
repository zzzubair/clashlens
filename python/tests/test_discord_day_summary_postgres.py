"""Legend day summaries end to end: saved game data through the real worker to
a published Daily board, then the bot's summary loop, run as the API's
database role, sending to a fake Discord. See docs/testing.md."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psycopg
from domain_test_support import as_api_role, domain_database
from test_discord_alerts_postgres import SITE, FakeDiscord, _summary_switch, _switch
from test_discord_bot_postgres import DISCORD_ID, _account, _verify
from test_reset_scenarios_postgres import (
    PREVIOUS_SEASON,
    SEASON,
    Battle,
    Player,
    Story,
    _saturday,
    attacks,
    day,
    defenses,
)

from bot.replies import Reply
from bot.store import Store
from bot.summaries import Summaries, SummaryLoop
from clashlens import api_discord_alerts, reconciliation_db
from clashlens.api_db import ApiDatabase
from clashlens.db import Database

FULL, LATE = "#2PP", "#8PP"
NAME = "Synthetic Legend I"


def _loop(api: ApiDatabase, discord: FakeDiscord, at: datetime,
          store: Store | None = None) -> SummaryLoop:
    """A freshly started bot's summary loop at `at`, as after a restart."""
    return SummaryLoop(Summaries(api, store or Store(api), SITE, now=lambda: at), discord)


class _OffDuringOutage(FakeDiscord):
    """Cannot be reached for the first try, while `turn_off` runs."""

    def __init__(self, turn_off: Callable[[], object]) -> None:
        super().__init__()
        self.turn_off = turn_off
        self.outages = 1

    async def send_dm(
        self, discord_user_id: str, reply: Reply, nonce: int | str, time_left: float
    ) -> None:
        if self.outages:
            self.turn_off()
        await super().send_dm(discord_user_id, reply, nonce, time_left)  # type: ignore[arg-type]


def test_one_summary_a_day_from_the_published_board_for_each_player_turned_on(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The Season that started on 5 October 2026. FULL was tracked from Day 1;
    Clash Lens first saw LATE at the Day 3 Reset, so LATE's Day 2 has no
    start reading and is Uncertain. Both are verified with Discord connected
    and FULL's attack alerts on, but no summary is sent, nor logged, until
    their summaries are turned on. Nothing is sent for the Day 3 Reset until
    its Daily board is published; then one DM covers both players' Day 2,
    with their places on that board, and a restarted bot sends it again to
    no one. LATE's summary then goes off: the Day 4 summary covers FULL alone,
    and FULL's attack alerts and summary stay on. LATE's summary goes on
    again for Day 5, but Discord cannot be reached for the first try, and
    LATE's summary goes off before the try again: that DM is not sent."""
    monkeypatch.setattr("bot.summaries.RETRY_SECONDS", 0.0)
    day_1, day_2, day_3, day_4, day_5 = day(1), day(2), day(3), day(4), day(5)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Player(tag) for tag in (FULL, LATE)})
        # FULL, Day 2: 3 attacks gain 100 and 8 defenses lose 110, 5,070 to
        # 5,060. Day 3: 8 attacks gain 320 and 8 defenses lose 240: 5,140.
        # Day 4: an attack gains 40 and a defense loses 10: 5,170.
        story.players[FULL].battles = (
            attacks(day_1, 40, 40, 40, 30) + defenses(day_1, *[10] * 8)
            + attacks(day_2, 40, 40, 20) + defenses(day_2, 40, *[10] * 7)
            + attacks(day_3, *[40] * 8) + defenses(day_3, *[30] * 8)
            + attacks(day_4, 40) + defenses(day_4, 10)
        )
        # LATE, Day 2: 2 attacks gain 80 and 8 defenses lose 160: 4,920.
        # Day 4: an attack gains 40 and a defense loses 20: 4,940.
        story.players[LATE].battles = (
            attacks(day_2, 40, 40) + defenses(day_2, *[20] * 8)
            + attacks(day_4, 40) + defenses(day_4, 20)
        )
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
            unasked = asyncio.run(_loop(api, discord, after_reset).tick())
            for tag in (FULL, LATE):
                assert _summary_switch(api, mine, tag, True) == 200
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
            day_4_dms = list(discord.sent)

            assert _summary_switch(api, mine, LATE, True) == 200
            story.reads(day_5 - timedelta(minutes=10), {FULL: 5170, LATE: 4940})
            story.reset(day_5, {FULL: (5170, SEASON), LATE: (4940, SEASON)})
            story.reads(day_5 + timedelta(minutes=20), {FULL: 5170, LATE: 4940})
            story.run_worker()
            story.wait(timedelta(hours=1))
            fifth = _OffDuringOutage(lambda: _summary_switch(api, mine, LATE, False))
            day_5_sent = asyncio.run(_loop(api, fifth, day_5 + timedelta(minutes=30)).tick())
            with owner.pool.connection() as connection:
                logged = connection.execute(
                    "SELECT reset_at, state FROM discord_day_summary_deliveries ORDER BY id"
                ).fetchall()
        finally:
            api.close()
            owner.close()

    places = {entry["tag"]: entry["position"] for entry in board["entries"]}
    assert (before_board, unasked, sent, restarted) == (0, 0, [1, 0], 0)
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
    assert [reply.title for _, reply in day_4_dms] == ["Legend day summary · Wed 7 Oct"]
    assert day_4_dms[0][1].body.split("\n\n")[0] == "\n".join([
        f"**{NAME} {FULL}** · Verified",
        "5,060 → 5,140 (+80)",
        "Attacks 8 (+320) · Defenses 8 (−240)",
        ("Daily board place 1 · [Day on Clash Lens](https://clashlens.test/players/%232PP"
         "?day=2026-10-07#legend-day-2026-10-07)"),
    ])
    assert LATE not in day_4_dms[0][1].body

    assert (day_5_sent, len(fifth.tries), fifth.sent) == (0, 1, [])
    assert logged == [(day_3, "sent"), (day_4, "sent"), (day_5, "failed")]


class _CorrectedAfterFirstRead(Store):
    """The bot's player page reads; `correct` runs once, right after the first."""

    def __init__(self, api: ApiDatabase, correct: Callable[[], None]) -> None:
        super().__init__(api)
        self.correct: Callable[[], None] | None = correct

    def player_page(self, tag: str, now: datetime) -> dict[str, Any] | None:
        page = super().player_page(tag, now)
        if self.correct is not None:
            correct, self.correct = self.correct, None
            correct()
        return page


WAITING, RAISED, KNOWN_START = "#2PP", "#8PP", "#PPP"


def test_a_monday_summary_from_one_board_while_it_is_corrected(
    database_url: str, archive_server, monkeypatch
) -> None:
    """The previous Season's Monday Reset, 28 September 2026, as its days
    must have ended by the real clock. RAISED's Sunday ends at 4,940 and the
    Monday raise takes it to 5,000. WAITING's Sunday cannot be calculated,
    so the Monday board ranks it by its last reading, 4,950, above RAISED,
    though a defense after that reading ended its day at 4,910. Clash Lens
    first saw KNOWN_START at the Sunday Reset, at 5,400, but that Reset's
    battle log never came, so nothing proves the day's battles from its
    start: its 8 attacks and 8 defenses give its change, +80, and the worker
    saves no end of day, which the summary shows as unavailable, not 5,480.
    It names WAITING as not finished; right after WAITING's page is read the
    fix lands, WAITING's day saves and the board is corrected with WAITING
    below RAISED, and RAISED keeps its place from the board the round began
    with."""
    season = PREVIOUS_SEASON
    saturday, sunday, monday = day(20, season), day(21, season), day(22, season)
    calculate, broken = reconciliation_db.recalculate_ranked_day, [0]

    def faulty(database: Database, connection: Any, *, player_id: int,
               day_start: datetime, **versions: str) -> bool:
        assert (player_id, day_start) != (broken[0], sunday), "a calculation fault"
        return calculate(database, connection, player_id=player_id, day_start=day_start,
                         **versions)

    monkeypatch.setattr(reconciliation_db, "recalculate_ranked_day", faulty)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        story = Story(connection_info, archive_server, monkeypatch,
                      {tag: Player(tag) for tag in (WAITING, RAISED, KNOWN_START)})
        # From 5,300 each Saturday ends at 5,140. RAISED's Sunday: an attack
        # gains 40 and 8 defenses lose 240, 4,940. WAITING's: an attack gains
        # 40 and 7 defenses lose 230, 4,950 at its last reading, then a
        # defense 5 minutes before the Reset loses 40, 4,910. KNOWN_START's:
        # 8 attacks gain 320 and 8 defenses lose 240, +80, to 5,480.
        story.players[RAISED].battles = (
            _saturday(saturday) + attacks(sunday, 40) + defenses(sunday, *[30] * 8))
        story.players[WAITING].battles = (
            _saturday(saturday) + attacks(sunday, 40) + defenses(sunday, *[40] * 3, *[30] * 3, 20)
            + [Battle(monday - timedelta(minutes=5), -40, False)])
        story.players[KNOWN_START].battles = (
            attacks(sunday, *[40] * 8) + defenses(sunday, *[30] * 8))
        story.reset(saturday, {WAITING: (5300, season), RAISED: (5300, season)})
        with psycopg.connect(connection_info) as connection:
            broken[0] = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (WAITING,)).fetchone()[0]
        story.reset(sunday, {
            WAITING: (5140, season), RAISED: (5140, season), KNOWN_START: (5400, season),
        }, no_log=(KNOWN_START,))
        story.reads(monday - timedelta(minutes=10), {WAITING: 4950, RAISED: 4940}, season)
        story.reset(monday, {
            WAITING: (5000, season), RAISED: (5000, season), KNOWN_START: (5480, season)})
        story.reads(monday + timedelta(minutes=20), {WAITING: 5000, RAISED: 5000}, season)
        story.run_worker()
        story.wait(timedelta(hours=1))
        known = story.saved_day(KNOWN_START, sunday)
        members = story.board_members(monday)
        board = story.served_board(monday)
        corrected: list[dict[str, Any] | None] = []

        def correct() -> None:
            broken[0] = 0
            database = Database(connection_info)
            try:
                reconciliation_db.enqueue_reconciliation(
                    database, player_tag=WAITING, day_start=sunday, now=monday,
                    request_key="after-the-fix",
                )
            finally:
                database.close()
            story.run_worker()
            corrected.append(story.served_board(monday))

        owner = ApiDatabase(connection_info)
        api = ApiDatabase(as_api_role(connection_info))
        discord = FakeDiscord()
        try:
            mine = _account(owner, "discord", DISCORD_ID, "drift")
            for tag in (WAITING, RAISED, KNOWN_START):
                _verify(owner, mine, tag)
                assert _summary_switch(api, mine, tag, True) == 200
            at = monday + timedelta(hours=2)
            sent = asyncio.run(
                _loop(api, discord, at, _CorrectedAfterFirstRead(api, correct)).tick())
        finally:
            api.close()
            owner.close()

    assert known is not None and (known["start"], known["end"]) == (5400, None)
    assert members[WAITING] == "failed"
    assert board is not None and corrected and corrected[0] is not None
    before = {entry["tag"]: entry["position"] for entry in board["entries"]}
    after = {entry["tag"]: entry["position"] for entry in corrected[0]["entries"]}
    assert before[RAISED] == before[WAITING] + 1 and after[WAITING] == after[RAISED] + 1
    assert sent == 1
    assert [(user, reply.title) for user, reply in discord.sent] == [
        (DISCORD_ID, "Legend day summary · Sun 27 Sep"),
    ]
    link = "https://clashlens.test/players/%23{}?day=2026-09-27#legend-day-2026-09-27"
    place = {tag: before.get(tag, "Unavailable") for tag in (RAISED, KNOWN_START)}
    blocks = discord.sent[0][1].body.split("\n\n")
    assert [
        (lines[0].split(" · ")[0], lines[1], lines[-1])
        for lines in (block.split("\n") for block in blocks[:2])
    ] == [
        (f"**{NAME} {RAISED}**", "5,140 → 4,940 (−200, +60 weekly reset)",
         f"Daily board place {place[RAISED]} · [Day on Clash Lens]({link.format('8PP')})"),
        (f"**{NAME} {KNOWN_START}**", "5,400 → Unavailable (+80)",
         f"Daily board place {place[KNOWN_START]} · [Day on Clash Lens]({link.format('PPP')})"),
    ]
    assert blocks[2:] == [
        f"Not finished yet: {NAME} {WAITING}. Their day shows on Clash Lens once it is saved.",
        "Places are from the Daily board at the 05:00 UTC Reset.",
    ]
