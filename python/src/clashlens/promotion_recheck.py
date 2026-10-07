"""Re-check the promotion list after each Monday Reset.

Legend II's top finishers move into Legend I at the Monday 05:00 UTC Reset.
From 05:30 the collector asks for the profile of every listed Legend II
player (migration 0076) not checked since the Reset, at most
``CLASHLENS_PROMOTION_RECHECK_PER_SECOND`` requests a second (20 by default,
0 turns it off) on the regular keys, at most two at once. Just before each
request, after its pacing wait, it is sent only while that Reset's collection
and settlement checks have finished, no tracked player is more than two
minutes late, and one key's worth of regular request slots is idle; otherwise
the player stays due. A request admitted just before a key wait or an API
outage can still start late, so at most two promotion requests ever start
together. Two in flight at about 120 ms each gives roughly 16 requests a
second, about an hour for 59,000 Legend II players. These answers are not
saved: a profile showing Legend I queues the ordinary discovery check, which
saves the profile and starts tracking; any other answer only refreshes the
list row. A request that fails, or an answer that cannot be read or shows an
uncertain tier, leaves the player due; it is asked again once the rest of the
list has been asked.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from . import weekly_eligibility
from .collector_db import CollectorDatabase
from .collector_http import ProviderFailure
from .db import DISCOVERY_QUEUE_CAP, lock_wait
from .profile import (
    LEGEND_I_TIER_ID,
    PROFILE_PARSER_VERSION,
    ProfileParseError,
    parse_profile,
)
from .promotion_candidates import CANDIDATE_TIER_IDS

if TYPE_CHECKING:
    from .collector import Collector

START_DELAY = timedelta(minutes=30)
BATCH_SIZE = 200
IN_FLIGHT = 2
IDLE_SECONDS = 60.0
# How long a reading of the database's spare-time checks is reused.
GUARD_SECONDS = 1.0
LEGEND_II_TIER_ID = 105000035
_WEEK = timedelta(days=7)
_WEEK_ANCHOR = datetime(2000, 1, 3, 5, tzinfo=UTC)  # a Monday Reset

# (tag, outcome, tier, trophies, answered at); outcome is promoted, listed or
# removed (not found, or a recognized tier outside Legend II and III).
Answer = tuple[str, str, int | None, int | None, datetime]


def week_start(now: datetime) -> datetime:
    """The latest Monday Reset at or before ``now``."""
    return _WEEK_ANCHOR + (now - _WEEK_ANCHOR) // _WEEK * _WEEK


def has_spare_time(database: CollectorDatabase, now: datetime) -> bool:
    """05:30 has passed, collection has time to spare and settlement checks are done."""
    if now < week_start(now) + START_DELAY:
        return False
    with database._connection() as connection, connection.transaction():
        return weekly_eligibility.has_time_to_spare(
            database, connection, now
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
    """The next listed Legend II players not checked since the Monday Reset."""
    with database._connection() as connection:
        return [
            str(row[0])
            for row in connection.execute(
                """
                SELECT normalized_tag FROM promotion_candidates
                WHERE league_tier_id = %s AND checked_at < %s
                  AND NOT (normalized_tag = ANY(%s::text[]))
                ORDER BY checked_at, normalized_tag
                LIMIT %s
                """,
                (LEGEND_II_TIER_ID, week_start(now), skip, BATCH_SIZE),
            )
        ]


def record_answers(database: CollectorDatabase, answers: list[Answer]) -> set[str]:
    """Save one batch's answers; returns the promoted players left due.

    A promoted player is handed on once tracked or given waiting work that
    still has to fetch the profile. Promoted players share the discovery queue's limit of
    DISCOVERY_QUEUE_CAP waiting checks; one that does not fit, or is not
    handed on, keeps its old row, so it stays due and is asked again later.
    """
    left_out: set[str] = set()
    promoted = [answer[0] for answer in answers if answer[1] == "promoted"]
    with database._connection() as connection, connection.transaction():
        if promoted:
            with lock_wait(connection, "1s"):
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('discovery-queue', 0))"
                )
            waiting = connection.execute(
                """
                SELECT count(*) FROM collector_work
                WHERE lane = 'ordinary' AND status IN ('pending', 'waiting_retry')
                  AND kind = 'discovery_profile' AND NOT eligibility_recheck
                """
            ).fetchone()[0]
            left_out = set(promoted[max(0, DISCOVERY_QUEUE_CAP - int(waiting)) :])
            for tag in promoted:
                if tag not in left_out and not connection.execute(
                    "SELECT clashlens_queue_promoted_player(%s)", (tag,)
                ).fetchone()[0]:
                    left_out.add(tag)
            answers = [answer for answer in answers if answer[0] not in left_out]
        kept = [answer for answer in answers if answer[1] != "removed"]
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
                [answer[0] for answer in kept],
                [answer[2] for answer in kept],
                [answer[3] for answer in kept],
                [answer[4] for answer in kept],
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
                [answer[0] for answer in answers if answer[1] == "removed"],
                [answer[4] for answer in answers if answer[1] == "removed"],
            ),
        )
    return left_out


async def _ask(collector: Collector, tag: str) -> Answer | None:
    try:
        response = await collector.client.fetch_player(collector.regular_keys, tag, "profile")
    except ProviderFailure:
        return None
    at = response.response_completed_at
    if response.http_status == 404:
        return (tag, "removed", None, None, at)
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
        return (tag, "promoted", None, profile.trophies, at)
    if profile.league_tier_id in CANDIDATE_TIER_IDS:
        return (tag, "listed", profile.league_tier_id, profile.trophies, at)
    return (tag, "removed", None, None, at)


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
        if self._collector._stopping.is_set():
            return False
        if time.monotonic() >= self._recheck_at:
            self._spare = await self._collector._database_call(
                has_spare_time, self._collector.database, self.clock()
            )
            self._recheck_at = time.monotonic() + GUARD_SECONDS
        return self._spare

    def _keys_idle(self) -> bool:
        """At least one key's worth of regular request slots is idle."""
        keys = self._collector.regular_keys
        now = time.monotonic()
        idle = sum(
            state.semaphore._value
            for state in keys._states
            if state.healthy and state.paused_until <= now
        )
        return idle >= keys.concurrency_per_key

    async def __call__(self) -> bool:
        """Wait for this request's start slot; False when it must not start now."""
        start = max(self._next_start, time.monotonic())
        self._next_start = start + self._interval
        await asyncio.sleep(start - time.monotonic())
        return await self.open() and self._keys_idle()


async def check_batch(
    collector: Collector, admit: Admission, attempted: set[str]
) -> Counter[str] | None:
    """Ask for one batch of listed players; None when nothing could be asked.

    Players asked this pass and left due are skipped until no other player is
    due; that ends the pass, so ``attempted`` is cleared and None returned, and
    the next pass asks them again.
    """
    if not await admit.open():
        return None
    now = admit.clock()
    tags = await collector._database_call(due_tags, collector.database, now, sorted(attempted))
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
    left_due = await collector._database_call(record_answers, collector.database, found)
    attempted.update(left_due)
    attempted.update(
        tag
        for tag, answer in zip(tags, answers, strict=True)
        if answer is None and tag not in refused
    )
    totals = Counter(answer[1] for answer in found)
    totals.update(
        asked=asked, failed=asked - len(found), queued=totals["promoted"] - len(left_due)
    )
    return totals


async def run(collector: Collector, stop_requested: asyncio.Event) -> None:
    """Re-check the list each Monday until stopped; never ends the collector early.

    A batch that was cut short or ended the list waits IDLE_SECONDS, so
    players left due are asked again at most once a minute. Each stretch of
    work ends with one printed line counting its answers.
    """
    rate = float(os.environ.get("CLASHLENS_PROMOTION_RECHECK_PER_SECOND", "20"))
    if rate <= 0:
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
