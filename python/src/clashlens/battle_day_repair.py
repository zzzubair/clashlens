"""Queue rebuilds of published Legend days whose battles moved day.

Migration 0057 moved each saved battle reported in the first
``domain.BATTLE_DAY_GRACE`` after a Reset to the previous Legend day and
listed each moved report in ``battle_day_repairs``. A day counts only its
player's own reports, so the player who made each moved report has their
published days rebuilt, from the earlier of the report's old and new day that
has a published result, then every later saved day of that Season, oldest
first. A player's rebuild is done once the latest published result of each
moved report's new day lists it and that of its old day no longer does.
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

    A player with reconciliation queued or running for one of those days
    waits for a later run. A player whose rebuild failed is not queued again
    but listed, at most ``max_jobs`` of them, in ``failed_blockers``.
    Returns the queued job ids in the republish command's report shape.
    """
    from .battle_ingestion import _refresh_battle_disagreements

    with database.pool.connection() as connection, connection.transaction():
        rows = connection.execute(
            """
            WITH own AS (
                SELECT CASE repair.perspective
                           WHEN 'attacker' THEN repair.attacker_player_id
                           ELSE repair.defender_player_id
                       END AS player_id,
                       repair.to_battle_id, repair.from_day, repair.to_day,
                       jsonb_build_array(jsonb_build_object(
                           'source_evidence_id', repair.evidence_id
                       )) AS listed
                FROM battle_day_repairs AS repair
            ), pending AS (
                SELECT own.player_id, own.to_battle_id, day.ranked_day_start
                FROM own
                LEFT JOIN LATERAL (
                    SELECT log.battles FROM api_player_daily_logs AS log
                    WHERE log.player_id = own.player_id
                      AND log.ranked_day_start = own.from_day
                    ORDER BY log.version DESC
                    LIMIT 1
                ) AS from_log ON true
                LEFT JOIN LATERAL (
                    SELECT log.battles FROM api_player_daily_logs AS log
                    WHERE log.player_id = own.player_id
                      AND log.ranked_day_start = own.to_day
                    ORDER BY log.version DESC
                    LIMIT 1
                ) AS to_log ON true
                CROSS JOIN LATERAL (
                    VALUES (own.from_day, from_log.battles),
                           (own.to_day, to_log.battles)
                ) AS day(ranked_day_start, battles)
                WHERE day.battles IS NOT NULL
                  AND (
                      (from_log.battles @> own.listed) IS TRUE
                      OR (to_log.battles @> own.listed) IS FALSE
                  )
            ), player AS (
                SELECT pending.player_id,
                       min(pending.ranked_day_start) AS first_day,
                       max(pending.ranked_day_start) AS last_day,
                       array_agg(DISTINCT pending.to_battle_id) AS battle_ids
                FROM pending
                GROUP BY pending.player_id
            ), candidates AS (
                SELECT player.*, job.id AS job_id, job.failure_category
                FROM player
                LEFT JOIN python_processing_jobs_worker AS job
                  ON job.deduplication_key = 'reconcile:battle-day:'
                     || player.player_id::text || ':'
                     || to_char(player.first_day AT TIME ZONE 'UTC',
                                'YYYY-MM-DD"T"HH24:MI:SS"Z"')
                WHERE (job.id IS NULL OR job.state = 'failed')
                  AND NOT EXISTS (
                      SELECT 1 FROM python_processing_jobs_worker AS active
                      WHERE active.work_type = 'reconcile_ranked_day'
                        AND active.state IN (
                            'pending', 'waiting_retry', 'waiting_dependency',
                            'leased'
                        )
                        AND (active.input_json ->> 'player_id')::bigint
                            = player.player_id
                        AND (active.input_json ->> 'ranked_day_start')
                            ::timestamptz BETWEEN player.first_day
                                              AND player.last_day
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
                   battle_ids, job_id, failure_category
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
        queued = [row for row in rows if row[5] is None]
        # A battle that gained its other side's report now compares the two.
        _refresh_battle_disagreements(
            connection, sorted({int(i) for row in queued for i in row[4]})
        )
        job_ids: list[int] = []
        for player_id, first_day, last_day, season_id, *_ in queued:
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
                    f"reconcile:battle-day:{int(player_id)}:{day_text}",
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
                "ranked_day_start": first_day.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "failure_category": (
                    _text_value(failure_category) if failure_category else None
                ),
            }
            for player_id, first_day, _, _, _, job_id, failure_category in rows
            if job_id is not None
        ],
    }
