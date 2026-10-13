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
    and the settings button ends the last. Names are escaped."""
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
    parts = replies.day_summary(SITE, RESET, days)
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
    assert replies.day_summary(SITE, RESET, days[-1:]) == []


class _SummaryLog:
    NONCE_PREFIX = "summary-"

    def __init__(self) -> None:
        self.finished: list[tuple[int, str]] = []

    def claim(self, _delivery: Delivery) -> int:
        return 7

    def still_due(self, _delivery: Delivery) -> bool:
        return True

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


def test_a_split_summary_sends_its_dms_in_order_each_with_its_own_nonce(monkeypatch) -> None:
    """Each DM of a summary has its own nonce, which never repeats an attack
    alert's; one Discord did not take is tried again with the same nonce
    before the next is sent, and the summary is logged sent once all went."""
    monkeypatch.setattr("bot.alerts.RETRY_SECONDS", 0.0)
    log, discord = _SummaryLog(), _Discord()
    loop = AlertLoop(None, discord, SITE)  # type: ignore[arg-type]
    delivery = Delivery(1, 0, "1", None, RESET, Reply("one"), log=log,  # type: ignore[arg-type]
                        more=[Reply("two"), Reply("three")])

    async def scenario() -> list[int]:
        sent = [await loop._claim(delivery)]
        while loop._waiting:
            waiting = loop._waiting.pop()
            sent.append(await loop._send(waiting))
        return sent

    assert asyncio.run(scenario()) == [1, 2]
    assert discord.tries == [
        ("summary-7-0", "one"), ("summary-7-1", "two"),
        ("summary-7-1", "two"), ("summary-7-2", "three"),
    ]
    assert log.finished == [(7, "sent")]
