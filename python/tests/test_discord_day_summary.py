"""Legend day summary wording and sending, from fixture data alone."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

from bot import replies
from bot.alerts import AlertLoop, Delivery, Unreachable
from bot.replies import Reply, Site

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
    body = replies.day_summary(SITE, RESET, [weekly, season_end])[0][0].body
    assert [block.split("\n")[1] for block in body.split("\n\n")[:2]] == [
        "5,440 → 5,320 (−120, −320 Season reset)",
        "5,000 → Unavailable (−20, +20 weekly reset)",
    ]


def test_a_long_summary_is_split_between_dms_and_cannot_ping_anyone() -> None:
    """40 players' days do not fit one Discord message: each goes in exactly
    one DM, in order, each DM under Discord's 4,096 characters, numbered,
    and the settings button ends the last; each DM carries the tags of the
    players it names. Names are escaped."""
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
    summary = replies.day_summary(SITE, RESET, days)
    parts = [part for part, _ in summary]
    assert len(parts) > 1
    assert [part.title for part in parts] == [
        f"Legend day summary · Tue 6 Oct ({n} of {len(parts)})"
        for n in range(1, len(parts) + 1)
    ]
    assert all(len(part.body) <= 4096 for part in parts)
    assert [len(part.links) for part in parts] == [0] * (len(parts) - 1) + [1]
    text = "\n\n".join(part.body for part in parts)
    assert [text.count(f"#{n:04d}**") for n in range(40)] == [1] * 40
    assert text.index("#0000**") < text.index("#0039**")
    assert "@everyone" not in text and "\\*\\*0\\*\\*" in text
    assert "Not finished yet: Slow #WAIT." in text
    assert [tags for _, tags in summary] == [
        tuple(day["tag"] for day in days if day["tag"] in part.body) for part in parts
    ]
    assert replies.day_summary(SITE, RESET, days[-1:]) == []


def test_a_long_not_finished_list_is_split_between_dms() -> None:
    """150 players whose day is not finished, with long names, are named at
    most 40 a line, so no DM goes over Discord's 4,096 characters."""
    yesterday = RESET - timedelta(days=1)
    waiting = [
        {"tag": f"#{n:07d}", "name": f"Still playing {n:06d}", "finished": False}
        for n in range(150)
    ]
    days = [replies.summary_day(_page("#2PP", _day(yesterday)), RESET, 1, False), *waiting]
    summary = replies.day_summary(SITE, RESET, days)
    assert all(len(part.body) <= 4096 for part, _ in summary)
    text = "\n\n".join(part.body for part, _ in summary)
    assert text.count("Not finished yet: ") == 4
    assert [text.count(day["tag"]) for day in waiting] == [1] * 150
    assert [tags for _, tags in summary] == [
        tuple(day["tag"] for day in days if day["tag"] in part.body) for part, _ in summary
    ]


class _NoAlerts:
    def prune(self) -> None:
        pass

    def confirmations_due(self) -> list:
        return []

    def due(self) -> list:
        return []


class _SummaryLog:
    """Each DM is due unless it names `gone`, a player since turned off."""

    NONCE_PREFIX = "summary-"

    def __init__(self, gone: str | None) -> None:
        self.gone = gone
        self.checked: list[tuple[str, ...]] = []
        self.finished: list[tuple[int, str]] = []

    def claim(self, _delivery: Delivery) -> int:
        return 7

    def still_due(self, delivery: Delivery) -> bool:
        self.checked.append(delivery.tags)
        return self.gone not in delivery.tags

    def finish(self, delivery_id: int, state: str, _reason: str | None) -> None:
        self.finished.append((delivery_id, state))


class _Discord:
    """Cannot be reached for the second DM's first try."""

    def __init__(self) -> None:
        self.tries: list[tuple[int | str, str]] = []

    async def send_dm(self, _user: str, reply: Reply, nonce: int | str, _left: float) -> None:
        self.tries.append((nonce, reply.body))
        if len(self.tries) == 2:
            raise Unreachable


def _send_summary(gone: str | None) -> tuple[list[int], _Discord, _SummaryLog]:
    """A summary of three DMs naming #A, #B and #C, sent by the alert loop,
    then round by round until none waits."""
    log, discord = _SummaryLog(gone), _Discord()
    loop = AlertLoop(_NoAlerts(), discord, SITE)  # type: ignore[arg-type]
    delivery = Delivery(1, 0, "1", None, RESET, Reply("one"), log=log,  # type: ignore[arg-type]
                        tags=("#A",), more=[(Reply("two"), ("#B",)), (Reply("three"), ("#C",))])

    async def scenario() -> list[int]:
        sent = [await loop._claim(delivery)]
        while loop._waiting:
            sent.append(await loop.tick())
        return sent

    return asyncio.run(scenario()), discord, log


def test_a_split_summary_sends_its_dms_in_order_each_with_its_own_nonce(monkeypatch) -> None:
    """Each DM of a summary has its own nonce, which never repeats an attack
    alert's; one Discord did not take is tried again with the same nonce
    before the next is sent, each is checked as still due for the players it
    names before it is tried, and the summary is logged sent once all went."""
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    sent, discord, log = _send_summary(None)
    assert sent == [1, 2]
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
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    sent, discord, log = _send_summary("#B")
    assert sent == [1]
    assert discord.tries == [("summary-7-0", "one")]
    assert log.finished == [(7, "failed")]


class _TwoSummaries:
    """Two Clashers' summaries due: the first's in two DMs, then the
    second's, each due while the loop asks."""

    NONCE_PREFIX = "summary-"

    def __init__(self) -> None:
        self.ids = iter((7, 8))
        self.finished: dict[int, str] = {}

    def due(self) -> list[Delivery]:
        return [
            Delivery(1, 0, "1", None, RESET, Reply("first, one"), log=self, tags=("#A",),
                     more=[(Reply("first, two"), ("#A",))]),
            Delivery(2, 0, "2", None, RESET, Reply("second"), log=self, tags=("#B",)),
        ]

    def prune(self) -> None:
        pass

    def claim(self, _delivery: Delivery) -> int:
        return next(self.ids)

    def still_due(self, _delivery: Delivery) -> bool:
        return True

    def finish(self, delivery_id: int, state: str, _reason: str | None) -> None:
        self.finished[delivery_id] = state


class _SlowDiscord:
    """Answers the second Clasher only after their DM's last moment."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_dm(self, user: str, reply: Reply, _nonce: int | str, _left: float) -> None:
        if user == "2":
            await asyncio.sleep(1)
        self.sent.append(reply.body)


def test_a_slow_dm_to_one_clasher_does_not_cost_another_their_next_dm(monkeypatch) -> None:
    """The first Clasher's first DM is delivered, then Discord takes longer
    than the second Clasher's DM may wait: the first Clasher's second DM went
    as soon as their first did, so only the second Clasher's is logged as not
    delivered."""
    monkeypatch.setattr("bot.alerts.NONCE_SECONDS", 0.2)
    summaries, discord = _TwoSummaries(), _SlowDiscord()
    loop = AlertLoop(_NoAlerts(), discord, SITE, summaries=summaries)  # type: ignore[arg-type]

    async def rounds() -> list[int]:
        return [await loop.tick(), await loop.tick()]

    assert asyncio.run(rounds()) == [2, 0]
    assert discord.sent == ["first, one", "first, two"]
    assert summaries.finished == {7: "sent", 8: "failed"}


def test_a_summary_dm_is_cut_off_at_the_next_reset() -> None:
    """A summary DM first tried 0.2 seconds before the next Reset has those
    0.2 seconds, not 2 minutes: Discord taking a second is too late, and
    the summary is logged as not delivered."""
    log, discord = _SummaryLog(None), _SlowDiscord()
    until = RESET + timedelta(days=1)
    loop = AlertLoop(_NoAlerts(), discord, SITE,  # type: ignore[arg-type]
                     now=lambda: until - timedelta(seconds=0.2))
    delivery = Delivery(2, 0, "2", None, RESET, Reply("late"), log=log,  # type: ignore[arg-type]
                        tags=("#A",), until=until)
    assert asyncio.run(loop._claim(delivery)) == 0
    assert discord.sent == []
    assert log.finished == [(7, "failed")]


class _DefenseSavedDuringSummaries:
    """Attack alerts with no database: one defense, saved as the summaries
    are read, its alert due for `window` seconds, as alerts are for 15
    minutes."""

    def __init__(self, window: float) -> None:
        self.window = window
        self.saved_at: float | None = None
        self.claimed = False

    def prune(self) -> None:
        pass

    def confirmations_due(self) -> list:
        return []

    def due(self) -> list[dict]:
        if self.saved_at is None or self.claimed:
            return []
        return [{"account_id": 1, "discord_user_id": "1", "battle_id": 3,
                 "defender_player_id": 2, "defender_tag": "#2PP", "defender_name": "Wanderer",
                 "attacker_tag": "#8QQ", "attacker_name": "Drift", "stars": 2,
                 "destruction_percentage": 87, "trophies_lost": 24, "saved_at": RESET,
                 "ranked_day_start": RESET}]

    def hit_rate(self, *_read: object) -> str:
        return "Unavailable"

    def claim(self, _delivery: Delivery) -> int | None:
        assert self.saved_at is not None
        self.claimed = time.monotonic() < self.saved_at + self.window
        return 3 if self.claimed else None

    def finish(self, *_outcome: object) -> None:
        pass


class _TenSummaries:
    """Ten Clashers' summaries, each due until it is logged."""

    NONCE_PREFIX = "summary-"

    def __init__(self, alerts: _DefenseSavedDuringSummaries) -> None:
        self.alerts = alerts
        self.unsent = list(range(10, 20))

    def due(self) -> list[Delivery]:
        if self.alerts.saved_at is None:
            self.alerts.saved_at = time.monotonic()
        return [Delivery(account, 0, str(account), None, RESET, Reply(f"summary {account}"),
                         log=self, tags=(f"#{account}",)) for account in self.unsent]

    def prune(self) -> None:
        pass

    def claim(self, delivery: Delivery) -> int:
        self.unsent.remove(delivery.account_id)
        return delivery.account_id

    def still_due(self, _delivery: Delivery) -> bool:
        return True

    def finish(self, *_outcome: object) -> None:
        pass


class _SlowSummaryDiscord:
    """Takes 0.1 seconds over each summary DM."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_dm(self, _user: str, reply: Reply, nonce: int | str, _left: float) -> None:
        if isinstance(nonce, str):
            await asyncio.sleep(0.1)
        self.sent.append(reply.title or reply.body)


def test_slow_summaries_do_not_hold_an_attack_alert_past_its_window(monkeypatch) -> None:
    """A defense is saved as 10 Clashers' summaries are read, each taking
    Discord 0.1 seconds, and its alert is due for 0.6 seconds, as alerts are
    for 15 minutes; sending all 10 first would take a second. The round
    starts no summary once 0.25 seconds have gone on them, so the next
    round sends the alert in time, and the rest of the summaries go in the
    rounds after it, each once."""
    monkeypatch.setattr("bot.alerts.SUMMARY_SECONDS", 0.0)
    monkeypatch.setattr("bot.alerts.SUMMARY_BUDGET_SECONDS", 0.25)
    alerts = _DefenseSavedDuringSummaries(window=0.6)
    summaries, discord = _TenSummaries(alerts), _SlowSummaryDiscord()
    loop = AlertLoop(alerts, discord, SITE, summaries=summaries)  # type: ignore[arg-type]

    async def rounds() -> None:
        while summaries.unsent:
            await loop.tick()

    asyncio.run(rounds())
    alert = "Wanderer #2PP was attacked"
    assert discord.sent.count(alert) == 1
    assert sorted(title for title in discord.sent if title != alert) == [
        f"summary {account}" for account in range(10, 20)
    ]
