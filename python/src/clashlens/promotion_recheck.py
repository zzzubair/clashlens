"""Re-check the promotion list after each Monday Reset.

Legend II's top finishers move into Legend I at the Monday 05:00 UTC Reset,
and Legend III's into Legend II. After the Reset sweep (05:03:40-05:10),
settlement (from 05:20) and the late-battle check (from 05:30), from 06:00 the
collector asks for the profile of every listed Legend II player (migration
0076) not checked since 06:00, then every listed Legend III player, at
most ``CLASHLENS_PROMOTION_RECHECK_PER_SECOND`` requests a second (20 by
default, 0 turns it off) on the regular keys, at most two at once. Just before
each request, after its pacing wait, it is sent only while that Reset's
collection and settlement checks have finished, no tracked player is more than
two minutes late, and one key's worth of regular request slots is idle;
otherwise the player stays due. A request admitted just before a key wait or
an API outage can still start late, so at most two promotion requests ever
start together. Two in flight at about 120 ms each gives roughly 16 requests a
second, about an hour for 59,000 Legend II players and about three more hours
for 192,000 Legend III players. These answers are not saved: a profile showing
Legend I saves the player as due a discovery check newer than that answer
(migration 0084), which saves the profile and starts tracking; any other
answer only refreshes the list row, so a Legend III player promoted to Legend
II is asked with Legend II next Monday. A request that fails, or an answer that
cannot be read or shows an uncertain tier, leaves the player due; it is asked
again once the rest of its tier has been asked. Legend III players are asked
only once no Legend II player is due, so a Legend II player left due is asked
again before any Legend III player. A lower-league answer saved between the
Reset and 06:00, such as the weekly check's, may predate the game applying a
promotion, so it does not stand in for that Monday's request.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from . import weekly_eligibility
from .collector_db import CollectorDatabase
from .collector_http import ProviderFailure
from .profile import (
    LEGEND_I_TIER_ID,
    PROFILE_PARSER_VERSION,
    ProfileParseError,
    parse_profile,
)
from .promotion_candidates import CANDIDATE_TIER_IDS, CANDIDATE_TIERS_IN_ORDER

if TYPE_CHECKING:
    from .collector import Collector

START_DELAY = timedelta(hours=1)
BATCH_SIZE = 200
IN_FLIGHT = 2
IDLE_SECONDS = 60.0
GUARD_SECONDS = 1.0
_WEEK = timedelta(days=7)
_WEEK_ANCHOR = datetime(2000, 1, 3, 5, tzinfo=UTC)  # a Monday Reset


class Outcome(StrEnum):
    """What one usable answer says about a listed player. PROMOTED: Legend
    I. LISTED: Legend II or III, the tier it shows. REMOVED: not found, or a
    recognised tier outside Legend II and III. A failed request, or an
    answer that cannot be read or shows an uncertain tier, is no answer
    (``_ask`` gives None): the player stays due."""

    PROMOTED = "promoted"
    LISTED = "listed"
    REMOVED = "removed"


class Answer(NamedTuple):
    tag: str
    outcome: Outcome
    tier: int | None
    trophies: int | None
    answered_at: datetime


class Step(StrEnum):
    """What one answer does to its promotion list row, the only state a
    listed player has here.

    A row is due from 06:00 each Monday until a ``checked_at`` after then
    is saved (``due_tags``). REFRESH saves the answer's tier and trophies
    and the time, so the row is not due again until next Monday; a promoted
    player's row is kept this way once handed on to tracking or discovery.
    REMOVE deletes the row. LEAVE_DUE keeps the old row as it was: a
    promoted player whose hand-over another job's lock refused. A newer
    answer saved meanwhile by profile processing wins over either write. A
    player left due, or with no answer, is asked again in this run only
    once the rest of its tier has been (``check_batch``), and from the
    start after a restart, as nothing about a pass is saved.
    """

    REFRESH = "refresh"
    REMOVE = "remove"
    LEAVE_DUE = "leave_due"


def step(outcome: Outcome, handed: bool) -> Step:
    """The row's step for an answer; ``handed`` says a promoted player was
    handed on (``clashlens_queue_promoted_player``)."""
    if outcome is Outcome.REMOVED:
        return Step.REMOVE
    if outcome is Outcome.PROMOTED and not handed:
        return Step.LEAVE_DUE
    return Step.REFRESH


def week_start(now: datetime) -> datetime:
    """The latest Monday Reset at or before ``now``."""
    return _WEEK_ANCHOR + (now - _WEEK_ANCHOR) // _WEEK * _WEEK


def has_spare_time(database: CollectorDatabase, now: datetime) -> bool:
    """06:00 has passed, collection has time to spare and settlement checks are done."""
    if now < week_start(now) + START_DELAY:
        return False
    with database.connection() as connection, connection.transaction():
        return weekly_eligibility.has_time_to_spare(
            connection, now
        ) and not connection.execute(
            """
            SELECT 1 FROM collector_work
            WHERE kind = 'reset_settlement' AND status IN ('pending', 'waiting_retry')
              AND sweep_id = (
                  SELECT id FROM collector_reset_sweeps WHERE boundary_at <= %s
                  ORDER BY boundary_at DESC LIMIT 1
              )
            LIMIT 1
            """,
            (now,),
        ).fetchone()


def due_tags(database: CollectorDatabase, now: datetime, skip: list[str]) -> list[str]:
    """The next listed players not checked since 06:00 on Monday, outside ``skip``.

    Legend III players come only once no Legend II player is due; while only
    skipped Legend II players are due, nothing is returned.
    """
    with database.connection() as connection:
        for tier in CANDIDATE_TIERS_IN_ORDER:
            due = connection.execute(
                """
                SELECT normalized_tag, normalized_tag = ANY(%s::text[])
                FROM promotion_candidates
                WHERE league_tier_id = %s AND checked_at < %s
                ORDER BY normalized_tag = ANY(%s::text[]), checked_at, normalized_tag
                LIMIT %s
                """,
                (skip, tier, week_start(now) + START_DELAY, skip, BATCH_SIZE),
            ).fetchall()
            if due:
                return [str(tag) for tag, skipped in due if not skipped]
    return []


def record_answers(database: CollectorDatabase, answers: list[Answer]) -> set[str]:
    """Save one batch's answers; returns the promoted players left due.

    A promoted player is handed on once tracked or saved as due a discovery
    check newer than this answer; one another job holds is left out, keeps
    its old row, stays due and is asked again later.
    """
    with database.connection() as connection, connection.transaction():
        steps: list[tuple[Answer, Step]] = []
        for answer in answers:
            handed = answer.outcome is not Outcome.PROMOTED or bool(connection.execute(
                "SELECT handed FROM clashlens_queue_promoted_player(%s)", (answer.tag,)
            ).fetchone()[0])
            steps.append((answer, step(answer.outcome, handed)))
        kept = [answer for answer, next_step in steps if next_step is Step.REFRESH]
        removed = [answer for answer, next_step in steps if next_step is Step.REMOVE]
        # A newer answer saved by profile processing meanwhile is kept.
        connection.execute(
            """
            UPDATE promotion_candidates AS candidate
            SET league_tier_id = COALESCE(answer.tier, candidate.league_tier_id),
                trophies = COALESCE(answer.trophies, candidate.trophies),
                checked_at = answer.checked
            FROM unnest(%s::text[], %s::integer[], %s::integer[], %s::timestamptz[])
                AS answer(tag, tier, trophies, checked)
            WHERE candidate.normalized_tag = answer.tag
              AND candidate.checked_at < answer.checked
            """,
            (
                [answer.tag for answer in kept],
                [answer.tier for answer in kept],
                [answer.trophies for answer in kept],
                [answer.answered_at for answer in kept],
            ),
        )
        connection.execute(
            """
            DELETE FROM promotion_candidates AS candidate
            USING unnest(%s::text[], %s::timestamptz[]) AS answer(tag, checked)
            WHERE candidate.normalized_tag = answer.tag
              AND candidate.checked_at < answer.checked
            """,
            (
                [answer.tag for answer in removed],
                [answer.answered_at for answer in removed],
            ),
        )
    return {answer.tag for answer, next_step in steps if next_step is Step.LEAVE_DUE}


async def _ask(collector: Collector, tag: str) -> Answer | None:
    try:
        response = await collector.client.fetch_player(collector.regular_keys, tag, "profile")
    except ProviderFailure:
        return None
    at = response.response_completed_at
    if response.http_status == 404:
        return Answer(tag, Outcome.REMOVED, None, None, at)
    if response.http_status != 200:
        return None
    try:
        profile = parse_profile(
            response.body,
            expected_tag=tag,
            observed_at=at,
            endpoint_version="promotion-recheck",
            parser_version=PROFILE_PARSER_VERSION,
        )
    except ProfileParseError:
        return None
    if profile.eligibility_state not in {"eligible", "ineligible"}:
        return None
    if profile.league_tier_id == LEGEND_I_TIER_ID:
        return Answer(tag, Outcome.PROMOTED, None, profile.trophies, at)
    if profile.league_tier_id in CANDIDATE_TIER_IDS:
        trophies = profile.trophies if profile.trophies >= 0 else None
        return Answer(tag, Outcome.LISTED, profile.league_tier_id, trophies, at)
    return Answer(tag, Outcome.REMOVED, None, None, at)


class Admission:
    """Paces request starts and checks for spare headroom just before each one."""

    def __init__(
        self,
        collector: Collector,
        rate: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._collector = collector
        self.clock = clock
        self._interval = 1 / rate
        self._next_start = 0.0
        self._spare = False
        self._recheck_at = 0.0

    async def open(self) -> bool:
        """Whether collection has time to spare, read at most once a GUARD_SECONDS."""
        if self._collector.stopping.is_set():
            return False
        if time.monotonic() >= self._recheck_at:
            self._spare = await self._collector.database_call(
                has_spare_time, self._collector.database, self.clock()
            )
            self._recheck_at = time.monotonic() + GUARD_SECONDS
        return self._spare

    def _keys_idle(self) -> bool:
        """At least one key's worth of regular request slots is idle."""
        keys = self._collector.regular_keys
        return keys.free_ready_slots() >= keys.concurrency_per_key

    async def __call__(self) -> bool:
        """Wait for this request's start slot; False when it must not start now."""
        stopping = self._collector.stopping
        if stopping.is_set():
            return False
        start = max(self._next_start, time.monotonic())
        self._next_start = start + self._interval
        try:
            await asyncio.wait_for(stopping.wait(), timeout=start - time.monotonic())
        except TimeoutError:
            pass
        return await self.open() and self._keys_idle()


async def check_batch(
    collector: Collector, admit: Admission, attempted: set[str]
) -> Counter[str] | None:
    """Ask for one batch of listed players; None when nothing could be asked.

    Players asked this pass and left due are skipped until no other player of
    their tier is due; that ends the pass, so ``attempted`` is cleared and None
    returned, and the next pass asks them again.
    """
    if not await admit.open():
        return None
    now = admit.clock()
    tags = await collector.database_call(due_tags, collector.database, now, sorted(attempted))
    if not tags:
        attempted.clear()
        return None
    gate = asyncio.Semaphore(IN_FLIGHT)
    refused: set[str] = set()

    async def ask(tag: str) -> Answer | None:
        async with gate:
            if not await admit():
                refused.add(tag)
                return None
            return await _ask(collector, tag)

    tasks = [asyncio.create_task(ask(tag)) for tag in tags]
    try:
        answers = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    found = [answer for answer in answers if answer is not None]
    asked = len(tags) - len(refused)
    if not asked:
        return None
    left_due = await collector.database_call(record_answers, collector.database, found)
    attempted.update(left_due)
    attempted.update(
        tag
        for tag, answer in zip(tags, answers, strict=True)
        if answer is None and tag not in refused
    )
    totals = Counter(answer.outcome.value for answer in found)
    totals.update(
        asked=asked, failed=asked - len(found),
        queued=totals[Outcome.PROMOTED.value] - len(left_due),
    )
    return totals


async def run(collector: Collector, stop_requested: asyncio.Event) -> None:
    """Re-check the list each Monday until stopped; never ends the collector early.

    A batch that was cut short or ended the list waits IDLE_SECONDS, so
    players left due are asked again at most once a minute. Each stretch of
    work ends with one printed line counting its answers.
    """
    try:
        rate = float(os.environ.get("CLASHLENS_PROMOTION_RECHECK_PER_SECOND", "20"))
    except ValueError as error:
        rate = 0
        print(
            json.dumps({"event": "promotion_recheck", "status": "failed", "error": repr(error)[:300]}),
            flush=True,
        )
    if not 0 < rate < math.inf:
        await stop_requested.wait()
        return
    admit = Admission(collector, rate)
    stretch: Counter[str] = Counter()
    monday: datetime | None = None
    attempted: set[str] = set()
    while not stop_requested.is_set():
        if week_start(admit.clock()) != monday:
            monday = week_start(admit.clock())
            attempted.clear()
        event = {"event": "promotion_recheck", "week_start": monday.isoformat()}
        try:
            batch = await check_batch(collector, admit, attempted)
        except Exception as error:  # noqa: BLE001 - retried after a pause
            batch = None
            print(json.dumps({**event, "status": "failed", "error": repr(error)[:300]}), flush=True)
        if batch:
            stretch.update(batch)
            if batch["asked"] == BATCH_SIZE:
                continue
        if stretch:
            print(json.dumps({**event, "status": "paused_or_done", **stretch}), flush=True)
            stretch = Counter()
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=IDLE_SECONDS)
        except TimeoutError:
            pass
