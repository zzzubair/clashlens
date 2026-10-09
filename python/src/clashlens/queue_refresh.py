"""New evidence for a player queues each ended day it can change, of the
current Season or, for a week after its end while it still takes
corrections, the Season before, keyed by the evidence and the day: a
pending repeat is merged, a finished one never holds it back. A recheck of
every active player's last two ended days twice a day catches anything
else."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from psycopg.types.json import Jsonb

from . import db, domain
from .first_battle_log import _queue
from .reconciliation import RECONCILIATION_RULE_VERSION
from .season_retirement import SEASON_CLOSE_WAIT

EVIDENCE_REFRESH_WINDOW = timedelta(days=7)
CHECK_INTERVAL_SECONDS = 600
_VERSIONS = {
    "parser": db.DEFAULT_PARSER_VERSION, "processing": db.PROCESSING_VERSION,
    "domain": db.DOMAIN_RULE_VERSION, "analytics": db.ANALYTICS_RULE_VERSION,
    "priority": db.PYTHON_BACKFILL_PRIORITY, "rule": RECONCILIATION_RULE_VERSION,
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


def _queue_day(connection: Any, player_id: int, day_start: datetime, key: str) -> None:
    _queue(connection, player_id, day_start, None, key=f"{key}:{_day_text(day_start)}",
           trigger="evidence", later_days=False, priority=db.PYTHON_BACKFILL_PRIORITY)


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
    day = _day_text(reset - timedelta(days=1))
    connection.execute(
        """
        INSERT INTO python_processing_jobs (observation_id, work_type, deduplication_key,
            input_json, parser_version, processing_version, domain_rule_version,
            analytics_rule_version, due_at, priority)
        SELECT NULL, 'reconcile_ranked_day', %(key)s, %(input)s, %(parser)s,
               %(processing)s, %(domain)s, %(analytics)s, %(at)s, %(priority)s
        WHERE EXISTS (SELECT 1 FROM collector_observations AS profile
            WHERE profile.player_id = %(player)s AND profile.endpoint = 'profile'
              AND profile.http_status BETWEEN 200 AND 299
              AND profile.response_completed_at <= %(at)s
              AND profile.response_completed_at > GREATEST((
                  SELECT last_success_at FROM collector_response_state
                  WHERE scope = %(scope)s AND identity_key = %(identity)s
                    AND endpoint = 'battle_log'), %(reset)s))
        ON CONFLICT DO NOTHING
        """,
        {**_VERSIONS, "at": check.response_completed_at, "player": check.player_id,
         "scope": check.scope, "identity": check.identity_key, "reset": reset,
         "key": f"reconcile:check:{check.player_id}:{day}:{check.response_completed_at}",
         "input": Jsonb({"player_id": check.player_id, "trigger": "battle_log_check",
                         "ranked_day_start": day})},
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


def queue_recheck(database: db.Database, slot: str, boundary: datetime) -> int:
    """Queue each active player's saved days ending at ``boundary`` and the
    Reset before, once per ``slot``, unless already waiting; count them."""
    with database.pool.connection() as connection, connection.transaction():
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
                WHERE job.work_type = 'reconcile_ranked_day'
                  AND job.state IN ('pending', 'waiting_retry')
                  AND job.input_json ->> 'player_id' = day.player_id::text
                  AND job.input_json ->> 'ranked_day_start' = day.day_text)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            {**_VERSIONS, "slot": slot, "last": boundary - timedelta(days=1),
             "before": boundary - timedelta(days=2)},
        ).rowcount


def due_recheck_slot(connection: Any, now: datetime) -> tuple[str, datetime] | None:
    """The recheck due at ``now`` and its Reset: the morning one once no
    day-end recheck waits, the night one from 23:00; none from 04:00 to 07:00."""
    boundary = domain.ranked_day_for(now).start
    if 4 <= now.astimezone(UTC).hour < 7:
        return None
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


class DailyRecheck:
    """Run the twice-daily recheck from the worker loop, logging its count."""

    def __init__(self, database: db.Database) -> None:
        self.database, self.next_check_at, self.done = database, float("-inf"), set()

    def run_when_due(self, now: datetime | None = None) -> None:
        if not isinstance(self.database, db.Database) or monotonic() < self.next_check_at:
            return
        self.next_check_at = monotonic() + CHECK_INTERVAL_SECONDS
        with self.database.pool.connection() as connection:
            due = due_recheck_slot(connection, now or datetime.now(UTC))
        if due is None or due[0] in self.done:
            return
        try:
            result = {"status": "queued", "days": queue_recheck(self.database, *due)}
            self.done.add(due[0])
        except Exception as error:  # noqa: BLE001 - retry at the next check
            result = {"status": "failed", "error": repr(error)[:300]}
        print(json.dumps({"event": "daily_recheck", "slot": due[0], **result}), flush=True)
