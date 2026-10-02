"""Recalculate saved Legend days that miss a battle saved after their Reset.

The Clash API can serve a battle log cached for about 60 seconds, so a battle
in the last minute before the 05:00 UTC Reset can first be saved after its
Legend day's result was published. Once per Reset, after the Reset sweep has
finished and every response fetched before it finished has been processed,
this finds each saved day whose result misses such a report, or still holds
the old agreement flag for its battle. It queues the existing ranked-day
recalculation for that day, then for each later saved day of that player one
at a time, each only once the day before it has finished. The live
recalculation and Reset paths are unchanged; the worker does the
recalculation and publication.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from time import monotonic

from psycopg.types.json import Jsonb

from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)
from .domain import ranked_day_for

# First check 30 minutes after the Reset, then every 10 minutes until every
# correction it started has finished.
SWEEP_DELAY = timedelta(minutes=30)
CHECK_INTERVAL_SECONDS = 600
# A report saved this close before its day ended may have missed the live
# recalculation queued with it, so it is checked too.
SAVE_MARGIN = timedelta(minutes=5)

_UNFINISHED = "('pending', 'leased', 'waiting_retry', 'waiting_dependency')"


def _day_text(day: str) -> str:
    return f"""to_char({day} AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')"""


def _job_key(player: str, day_text: str, mark: str) -> str:
    # A correction is the late report that started it (its mark) followed
    # through the player's saved days; one job key per day records progress.
    return f"'reconcile:late-battle:' || {player} || ':' || {day_text} || ':' || {mark}"


_FIRST_DAY_KEY = _job_key(
    "mark.player_id", _day_text("mark.ranked_day_start"), "mark.id"
)

# Every retained battle with a report saved late is checked against each
# reporting player's latest saved result: their own report and the battle's
# current agreement flag must both be listed. Days of a retired season cannot
# be recalculated and are skipped.
_OUTSTANDING_CORRECTIONS = f"""
WITH late_battle AS (
    SELECT battle.id, battle.ranked_day_start,
           battle.attacker_player_id, battle.defender_player_id,
           battle.disagreement_state = 'disagreement' AS disagreement,
           array_agg(perspective.evidence_id) AS late_ids
    FROM legend_battles AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
    JOIN battle_evidence AS evidence ON evidence.id = perspective.evidence_id
    WHERE battle.ranked_day_start < %(boundary)s
      AND evidence.created_at
          >= battle.ranked_day_start + interval '24 hours' - %(margin)s
    GROUP BY battle.id
), report AS (
    SELECT CASE perspective.perspective
               WHEN 'attacker' THEN battle.attacker_player_id
               ELSE battle.defender_player_id
           END AS player_id,
           battle.ranked_day_start, battle.late_ids,
           jsonb_build_object(
               'source_evidence_id', perspective.evidence_id,
               'disagreement', battle.disagreement
           ) AS expected
    FROM late_battle AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
), saved_day AS (
    SELECT pair.player_id, pair.ranked_day_start,
           NOT coalesce(
               version.contribution_evidence @> pair.expected, false
           ) AS stale
    FROM (
        SELECT player_id, ranked_day_start, jsonb_agg(expected) AS expected
        FROM report
        GROUP BY player_id, ranked_day_start
    ) AS pair
    CROSS JOIN LATERAL (
        SELECT log.ranked_day_version_id
        FROM api_player_daily_logs AS log
        WHERE log.player_id = pair.player_id
          AND log.ranked_day_start = pair.ranked_day_start
        ORDER BY log.version DESC
        LIMIT 1
    ) AS published
    LEFT JOIN ranked_day_versions AS version
      ON version.id = published.ranked_day_version_id
    WHERE NOT EXISTS (
        SELECT 1 FROM season_detail_retirements AS retirement
        WHERE retirement.status IN ('finalized', 'retired')
          AND (
              retirement.official_season_id = version.official_season_id
              OR (pair.ranked_day_start >= retirement.season_start
                  AND pair.ranked_day_start < retirement.season_end)
          )
    )
), mark AS (
    SELECT DISTINCT report.player_id, report.ranked_day_start, late.id,
           max(late.id) OVER (
               PARTITION BY report.player_id, report.ranked_day_start
           ) AS newest
    FROM report
    CROSS JOIN LATERAL unnest(report.late_ids) AS late(id)
), correction AS (
    SELECT mark.player_id, mark.ranked_day_start AS first_day, mark.id AS mark,
           coalesce(first_job.created_at, 'infinity') AS started_at
    FROM mark
    JOIN saved_day
      ON saved_day.player_id = mark.player_id
     AND saved_day.ranked_day_start = mark.ranked_day_start
    LEFT JOIN LATERAL (
        SELECT job.created_at
        FROM python_processing_jobs_worker AS job
        WHERE job.deduplication_key = {_FIRST_DAY_KEY}
        LIMIT 1
    ) AS first_job ON true
    WHERE first_job.created_at IS NOT NULL
       OR (saved_day.stale AND mark.id = mark.newest)
)
SELECT correction.player_id, correction.mark,
       coalesce(bool_or(job.state IN {_UNFINISHED}), false) AS waiting,
       (array_agg(saved_day_row.day_text ORDER BY saved_day_row.ranked_day_start)
            FILTER (WHERE job.id IS NULL))[1] AS next_day,
       (array_agg(saved_day_row.job_key ORDER BY saved_day_row.ranked_day_start)
            FILTER (WHERE job.id IS NULL))[1] AS next_key
FROM correction
CROSS JOIN LATERAL (
    SELECT saved.ranked_day_start, saved.day_text,
           {_job_key("correction.player_id", "saved.day_text", "correction.mark")}
               AS job_key
    FROM (
        SELECT DISTINCT log.ranked_day_start,
               {_day_text("log.ranked_day_start")} AS day_text
        FROM api_player_daily_logs AS log
        WHERE log.player_id = correction.player_id
          AND log.ranked_day_start >= correction.first_day
          AND log.ranked_day_start <= correction.started_at
    ) AS saved
) AS saved_day_row
LEFT JOIN LATERAL (
    SELECT job.id, job.state
    FROM python_processing_jobs_worker AS job
    WHERE job.deduplication_key = saved_day_row.job_key
    LIMIT 1
) AS job ON true
GROUP BY correction.player_id, correction.first_day, correction.mark
HAVING bool_or(job.id IS NULL OR job.state IN {_UNFINISHED})
ORDER BY correction.player_id, correction.first_day, correction.mark
"""


def sweep_late_battles(
    database: Database, *, now: datetime
) -> tuple[list[int], int] | None:
    """Queue the next recalculations, or None while the Reset is not ready.

    Returns the queued job ids and how many players still have a correction
    in progress.
    """

    boundary = ranked_day_for(now).start
    if now < boundary + SWEEP_DELAY:
        return None
    with database.pool.connection() as connection, connection.transaction():
        sweep = connection.execute(
            """
            SELECT GREATEST(sweep.created_at, max(work.updated_at)),
                   count(*) FILTER (WHERE work.status NOT IN (
                       'complete', 'failed', 'cancelled'
                   ))
            FROM collector_reset_sweeps AS sweep
            LEFT JOIN collector_work AS work
              ON work.sweep_id = sweep.id AND work.kind = 'reset_baseline'
            WHERE sweep.boundary_at = %s
            GROUP BY sweep.id
            """,
            (boundary,),
        ).fetchone()
        if sweep is None or sweep[1] > 0:
            return None
        responses_pending = connection.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM python_processing_jobs
                WHERE work_type IN ('process_observation', 'replay_observation')
                  AND status IN (
                      'pending', 'leased', 'waiting_retry', 'waiting_dependency'
                  )
                  AND created_at <= %s
            )
            """,
            (sweep[0],),
        ).fetchone()[0]
        if responses_pending:
            return None
        rows = connection.execute(
            _OUTSTANDING_CORRECTIONS,
            {"boundary": boundary, "margin": SAVE_MARGIN},
        ).fetchall()
        corrections: dict[int, list[tuple[int, bool, str, str]]] = {}
        for player_id, mark, waiting, next_day, next_key in rows:
            corrections.setdefault(int(player_id), []).append(
                (int(mark), bool(waiting), next_day, next_key)
            )
        job_ids: list[int] = []
        for player_id, player_corrections in corrections.items():
            # Each day's result reads the day before it, so a player has one
            # recalculation at a time, earliest day first.
            if any(waiting for _mark, waiting, _day, _key in player_corrections):
                continue
            mark, _waiting, day_text, job_key = player_corrections[0]
            row = connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key,
                    input_json, state, due_at, parser_version,
                    processing_version, domain_rule_version,
                    analytics_rule_version
                ) VALUES (
                    NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                    transaction_timestamp(), %s, %s, %s, %s
                )
                ON CONFLICT (deduplication_key) DO NOTHING
                RETURNING id
                """,
                (
                    job_key,
                    Jsonb(
                        {
                            "player_id": player_id,
                            "ranked_day_start": day_text,
                            "trigger": "late_battle_sweep",
                            "late_evidence_id": mark,
                        }
                    ),
                    DEFAULT_PARSER_VERSION,
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ANALYTICS_RULE_VERSION,
                ),
            ).fetchone()
            if row is not None:
                job_ids.append(int(row[0]))
        return job_ids, len(corrections)


class LateBattleSweep:
    """Run the sweep from the worker loop until each Reset's corrections finish."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.next_check_at = float("-inf")
        self.swept_boundary: datetime | None = None

    def run_when_due(self, now: datetime | None = None) -> None:
        if not isinstance(self.database, Database) or monotonic() < self.next_check_at:
            return
        self.next_check_at = monotonic() + CHECK_INTERVAL_SECONDS
        now = now or datetime.now(UTC)
        boundary = ranked_day_for(now).start
        if boundary == self.swept_boundary:
            return
        event = {"event": "late_battle_sweep", "boundary_at": boundary.isoformat()}
        try:
            result = sweep_late_battles(self.database, now=now)
        except Exception as error:  # noqa: BLE001 - retry at the next check
            print(
                json.dumps({**event, "status": "failed", "error": repr(error)[:300]}),
                flush=True,
            )
            return
        if result is None:
            return
        job_ids, players_in_progress = result
        if players_in_progress == 0:
            self.swept_boundary = boundary
        print(
            json.dumps(
                {
                    **event,
                    "status": "complete" if players_in_progress == 0 else "running",
                    "queued_jobs": len(job_ids),
                    "players_in_progress": players_in_progress,
                }
            ),
            flush=True,
        )
