"""Queue rebuilds of published Legend days whose battles moved day.

Migration 0057 moved each saved battle reported in the first
``domain.BATTLE_DAY_GRACE`` after a Reset to the previous Legend day and
listed each moved report in ``battle_day_repairs``. A day counts only its
player's own reports, so the player who made each moved report has their
published days rebuilt, from the earlier of the report's old and new day that
has a published result, then every later saved day of that Season, oldest
first. A player's rebuild is done once the latest published result of each
moved report's new day lists the report now selected for that battle side,
the moved one or a later replacement, and that of its old day no longer lists
the moved one. Every battle that gained or lost a side compares its two
reports again on each run, whoever is rebuilt.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from psycopg.types.json import Jsonb

from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
    _text_value,
)


def enqueue_rebuilds(database: Database, *, max_jobs: int) -> dict[str, Any]:
    """Queue at most ``max_jobs`` rebuilds of players not yet done.

    A player with reconciliation queued or running that rebuilds one of those
    days waits for a later run. A player whose latest rebuild failed is not
    queued again but listed, at most ``max_jobs`` of them, in
    ``failed_blockers``; deleting the failed job lets a later run queue it.
    Returns the queued job ids in the republish command's report shape.
    """
    from .battle_ingestion import _refresh_battle_disagreements

    with database.pool.connection() as connection, connection.transaction():
        # A battle that gained or lost a side's report compares them again.
        _refresh_battle_disagreements(
            connection,
            [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT battle_id FROM battle_day_repairs,
                        unnest(ARRAY[from_battle_id, to_battle_id]) AS battle_id
                    ORDER BY battle_id
                    """
                ).fetchall()
            ],
        )
        rows = connection.execute(
            """
            WITH own AS (
                SELECT CASE repair.perspective
                           WHEN 'attacker' THEN repair.attacker_player_id
                           ELSE repair.defender_player_id
                       END AS player_id,
                       repair.from_day, repair.to_day,
                       jsonb_build_array(jsonb_build_object(
                           'source_evidence_id', repair.evidence_id
                       )) AS moved,
                       jsonb_build_array(jsonb_build_object(
                           'source_evidence_id', coalesce(
                               selected.evidence_id, repair.evidence_id
                           )
                       )) AS shown
                FROM battle_day_repairs AS repair
                LEFT JOIN battle_perspectives AS selected
                  ON selected.battle_id = repair.to_battle_id
                 AND selected.perspective = repair.perspective
            ), pending AS (
                SELECT own.player_id, day.ranked_day_start, day.season_id
                FROM own
                LEFT JOIN LATERAL (
                    SELECT log.battles, log.official_season_id
                    FROM api_player_daily_logs AS log
                    WHERE log.player_id = own.player_id
                      AND log.ranked_day_start = own.from_day
                    ORDER BY log.version DESC
                    LIMIT 1
                ) AS from_log ON true
                LEFT JOIN LATERAL (
                    SELECT log.battles, log.official_season_id
                    FROM api_player_daily_logs AS log
                    WHERE log.player_id = own.player_id
                      AND log.ranked_day_start = own.to_day
                    ORDER BY log.version DESC
                    LIMIT 1
                ) AS to_log ON true
                CROSS JOIN LATERAL (
                    VALUES (own.from_day, from_log.battles,
                            from_log.official_season_id),
                           (own.to_day, to_log.battles,
                            to_log.official_season_id)
                ) AS day(ranked_day_start, battles, season_id)
                WHERE day.battles IS NOT NULL
                  AND (
                      (from_log.battles @> own.moved) IS TRUE
                      OR (to_log.battles @> own.shown) IS FALSE
                  )
            ), player AS (
                SELECT pending.player_id,
                       min(pending.ranked_day_start) AS first_day,
                       max(pending.ranked_day_start) AS last_day
                FROM pending
                GROUP BY pending.player_id
            ), candidates AS (
                SELECT player.*, job.id AS job_id, job.failure_category,
                       job.input_json ->> 'ranked_day_start' AS job_day
                FROM player
                LEFT JOIN python_processing_jobs_worker AS job
                  ON job.deduplication_key
                     = 'reconcile:battle-day:' || player.player_id::text
                WHERE (job.id IS NULL OR job.state = 'failed')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pending
                      JOIN python_processing_jobs_worker AS active
                        ON (active.input_json ->> 'player_id')::bigint
                           = pending.player_id
                      CROSS JOIN LATERAL (
                          SELECT (active.input_json ->> 'ranked_day_start')
                                     ::timestamptz AS first_day,
                                 active.input_json ->> 'recalculate_season'
                                     AS season_id
                      ) AS rebuild
                      WHERE pending.player_id = player.player_id
                        AND active.work_type = 'reconcile_ranked_day'
                        AND active.state IN (
                            'pending', 'waiting_retry', 'waiting_dependency',
                            'leased'
                        )
                        AND (
                            pending.ranked_day_start = rebuild.first_day
                            OR active.input_json ? 'recalculate_season' AND (
                                pending.ranked_day_start = (
                                    active.input_json ->> 'last_ranked_day_start'
                                )::timestamptz
                                OR pending.ranked_day_start >= rebuild.first_day
                                AND pending.season_id = rebuild.season_id
                            )
                        )
                  )
            )
            SELECT player_id, first_day, last_day,
                   (
                       SELECT log.official_season_id
                       FROM api_player_daily_logs AS log
                       WHERE log.player_id = ranked.player_id
                         AND log.ranked_day_start = ranked.first_day
                       ORDER BY log.version DESC
                       LIMIT 1
                   ),
                   job_id, failure_category, job_day
            FROM (
                SELECT candidates.*,
                       row_number() OVER (
                           PARTITION BY job_id IS NULL ORDER BY player_id
                       ) AS position
                FROM candidates
            ) AS ranked
            WHERE position <= %s
            ORDER BY player_id
            """,
            (max_jobs,),
        ).fetchall()
        job_ids: list[int] = []
        for player_id, first_day, last_day, season_id, job_id, *_ in rows:
            if job_id is not None:
                continue
            day_text = first_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key, input_json,
                    state, due_at, parser_version, processing_version,
                    domain_rule_version, analytics_rule_version
                ) VALUES (
                    NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                    clock_timestamp(), %s, %s, %s, %s
                )
                ON CONFLICT (deduplication_key) DO NOTHING
                RETURNING id
                """,
                (
                    f"reconcile:battle-day:{int(player_id)}",
                    Jsonb(
                        {
                            "player_id": int(player_id),
                            "ranked_day_start": day_text,
                            "last_ranked_day_start": last_day.astimezone(
                                UTC
                            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "recalculate_season": _text_value(season_id),
                            "trigger": "battle_day_repair",
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
    return {
        "job_ids": job_ids,
        "evaluated_count": 0,
        "failure_reasons": {},
        "failed_blockers": [
            {
                "job_id": int(job_id),
                "player_id": int(player_id),
                "ranked_day_start": job_day,
                "failure_category": (
                    _text_value(failure_category) if failure_category else None
                ),
            }
            for player_id, _, _, _, job_id, failure_category, job_day in rows
            if job_id is not None
        ],
    }
