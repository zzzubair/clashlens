"""Re-check the promotion list after each Monday Reset.

Legend II's top finishers move into Legend I at the Monday 05:00 UTC Reset.
From 05:30, once that Reset's collection has finished and no tracked player
is more than two minutes late, the collector asks for the profile of every
listed player (migration 0076) not checked since the Reset: Legend II first,
then Legend III, at most ``CLASHLENS_PROMOTION_RECHECK_PER_SECOND`` requests
a second (20 by default, 0 turns it off) on the regular keys. It pauses
whenever collection falls behind again. These answers are not saved: a
profile showing Legend I queues the ordinary discovery check, which saves the
profile and starts tracking; any other answer only refreshes the list row.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
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
IN_FLIGHT = 16
IDLE_SECONDS = 60.0
# A player whose profile keeps failing is left until next week.
MAX_FAILURES = 3
_WEEK = timedelta(days=7)
_WEEK_ANCHOR = datetime(2000, 1, 3, 5, tzinfo=UTC)  # a Monday Reset

# (tag, outcome, tier, trophies, answered at); outcome is promoted, listed,
# removed (not found, or a recognized tier outside Legend II and III) or
# checked (an answer the parser could not place).
Answer = tuple[str, str, int | None, int | None, datetime]


def week_start(now: datetime) -> datetime:
    """The latest Monday Reset at or before ``now``."""
    return _WEEK_ANCHOR + (now - _WEEK_ANCHOR) // _WEEK * _WEEK


def due_tags(database: CollectorDatabase, now: datetime, skip: list[str]) -> list[str] | None:
    """The next listed players to ask for, or None while collection is busy."""
    monday = week_start(now)
    with database._connection() as connection, connection.transaction():
        if now < monday + START_DELAY or not weekly_eligibility.has_time_to_spare(
            database, connection, now
        ):
            return None
        return [
            str(row[0])
            for row in connection.execute(
                """
                SELECT normalized_tag FROM promotion_candidates
                WHERE checked_at < %s AND NOT (normalized_tag = ANY(%s::text[]))
                ORDER BY league_tier_id DESC, checked_at, normalized_tag
                LIMIT %s
                """,
                (monday, skip, BATCH_SIZE),
            )
        ]


def record_answers(database: CollectorDatabase, answers: list[Answer]) -> int:
    """Save one batch's answers; returns how many discovery checks were queued.

    Promoted players share the discovery queue's limit of DISCOVERY_QUEUE_CAP
    waiting checks; one that does not fit keeps its old row, so it stays due
    and is asked again in a later batch.
    """
    queued = 0
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
            answers = [answer for answer in answers if answer[0] not in left_out]
            for tag in promoted:
                if tag not in left_out:
                    queued += int(
                        connection.execute(
                            "SELECT clashlens_queue_promoted_player(%s)", (tag,)
                        ).fetchone()[0]
                    )
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
    return queued


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
        return (tag, "checked", None, None, at)
    if profile.league_tier_id == LEGEND_I_TIER_ID:
        return (tag, "promoted", None, profile.trophies, at)
    if profile.league_tier_id in CANDIDATE_TIER_IDS:
        return (tag, "listed", profile.league_tier_id, profile.trophies, at)
    if profile.eligibility_state in {"eligible", "ineligible"}:
        return (tag, "removed", None, None, at)
    return (tag, "checked", None, None, at)


async def check_batch(
    collector: Collector, now: datetime, failures: Counter[str], rate: float
) -> Counter[str] | None:
    """Ask for one batch of listed players; None when nothing could be asked."""
    skip = [tag for tag, count in failures.items() if count >= MAX_FAILURES]
    tags = await collector._database_call(due_tags, collector.database, now, skip)
    if not tags:
        return None
    gate = asyncio.Semaphore(IN_FLIGHT)

    async def ask(tag: str) -> Answer | None:
        try:
            return await _ask(collector, tag)
        finally:
            gate.release()

    tasks = []
    next_start = time.monotonic()
    for tag in tags:
        await asyncio.sleep(max(0.0, next_start - time.monotonic()))
        await gate.acquire()
        next_start = max(next_start, time.monotonic()) + 1 / rate
        tasks.append(asyncio.create_task(ask(tag)))
    answers = await asyncio.gather(*tasks)
    found = [answer for answer in answers if answer is not None]
    for tag, answer in zip(tags, answers, strict=True):
        if answer is None:
            failures[tag] += 1
    queued = await collector._database_call(record_answers, collector.database, found)
    totals = Counter(answer[1] for answer in found)
    totals.update(failed=len(tags) - len(found), queued=queued)
    return totals


async def run(collector: Collector, stop_requested: asyncio.Event) -> None:
    """Re-check the list each Monday until stopped; never ends the collector early.

    Each stretch of work ends with one printed line counting its answers.
    """
    rate = float(os.environ.get("CLASHLENS_PROMOTION_RECHECK_PER_SECOND", "20"))
    if rate <= 0:
        await stop_requested.wait()
        return
    monday: datetime | None = None
    failures: Counter[str] = Counter()
    stretch: Counter[str] = Counter()
    while not stop_requested.is_set():
        now = datetime.now(UTC)
        if week_start(now) != monday:
            monday, failures = week_start(now), Counter()
        event = {"event": "promotion_recheck", "week_start": monday.isoformat()}
        try:
            batch = await check_batch(collector, now, failures, rate)
        except Exception as error:  # noqa: BLE001 - retried after a pause
            batch = None
            print(json.dumps({**event, "status": "failed", "error": repr(error)[:300]}), flush=True)
        if batch:
            stretch.update(batch)
            continue
        if stretch:
            print(json.dumps({**event, "status": "paused_or_done", **stretch}), flush=True)
            stretch = Counter()
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=IDLE_SECONDS)
        except TimeoutError:
            pass
