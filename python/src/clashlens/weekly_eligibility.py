"""Paced Monday eligibility work, sharing the ordinary collector and key pool."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from . import collector_handover
from .collector_db import CollectorDatabase, CollectorIntent

if TYPE_CHECKING:
    from .collector import Collector

CHECK_INTERVAL_SECONDS = 2.0
SCHEDULE_INTERVAL_SECONDS = 60.0
LIVE_DELAY_LIMIT = timedelta(minutes=2)


class Eligibility(StrEnum):
    """Where an untracked player's eligibility answer stands this week (the
    week from Monday's Reset, or from ``eligibility_answer_after`` when a
    Monday re-check's unsaved Legend I answer raised it).

    NOT_DUE: no answer is needed (``players.eligibility_due_at`` empty).
    DUE: needed now, with no check: the next collector pass with room among
    the 500 waiting checks adds one, sets the next try 5 minutes on
    (doubling per attempt to 6 hours) and counts the attempt. CHECK_WAITING:
    a check waits to be collected. INTERPRETING: this week's profile was
    fetched and waits for the worker. ANSWERED: tracked, or a recognised
    league or not-found answer was saved this week: the next pass clears
    the due time. BACKING_OFF: due again at the saved next try, after a
    check failed or a profile showed no recognised league.

    Events: a battle log, ranking, import or Monday re-check naming the
    player saves them as due; each collector pass
    (``clashlens_admit_discovery_profiles``) clears, waits for or adds a
    check for each due player, in that order, and the weekly loop below also
    adds Monday checks for every untracked player. Owner: those database
    functions write it; ``eligibility`` reads it. Everything is saved, so a
    failed check, a crash or a restart leaves the player due and the next
    pass after the next try continues.
    """

    NOT_DUE = "not_due"
    DUE = "due"
    CHECK_WAITING = "check_waiting"
    INTERPRETING = "interpreting"
    ANSWERED = "answered"
    BACKING_OFF = "backing_off"


def eligibility(connection: Any, player_id: int, now: datetime) -> Eligibility:
    """The player's ``Eligibility`` at ``now``, decided as a pass would."""
    due_at, answered, waiting, interpreting = connection.execute(
        """
        SELECT player.eligibility_due_at,
               player.active OR clashlens_eligibility_answered_since(
                   player.id, week.boundary_at, %(now)s),
               EXISTS (
                   SELECT 1 FROM collector_work AS work
                   WHERE work.player_id = player.id
                     AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                     AND work.status IN ('pending', 'waiting_retry')),
               clashlens_eligibility_processing_since(player.id, week.boundary_at, %(now)s)
        FROM players AS player
        CROSS JOIN LATERAL (
            SELECT GREATEST(clashlens_eligibility_week(%(now)s), player.eligibility_answer_after)
                AS boundary_at
        ) AS week
        WHERE player.id = %(player)s
        """,
        {"now": now, "player": player_id},
    ).fetchone()
    if due_at is None:
        return Eligibility.NOT_DUE
    if answered:
        return Eligibility.ANSWERED
    if waiting:
        return Eligibility.CHECK_WAITING
    if interpreting:
        return Eligibility.INTERPRETING
    return Eligibility.DUE if due_at <= now else Eligibility.BACKING_OFF


def has_time_to_spare(connection: Any, now: datetime) -> bool:
    """Reset collection has finished and no tracked player is over two minutes late."""
    return collector_handover.admission_phase(connection, now) is collector_handover.AdmissionPhase.OPEN and not connection.execute(
        "SELECT 1 FROM players WHERE active AND next_due_at < %s LIMIT 1",
        (now - LIVE_DELAY_LIMIT,),
    ).fetchone()


def next_check(
    database: CollectorDatabase,
    now: datetime,
    *,
    schedule: bool,
    held: list[int] | None = None,
) -> CollectorIntent | None:
    """Admit at most one check while regular collection has time to spare."""
    with database.connection() as connection:
        with connection.transaction():
            if not has_time_to_spare(connection, now):
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
                          work.league_history_status <> 'not_applicable' AND NOT EXISTS (
                              SELECT 1 FROM collector_observations AS observation
                              WHERE observation.id = work.league_history_observation_id
                                AND (observation.http_status BETWEEN 200 AND 299
                                     OR observation.http_status = 404)),
                          NOT EXISTS (
                              SELECT 1 FROM collector_observations AS observation
                              WHERE observation.id = work.profile_observation_id
                                AND (observation.http_status BETWEEN 200 AND 299
                                     OR observation.http_status = 404))
                   FROM collector_work AS work
                   WHERE eligibility_recheck
                     AND status IN ('pending', 'waiting_retry') AND due_at <= %s
                     AND NOT (work.id = ANY(%s::bigint[]))
                   ORDER BY due_at, id LIMIT 1""",
                (now, held or []),
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
        intent = await collector.database_call(
            next_check,
            collector.database,
            datetime.now(UTC),
            schedule=schedule,
            held=collector.commits.held_work(),
        )
        if schedule:
            next_schedule = started + SCHEDULE_INTERVAL_SECONDS
        if intent is not None:
            await collector.intents.collect(intent)
        delay = max(0.0, CHECK_INTERVAL_SECONDS - (time.monotonic() - started))
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=delay)
        except TimeoutError:
            pass
