"""New evidence for a player queues a recheck of each ended day it can
change, of the current Season or, for a week after its end while it still
takes corrections, the Season before, in the day recheck lane
(``background_pacing``). A day usually waits in one recheck at most: new
evidence for a day whose recheck has not started adds nothing, as that
recheck reads the newest evidence when it runs; one already running may have
read too early, so the evidence queues one more. From 16:41 on 9 Oct 2026, 21,088
rechecks in 39 minutes were for 3,782 player days. A recheck of every active
player's last two ended days twice a day catches anything else, in the same
lane: about 25,000 at once at backfill priority held Season repair and the
Reset's day-end recalculations back for 6 to 7 hours a day (10 Oct 2026). The
lane takes the oldest first, so the recheck fills it only up to
``RECHECK_BATCH`` waiting, topped up each ``BATCH_INTERVAL_SECONDS``: all at
once, four at a time at the 60 to 70 a minute measured two at a time, a
recheck for evidence of any other day would wait up to about 3 hours."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from psycopg.types.json import Jsonb

from . import db, domain
from .background_pacing import DAY_RECHECK_PRIORITY
from .reconciliation import RECONCILIATION_RULE_VERSION
from .season_retirement import SEASON_CLOSE_WAIT

EVIDENCE_REFRESH_WINDOW = timedelta(days=7)
CHECK_INTERVAL_SECONDS = 600
RECHECK_BATCH = 500
BATCH_INTERVAL_SECONDS = 60
_VERSIONS = {
    "parser": db.DEFAULT_PARSER_VERSION, "processing": db.PROCESSING_VERSION,
    "domain": db.DOMAIN_RULE_VERSION, "analytics": db.ANALYTICS_RULE_VERSION,
    "rule": RECONCILIATION_RULE_VERSION,
}


def _seasons(at: datetime) -> dict[str, str | None]:
    """The Seasons whose days evidence seen at ``at`` may change: the current
    one, and the one before until ``SEASON_CLOSE_WAIT`` after it ended, when
    its corrections stop."""
    season_start = domain.ranked_day_for(at).season_start
    previous = domain.ranked_day_for(season_start - timedelta(days=1)).official_season_id
    return {"season": domain.ranked_day_for(at).official_season_id,
            "previous": previous if at < season_start + SEASON_CLOSE_WAIT else None}


def _day_text(day_start: datetime) -> str:
    return f"{day_start.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"


# The worker's database role reaches jobs through its view, the collector's
# only through the table, which names the state ``status``.
_WORKER_JOBS = ("python_processing_jobs_worker", "state")
_COLLECTOR_JOBS = ("python_processing_jobs", "status")


def _queue_day(connection: Any, player_id: int, day_start: datetime, key: str,
               trigger: str = "evidence", only_if: str = "TRUE",
               jobs: tuple[str, str] = _WORKER_JOBS, **params: Any) -> None:
    """Queue a recheck of ``day_start`` unless one has not started. That one
    stays locked until this transaction ends, so no worker starts it before
    this evidence is saved; one a worker is starting is skipped, so this
    evidence queues the next. Two saves committing at once can each queue
    one; both run."""
    day, (relation, state) = _day_text(day_start), jobs
    connection.execute(
        f"""
        WITH waiting AS MATERIALIZED (
            SELECT FROM {relation} AS job
            WHERE job.work_type = 'reconcile_ranked_day' AND job.{state} = 'pending'
              AND job.priority = {DAY_RECHECK_PRIORITY}
              AND job.input_json ->> 'player_id' = %(player)s
              AND job.input_json ->> 'ranked_day_start' = %(day)s
            LIMIT 1 FOR SHARE SKIP LOCKED)
        INSERT INTO {relation} (observation_id, work_type,
            deduplication_key, input_json, {state}, due_at, parser_version,
            processing_version, domain_rule_version, analytics_rule_version, priority)
        SELECT NULL, 'reconcile_ranked_day', %(key)s, %(input)s, 'pending',
               clock_timestamp(), %(parser)s, %(processing)s, %(domain)s, %(analytics)s,
               {DAY_RECHECK_PRIORITY}
        WHERE NOT EXISTS (SELECT FROM waiting) AND {only_if}
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        {**_VERSIONS, **params, "player": str(player_id), "day": day,
         "key": f"{key}:{day}", "input": Jsonb({
             "player_id": int(player_id), "ranked_day_start": day, "trigger": trigger})},
    )


def queue_for_reading(connection: Any, player_id: int, evidence: str, at: datetime) -> None:
    """A profile or battle log read at ``at`` can judge the newest ended day,
    and its own day once that has ended, when it is processed late."""
    rows = connection.execute(
        """
        (SELECT ranked_day_start FROM ranked_day_versions
         WHERE player_id = %(player)s AND reconciliation_rule_version = %(rule)s
           AND official_season_id IN (%(season)s, %(previous)s)
           AND ranked_day_end <= %(at)s AND ranked_day_end > %(at)s - %(window)s
         ORDER BY ranked_day_start DESC LIMIT 1)
        UNION
        SELECT ranked_day_start FROM ranked_day_versions
        WHERE player_id = %(player)s AND reconciliation_rule_version = %(rule)s
          AND official_season_id IN (%(season)s, %(previous)s)
          AND ranked_day_start <= %(at)s AND ranked_day_end > %(at)s
          AND ranked_day_end <= clock_timestamp()
        """,
        {**_VERSIONS, "player": player_id, "at": at, "window": EVIDENCE_REFRESH_WINDOW,
         **_seasons(at)},
    ).fetchall()
    for (day_start,) in rows:
        _queue_day(connection, player_id, day_start, f"reconcile:{evidence}:{player_id}")


def queue_for_check(connection: Any, check: Any) -> None:
    """A successful unchanged battle-log check (``collector_db``) saves no log
    yet covers each profile read since the last check and the latest Reset:
    queue the day that ended there, which the worker calculates if saved."""
    if check.player_id is None:
        return
    reset = domain.ranked_day_for(check.response_completed_at).start
    _queue_day(
        connection, check.player_id, reset - timedelta(days=1),
        f"reconcile:check:{check.player_id}:{check.response_completed_at}",
        "battle_log_check",
        jobs=_COLLECTOR_JOBS,
        only_if="""EXISTS (SELECT 1 FROM collector_observations AS profile
            WHERE profile.player_id = %(reader)s AND profile.endpoint = 'profile'
              AND profile.http_status BETWEEN 200 AND 299
              AND profile.response_completed_at <= %(at)s
              AND profile.response_completed_at > GREATEST((
                  SELECT last_success_at FROM collector_response_state
                  WHERE scope = %(scope)s AND identity_key = %(identity)s
                    AND endpoint = 'battle_log'), %(reset)s))""",
        at=check.response_completed_at, reader=check.player_id,
        scope=check.scope, identity=check.identity_key, reset=reset,
    )


def queue_for_battles(connection: Any, observation_id: int, corrected_ids: Iterable[int],
                      at: datetime) -> None:
    """Each battle whose report log ``observation_id`` added, or changed from
    that side's report before it, in time, length, stars, destruction or
    trophies, or whose agreement it corrected: recalculate its ended day and
    the day before, which reads its battles, for both players."""
    rows = connection.execute(
        """
        WITH changed AS (
            SELECT evidence.battle_id FROM battle_evidence AS evidence
            LEFT JOIN battle_source_rows AS source ON source.id = evidence.source_row_id
            LEFT JOIN LATERAL (
                SELECT earlier.battle_timestamp, earlier.stars, earlier.destruction_percentage,
                       earlier.attacker_gain, earlier.defender_loss,
                       earlier_source.source_json ->> 'battleTime' AS battle_time
                FROM battle_evidence AS earlier
                LEFT JOIN battle_source_rows AS earlier_source
                  ON earlier_source.id = earlier.source_row_id
                WHERE earlier.battle_id = evidence.battle_id
                  AND earlier.perspective = evidence.perspective
                  AND earlier.id < evidence.id
                ORDER BY earlier.id DESC LIMIT 1
            ) AS before ON true
            WHERE evidence.observation_id = %(observation)s AND evidence.battle_id IS NOT NULL
              AND (before.stars IS NULL
                   OR (before.battle_timestamp, before.stars, before.destruction_percentage,
                       before.attacker_gain, before.defender_loss)
                      <> (evidence.battle_timestamp, evidence.stars,
                          evidence.destruction_percentage, evidence.attacker_gain,
                          evidence.defender_loss)
                   OR before.battle_time IS DISTINCT FROM source.source_json ->> 'battleTime')
            UNION SELECT unnest(%(corrected)s::bigint[])
        )
        SELECT DISTINCT day.player_id, day.ranked_day_start FROM changed
        JOIN legend_battles AS battle ON battle.id = changed.battle_id
        JOIN ranked_day_versions AS day
          ON day.player_id IN (battle.attacker_player_id, battle.defender_player_id)
         AND day.ranked_day_start IN (battle.ranked_day_start,
                                      battle.ranked_day_start - interval '1 day')
        WHERE day.reconciliation_rule_version = %(rule)s
          AND day.official_season_id IN (%(season)s, %(previous)s)
          AND day.ranked_day_end <= %(at)s
        """,
        {**_VERSIONS, "observation": observation_id, "corrected": sorted(corrected_ids),
         "at": at, **_seasons(at)},
    ).fetchall()
    for player_id, day_start in rows:
        _queue_day(connection, int(player_id), day_start,
                   f"reconcile:report:{observation_id}:{player_id}")


def queue_recheck(database: db.Database, slot: str, boundary: datetime) -> tuple[int, int]:
    """Queue each active player's saved days ending at ``boundary`` and the
    Reset before, once per ``slot``, unless already waiting, while fewer than
    ``RECHECK_BATCH`` day rechecks wait; return how many were queued and how
    many could have been. Fewer queued than could have been ends the slot."""
    with database.pool.connection() as connection, connection.transaction():
        room = max(0, RECHECK_BATCH - connection.execute(
            f"""SELECT count(*) FROM (SELECT FROM python_processing_jobs_worker
                WHERE work_type = 'reconcile_ranked_day' AND state = 'pending'
                  AND priority = {DAY_RECHECK_PRIORITY} LIMIT %s) AS capped""",
            (RECHECK_BATCH,),
        ).fetchone()[0])
        if not room:
            return 0, 0
        return connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (observation_id, work_type,
                deduplication_key, input_json, state, due_at, parser_version,
                processing_version, domain_rule_version, analytics_rule_version, priority)
            SELECT NULL, 'reconcile_ranked_day',
                   concat_ws(':', 'reconcile:recheck', %(slot)s::text, player_id, day_text),
                   jsonb_build_object('player_id', player_id, 'trigger', 'recheck',
                                      'ranked_day_start', day_text),
                   'pending', clock_timestamp(), %(parser)s, %(processing)s,
                   %(domain)s, %(analytics)s, %(priority)s
            FROM (
                SELECT DISTINCT saved.player_id, to_char(
                    saved.ranked_day_start AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                ) AS day_text
                FROM ranked_day_versions AS saved
                JOIN players AS player ON player.id = saved.player_id AND player.active
                WHERE saved.ranked_day_start IN (%(last)s, %(before)s)
                  AND saved.reconciliation_rule_version = %(rule)s
            ) AS day
            WHERE NOT EXISTS (
                SELECT 1 FROM python_processing_jobs_worker AS job
                WHERE job.deduplication_key = concat_ws(
                    ':', 'reconcile:recheck', %(slot)s::text, day.player_id, day.day_text))
              AND NOT EXISTS (
                SELECT 1 FROM python_processing_jobs_worker AS job
                WHERE job.work_type = 'reconcile_ranked_day'
                  AND job.state IN ('pending', 'waiting_retry')
                  AND job.input_json ->> 'player_id' = day.player_id::text
                  AND job.input_json ->> 'ranked_day_start' = day.day_text)
            LIMIT %(room)s
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            {**_VERSIONS, "priority": DAY_RECHECK_PRIORITY, "slot": slot, "room": room,
             "last": boundary - timedelta(days=1),
             "before": boundary - timedelta(days=2)},
        ).rowcount, room


def due_recheck_slot(connection: Any, now: datetime) -> tuple[str, datetime] | None:
    """The recheck due at ``now`` and its Reset: the morning one once no
    day-end recheck waits, the night one from 23:00."""
    boundary = domain.ranked_day_for(now).start
    if now >= boundary.replace(hour=23):
        return f"{boundary:%Y-%m-%d}:night", boundary
    waiting = connection.execute(
        """SELECT EXISTS (SELECT 1 FROM python_processing_jobs_worker
            WHERE work_type = 'reconcile_ranked_day'
              AND deduplication_key LIKE 'reconcile:day-end:%%'
              AND input_json ->> 'ranked_day_start' = %s
              AND state IN ('pending', 'leased', 'waiting_retry'))""",
        (_day_text(boundary - timedelta(days=1)),),
    ).fetchone()[0]
    return None if waiting else (f"{boundary:%Y-%m-%d}:morning", boundary)


def previous_recheck_slot(now: datetime) -> tuple[str, datetime]:
    """The recheck before the one due at ``now``: the Reset's morning one
    from 23:00, until then the night one of the Reset before."""
    boundary = domain.ranked_day_for(now).start
    if now >= boundary.replace(hour=23):
        return f"{boundary:%Y-%m-%d}:morning", boundary
    before = boundary - timedelta(days=1)
    return f"{before:%Y-%m-%d}:night", before


class DailyRecheck:
    """Run the twice-daily recheck from the worker loop, a batch each
    ``BATCH_INTERVAL_SECONDS`` until a slot is queued, the oldest unfinished
    slot first, none from 04:00 to 07:00, logging its counts. The first
    check after the worker starts also finishes the slot before the due
    one, which a restart may have left unfinished."""

    def __init__(self, database: db.Database) -> None:
        self.database, self.next_check_at, self.done = database, float("-inf"), set()
        self.unfinished: list[tuple[str, datetime]] | None = None

    def run_when_due(self, now: datetime | None = None) -> None:
        if not isinstance(self.database, db.Database) or monotonic() < self.next_check_at:
            return
        self.next_check_at = monotonic() + CHECK_INTERVAL_SECONDS
        now = now or datetime.now(UTC)
        if 4 <= now.astimezone(UTC).hour < 7:
            return
        if self.unfinished is None:
            self.unfinished = [previous_recheck_slot(now)]
        with self.database.pool.connection() as connection:
            due = due_recheck_slot(connection, now)
        if due is not None and due[0] not in self.done and due not in self.unfinished:
            self.unfinished.append(due)
        if not self.unfinished:
            return
        slot = self.unfinished[0]
        try:
            queued, room = queue_recheck(self.database, *slot)
        except Exception as error:  # noqa: BLE001 - retry at the next check
            result = {"status": "failed", "error": repr(error)[:300]}
        else:
            if queued < room:
                self.done.add(slot[0])
                self.unfinished.remove(slot)
            if self.unfinished:
                self.next_check_at = monotonic() + BATCH_INTERVAL_SECONDS
            if not room:  # the lane is full
                return
            result = {"status": "queued" if queued < room else "queuing", "days": queued}
        print(json.dumps({"event": "daily_recheck", "slot": slot[0], **result}), flush=True)
