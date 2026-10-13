"""Legend day summary wording and sending, from fixture data alone."""

from __future__ import annotations

import asyncio
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
        "ranked_day_start": start.isoformat(),
        "state": "Complete", "confidence": "exact", "coverage": "complete",
        "completeness": {"state": "complete"}, "uncertainty_reasons": [],
        "battles_complete": True, "adjustments": [],
        "offense_events": [{}] * 8, "defense_events": [{}] * 8,
        "start_trophies": 5000, "net_trophy_change": 80,
        "attack_count": 8, "attack_gain": 320, "defense_count": 8, "defense_loss": 240,
        "reset_rank": 1234,
        **fields,
    }


def _page(tag: str, *days: dict, name: str = "Wanderer") -> dict:
    return {"tag": tag, "name": name, "screen_ready": {"days": list(days)}}


def test_a_finished_day_reads_as_its_page_shows_it() -> None:
    """The day ended at RESET, labelled as the website labels it; the next
    day's start is the end of day, and a gap from the battles makes it
    Uncertain. A day not saved yet is not finished; a day the player had not
    signed up for is no Legend day."""
    yesterday = RESET - timedelta(days=1)
    verified = replies.summary_day(_page("#2PP", _day(yesterday)), RESET)
    assert verified is not None
    assert (verified["status"], verified["end"], verified["place"]) == ("Verified", 5080, 1234)
    calculated = replies.summary_day(_page("#2PP", _day(yesterday, confidence="inferred")), RESET)
    assert calculated is not None and calculated["status"] == "Calculated"
    gap = replies.summary_day(
        _page("#2PP", _day(yesterday), _day(RESET, start_trophies=5070)), RESET)
    assert gap is not None and gap["status"] == "Uncertain" and gap["end"] == 5070
    assert gap["conflict"] == "Battles add up to 5,080; next day started at 5,070."
    live = replies.summary_day(_page("#2PP", _day(yesterday, state="Live")), RESET)
    assert live == {"tag": "#2PP", "name": "Wanderer", "finished": False}
    unenrolled = _day(yesterday, uncertainty_reasons=["not_enrolled"],
                      offense_events=[], defense_events=[])
    assert replies.summary_day(_page("#2PP", unenrolled), RESET) is None
    assert replies.summary_day(_page("#2PP", _day(RESET)), RESET) is None


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
            RESET,
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
    days = [replies.summary_day(_page("#2PP", _day(yesterday)), RESET), *waiting]
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
    """A summary of three DMs naming #A, #B and #C, sent by the alert loop
    round by round until none waits."""
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
    assert sent == [1, 0, 1, 1]
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
    assert sent == [1, 0]
    assert discord.tries == [("summary-7-0", "one")]
    assert log.finished == [(7, "failed")]
