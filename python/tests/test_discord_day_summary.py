"""Legend day summary wording and sending, from fixture data alone."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from bot import replies
from bot.alerts import AlertLoop, Unreachable
from bot.discord_app import DiscordMessenger, lay_out
from bot.replies import GREY, Reply, Site, say
from bot.summaries import Summary, SummaryLoop

SITE = Site("https://clashlens.test")
RESET = datetime(2026, 10, 7, 5, tzinfo=UTC)


def _day(start: datetime, **fields: object) -> dict:
    """A player page day as the private API sends it: a Complete, exact day
    with all 8 of each battle, 5,000 to 5,080."""
    return {
        "ranked_day_start": start.isoformat(), "version": 1,
        "state": "Complete", "confidence": "exact", "coverage": "complete",
        "completeness": {"state": "complete"}, "uncertainty_reasons": [],
        "battles_complete": True, "adjustments": [],
        "offense_events": [{}] * 8, "defense_events": [{}] * 8,
        "start_trophies": 5000, "net_trophy_change": 80,
        "attack_count": 8, "attack_gain": 320, "defense_count": 8, "defense_loss": 240,
        **fields,
    }


def _page(tag: str, *days: dict, name: str = "Wanderer") -> dict:
    return {"tag": tag, "name": name, "screen_ready": {"days": list(days)}}


def test_a_finished_day_reads_as_its_page_shows_it() -> None:
    """The day ended at RESET, labelled as the website labels it, with the
    place given from the Reset's Daily board, and its end of day left for
    the one saved with its version, never worked out from its start and
    change. A day not saved yet is not finished, nor is a player that board
    went without before their day saved; a day the player had not signed up
    for is no Legend day."""
    yesterday = RESET - timedelta(days=1)
    verified = replies.summary_day(_page("#2PP", _day(yesterday)), RESET, 1234, False)
    assert verified is not None
    assert (verified["status"], verified["version"], verified["end"], verified["place"]) == (
        "Verified", 1, None, 1234)
    calculated = replies.summary_day(
        _page("#2PP", _day(yesterday, confidence="inferred")), RESET, 1234, False)
    assert calculated is not None and calculated["status"] == "Calculated"
    live = replies.summary_day(_page("#2PP", _day(yesterday, state="Live")), RESET, None, False)
    assert live == {"tag": "#2PP", "name": "Wanderer", "finished": False}
    # A player Clash Lens knows with no saved day at all.
    unsaved = {"tag": "#2PP", "name": "Wanderer"}
    assert replies.summary_day(unsaved, RESET, None, True) == live
    assert replies.summary_day(unsaved, RESET, None, False) is None
    unenrolled = _day(yesterday, uncertainty_reasons=["not_enrolled"],
                      offense_events=[], defense_events=[])
    assert replies.summary_day(_page("#2PP", unenrolled), RESET, None, False) is None
    assert replies.summary_day(_page("#2PP", _day(RESET)), RESET, None, False) is None


def test_the_end_of_day_is_the_saved_one_before_any_weekly_or_season_reset() -> None:
    """A Season's last day from 5,440 loses 120, and the Season reset takes
    the 5,320 saved as its end of day to 5,000: the end shown is 5,320, as
    its Season page shows, with the reset beside the change, and the next
    day starting at 5,000 leaves it Verified. A day lost 20 before a Monday
    raise, and the worker saved no end of day for it: its end stays
    unavailable."""
    yesterday = RESET - timedelta(days=1)
    season_end = replies.summary_day(_page("#2PP", _day(
        yesterday, start_trophies=5440, net_trophy_change=-120,
        adjustments=[{"type": "season_reset", "amount": -320}],
    ), _day(RESET)), RESET, 3, False)
    weekly = replies.summary_day(_page("#8PP", _day(
        yesterday, net_trophy_change=-20, adjustments=[{"type": "weekly_reset", "amount": 20}],
    ), _day(RESET)), RESET, 4, False)
    assert season_end is not None and weekly is not None
    assert season_end["status"] == "Verified"
    season_end["end"] = 5320
    cards = replies.day_summary(SITE, RESET, [weekly, season_end], {})[0][0].cards
    assert [card.parts[0].split("\n")[1] for card in cards] == [
        "5,440 → 5,320 (−120, −320 Season reset)",
        "5,000 → Unavailable (−20, +20 weekly reset)",
    ]
    # Each card opens that player's day on Clash Lens.
    assert cards[0].button is not None and cards[0].button.url == (
        "https://clashlens.test/players/%232PP?day=2026-10-06#legend-day-2026-10-06"
    )


def test_a_long_summary_is_split_between_dms_and_cannot_ping_anyone() -> None:
    """40 players' days do not fit one Discord message: each goes in exactly
    one DM, in order, each DM under Discord's 4,000 characters, numbered,
    and the settings button ends the last; each DM carries the tags of the
    players it names. Names are escaped. Each player's card has its own
    colour, the not-finished one grey, and each DM keeps inside Discord's 40
    parts and 4,000 characters a message."""
    yesterday = RESET - timedelta(days=1)
    days = [
        replies.summary_day(
            _page(f"#{n:04d}", _day(yesterday, start_trophies=6000 - n),
                  name=f"@everyone **{n}**"),
            RESET, n + 1, False,
        )
        for n in range(40)
    ]
    days.append({"tag": "#WAIT", "name": "Slow", "finished": False})
    colours = replies.colours([day["tag"] for day in days])
    summary = replies.day_summary(SITE, RESET, days, colours)
    parts = [part for part, _ in summary]
    assert len(parts) > 1
    assert [part.title for part in parts] == [
        f"Legend day summary · Tue 6 Oct ({n} of {len(parts)})"
        for n in range(1, len(parts) + 1)
    ]
    assert [len(part.links) for part in parts] == [0] * (len(parts) - 1) + [1]
    for part in parts:
        view = discord.ui.LayoutView()
        lay_out(view, part)
        items = list(view.walk_children())
        assert len(items) <= 40
        shown = [item.content for item in items if isinstance(item, discord.ui.TextDisplay)]
        assert sum(map(len, shown)) <= 4000
    assert {
        re.search(r"#\d{4}", card.parts[0])[0]: card.colour
        for part in parts for card in part.cards if card.colour != GREY
    } == {day["tag"]: colours[day["tag"]] for day in days[:40]}
    text = "\n\n".join(part.text for part in parts)
    assert [text.count(f"** #{n:04d} ·") for n in range(40)] == [1] * 40
    assert text.index("#0000 ·") < text.index("#0039 ·")
    assert "@everyone" not in text and "\\*\\*0\\*\\*" in text
    assert "Not finished yet: Slow #WAIT." in text
    assert [tags for _, tags in summary] == [
        tuple(day["tag"] for day in days if day["tag"] in part.text) for part in parts
    ]
    assert replies.day_summary(SITE, RESET, days[-1:], colours) == []


def test_a_long_not_finished_list_is_split_between_dms() -> None:
    """150 players whose day is not finished, with long names, are named at
    most 40 a line, so no DM goes over Discord's 4,000 characters."""
    yesterday = RESET - timedelta(days=1)
    waiting = [
        {"tag": f"#{n:07d}", "name": f"Still playing {n:06d}", "finished": False}
        for n in range(150)
    ]
    days = [replies.summary_day(_page("#2PP", _day(yesterday)), RESET, 1, False), *waiting]
    summary = replies.day_summary(SITE, RESET, days, {})
    assert all(len(part.text) <= 4000 for part, _ in summary)
    text = "\n\n".join(part.text for part, _ in summary)
    assert text.count("Not finished yet: ") == 4
    assert [text.count(day["tag"]) for day in waiting] == [1] * 150
    assert [tags for _, tags in summary] == [
        tuple(day["tag"] for day in days if day["tag"] in part.text) for part, _ in summary
    ]


class _SummaryLog:
    """Summaries with no database, read at `now`: each logged as 7, and each
    DM due unless it names `gone`, a player since turned off."""

    def __init__(self, gone: str | None = None, now: datetime = RESET) -> None:
        self.gone = gone
        self.now = lambda: now
        self.checked: list[tuple[str, ...]] = []
        self.finished: list[tuple[int, str]] = []

    def claim(self, _summary: Summary) -> int | None:
        return 7

    def still_due(self, _summary: Summary, tags: tuple[str, ...]) -> bool:
        self.checked.append(tags)
        return self.gone not in tags

    def finish(self, summary_id: int, state: str, _reason: str | None) -> None:
        self.finished.append((summary_id, state))


def _three_dms() -> Summary:
    """A summary of three DMs naming #A, #B and #C."""
    return Summary(1, "1", RESET, [
        (say("one"), ("#A",)), (say("two"), ("#B",)), (say("three"), ("#C",)),
    ])


class _Discord:
    """Cannot be reached for the second DM's first try."""

    def __init__(self) -> None:
        self.tries: list[tuple[int | str, str]] = []

    async def send_dm(self, _user: str, reply: Reply, nonce: int | str, _left: float) -> None:
        self.tries.append((nonce, reply.text))
        if len(self.tries) == 2:
            raise Unreachable


def _send_summary(gone: str | None) -> tuple[int, _Discord, _SummaryLog]:
    log, discord = _SummaryLog(gone), _Discord()
    sent = asyncio.run(SummaryLoop(log, discord).send(_three_dms()))  # type: ignore[arg-type]
    return sent, discord, log


def test_a_split_summary_sends_its_dms_in_order_each_with_its_own_nonce(monkeypatch) -> None:
    """Each DM of a summary has its own nonce, which never repeats an attack
    alert's; one Discord did not take is tried again with the same nonce
    before the next is sent, each DM after the first is checked as still due
    for the players it names before it is tried and tried again, and the
    summary is logged sent once all went."""
    monkeypatch.setattr("bot.summaries.RETRY_SECONDS", 0.0)
    sent, discord, log = _send_summary(None)
    assert sent == 3
    assert discord.tries == [
        ("summary-7-0", "one"), ("summary-7-1", "two"),
        ("summary-7-1", "two"), ("summary-7-2", "three"),
    ]
    assert log.checked == [("#B",), ("#B",), ("#C",)]
    assert log.finished == [(7, "sent")]


def test_a_summary_dm_naming_a_player_turned_off_is_not_sent(monkeypatch) -> None:
    """#B's summary goes off after the first DM is delivered: the DM naming
    #B, and the one after it, are not sent, and the summary is logged as
    not delivered."""
    monkeypatch.setattr("bot.summaries.RETRY_SECONDS", 0.0)
    sent, discord, log = _send_summary("#B")
    assert sent == 1
    assert discord.tries == [("summary-7-0", "one")]
    assert log.finished == [(7, "failed")]


class _TwoSummaries(_SummaryLog):
    """Two Clashers' summaries due: the first's one DM, then the second's
    two."""

    def __init__(self) -> None:
        super().__init__()
        self.ids = iter((7, 8))

    def due(self) -> list[Summary]:
        return [
            Summary(2, "2", RESET, [(say("first"), ("#B",))]),
            Summary(1, "1", RESET, [
                (say("second, one"), ("#A",)), (say("second, two"), ("#A",)),
            ]),
        ]

    def prune(self) -> None:
        pass

    def claim(self, _summary: Summary) -> int | None:
        return next(self.ids)


class _SlowDiscord:
    """Takes a second over Clasher 2's DMs and 0.2 seconds over any other."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_dm(self, user: str, reply: Reply, _nonce: int | str, _left: float) -> None:
        await asyncio.sleep(1 if user == "2" else 0.2)
        self.sent.append(reply.text)


def test_each_summary_dm_has_its_own_time_from_its_own_first_try(monkeypatch) -> None:
    """Each DM may wait 0.3 seconds. Discord takes longer than that over the
    first Clasher's, which is logged as not delivered; it takes 0.2 seconds
    over each of the second Clasher's two, 0.4 together, and both are
    delivered in order, as each has its own time from its own first try."""
    monkeypatch.setattr("bot.summaries.NONCE_SECONDS", 0.3)
    log, discord = _TwoSummaries(), _SlowDiscord()
    assert asyncio.run(SummaryLoop(log, discord).tick()) == 2  # type: ignore[arg-type]
    assert discord.sent == ["second, one", "second, two"]
    assert log.finished == [(7, "failed"), (8, "sent")]


def test_a_summary_dm_is_cut_off_at_the_next_reset() -> None:
    """A summary DM first tried 0.2 seconds before the next Reset has those
    0.2 seconds, not 2 minutes: Discord taking a second is too late, and
    the summary is logged as not delivered."""
    log = _SummaryLog(now=RESET + timedelta(days=1) - timedelta(seconds=0.2))
    discord = _SlowDiscord()
    summary = Summary(2, "2", RESET, [(say("late"), ("#A",))])
    assert asyncio.run(SummaryLoop(log, discord).send(summary)) == 0  # type: ignore[arg-type]
    assert discord.sent == []
    assert log.finished == [(7, "failed")]


class _OneAttack:
    """Attack alerts with no database: one attack alert due until logged."""

    def __init__(self) -> None:
        self.logged = False

    def prune(self) -> None:
        pass

    def confirmations_due(self) -> list:
        return []

    def due(self) -> list[dict]:
        if self.logged:
            return []
        return [{"account_id": 1, "discord_user_id": "1", "battle_id": 3,
                 "defender_player_id": 2, "defender_tag": "#2PP", "defender_name": "Wanderer",
                 "attacker_tag": "#8QQ", "attacker_name": "Drift", "stars": 2,
                 "destruction_percentage": 87, "trophies_lost": 24, "saved_at": RESET,
                 "ranked_day_start": RESET, "colour": replies.PLAYER_COLOURS[0]}]

    def attacker(self, *_read: object) -> dict:
        return {"clan": None, "rank": None, "start_trophies": None, "hit_rate": "Unavailable"}

    def claim(self, _delivery: object) -> int:
        self.logged = True
        return 3

    def finish(self, *_outcome: object) -> None:
        pass


class _HeldDiscord:
    """Holds each summary DM until `release` is set, as if Discord took 100
    seconds over each; an attack alert DM goes at once."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.holding = 0
        self.sent: list[str] = []

    async def send_dm(self, _user: str, reply: Reply, nonce: int | str, _left: float) -> None:
        if isinstance(nonce, str):
            self.holding += 1
            await self.release.wait()
        self.sent.append(reply.text.splitlines()[0])


def test_slow_summary_dms_do_not_delay_an_attack_alert() -> None:
    """While a summary's first DM of three waits on Discord, the attack alert
    loop sends an alert due at once through the same messenger; the
    summary's DMs then arrive in order."""
    log, discord = _SummaryLog(), _HeldDiscord()
    alerts = AlertLoop(_OneAttack(), discord, SITE)  # type: ignore[arg-type]
    summaries = SummaryLoop(log, discord)  # type: ignore[arg-type]

    async def scenario() -> tuple[int, list[str], int]:
        sending = asyncio.create_task(summaries.send(_three_dms()))
        while not discord.holding:
            await asyncio.sleep(0)
        alert_sent = await asyncio.wait_for(alerts.tick(), 1)
        during = list(discord.sent)
        discord.release.set()
        return alert_sent, during, await sending

    alert = "🛡️ \u2068Wanderer\u2069  −24 🏆"
    assert asyncio.run(scenario()) == (1, [alert], 3)
    assert discord.sent == [alert, "one", "two", "three"]
    assert log.finished == [(7, "sent")]


class _HeldClient:
    """A Discord client whose DMs each wait for their own release."""

    def __init__(self) -> None:
        self.http = SimpleNamespace(max_ratelimit_timeout=None)
        self.releases: dict[int, asyncio.Event] = {}

    async def create_dm(self, user: SimpleNamespace) -> SimpleNamespace:
        release = self.releases[user.id] = asyncio.Event()

        async def send(**_message: object) -> None:
            await release.wait()

        return SimpleNamespace(send=send)


def test_dms_sent_at_once_leave_the_clients_rate_limit_wait_as_it_was() -> None:
    """An attack alert DM and a summary DM in flight together, each finishing
    in the order it started, leave the client's own rate limit wait as it
    was, not at either DM's time left, so command replies keep it."""
    client = _HeldClient()
    messenger = DiscordMessenger(client)  # type: ignore[arg-type]

    async def scenario() -> None:
        alert = asyncio.create_task(messenger.send_dm("1", say("alert"), 3, 60.0))
        summary = asyncio.create_task(
            messenger.send_dm("2", say("summary"), "summary-7-0", 120.0))
        while len(client.releases) < 2:
            await asyncio.sleep(0)
        client.releases[1].set()
        await alert
        client.releases[2].set()
        await summary

    asyncio.run(scenario())
    assert client.http.max_ratelimit_timeout is None
