"""Paced Monday eligibility work, sharing the ordinary collector and key pool."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .collector_db import CollectorDatabase, CollectorIntent

if TYPE_CHECKING:
    from .collector import Collector

CHECK_INTERVAL_SECONDS = 2.0
SCHEDULE_INTERVAL_SECONDS = 60.0
LIVE_DELAY_LIMIT = timedelta(minutes=2)


def next_check(
    database: CollectorDatabase, now: datetime, *, schedule: bool
) -> CollectorIntent | None:
    """Admit at most one check while regular collection has time to spare."""
    with database._connection() as connection:
        with connection.transaction():
            if not database._regular_admission_open(connection, now):
                return None
            if connection.execute(
                """SELECT 1 FROM players WHERE active AND next_due_at < %s
                   LIMIT 1""",
                (now - LIVE_DELAY_LIMIT,),
            ).fetchone():
                return None
            if schedule:
                connection.execute(
                    "SELECT clashlens_enqueue_weekly_eligibility(%s)", (now,)
                )
            connection.execute(
                "SELECT clashlens_admit_discovery_profiles(%s)", (now,),
            )
            row = connection.execute(
                """SELECT work.id, work.player_id, work.normalized_tag, work.due_at,
                          work.league_history_status = 'pending',
                          NOT EXISTS (
                              SELECT 1 FROM collector_observations AS observation
                              WHERE observation.id = work.profile_observation_id
                                AND (observation.http_status BETWEEN 200 AND 299
                                     OR observation.http_status = 404))
                   FROM collector_work AS work
                   WHERE eligibility_recheck
                     AND status IN ('pending', 'waiting_retry') AND due_at <= %s
                   ORDER BY due_at, id LIMIT 1""",
                (now,),
            ).fetchone()
    if row is None:
        return None
    return CollectorIntent(
        "discovery_profile", row[3], int(row[1]), str(row[2]),
        work_id=int(row[0]), due_at=row[3], league_history_required=bool(row[4]),
        eligibility_recheck=True,
        profile_required=bool(row[5]),
    )


async def run(collector: Collector, stop_requested: asyncio.Event) -> None:
    """One in-flight check, at most 30 starts/minute, without catch-up bursts."""
    next_schedule = 0.0
    while not stop_requested.is_set():
        started = time.monotonic()
        schedule = started >= next_schedule
        intent = await collector._database_call(
            next_check, collector.database, datetime.now(UTC), schedule=schedule
        )
        if schedule:
            next_schedule = started + SCHEDULE_INTERVAL_SECONDS
        if intent is not None:
            await collector.collect_intent(intent)
        delay = max(0.0, CHECK_INTERVAL_SECONDS - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=delay)
        except TimeoutError:
            pass
