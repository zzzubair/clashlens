"""Queue rebuilds of published Legend days whose battles moved day.

Migration 0057 moved each saved battle reported in the first
``domain.BATTLE_DAY_GRACE`` after a Reset to the previous Legend day and
listed each moved report in ``battle_day_repairs``. Both players of each moved
battle have their published days rebuilt, from the earlier of the battle's old
and new day that has a published result, then every later saved day of that
Season, oldest first.
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
    """Queue at most ``max_jobs`` player rebuilds not queued before.

    Returns the queued job ids in the republish command's report shape.
    """
    from .battle_ingestion import _refresh_battle_disagreements

    with database.pool.connection() as connection, connection.transaction():
        rows = connection.execute(
            """
            WITH moved AS (
                SELECT side.player_id, repair.to_battle_id,
                       repair.from_day, repair.to_day
                FROM battle_day_repairs AS repair
                CROSS JOIN LATERAL (
                    VALUES (repair.attacker_player_id),
                           (repair.defender_player_id)
                ) AS side(player_id)
            ), first_day AS (
                SELECT moved.player_id,
                       min(log.ranked_day_start) AS ranked_day_start,
                       array_agg(DISTINCT moved.to_battle_id) AS battle_ids
                FROM moved
                JOIN api_player_daily_logs AS log
                  ON log.player_id = moved.player_id
                 AND log.ranked_day_start IN (moved.from_day, moved.to_day)
                GROUP BY moved.player_id
            )
            SELECT first_day.player_id, first_day.ranked_day_start,
                   (
                       SELECT log.official_season_id
                       FROM api_player_daily_logs AS log
                       WHERE log.player_id = first_day.player_id
                         AND log.ranked_day_start = first_day.ranked_day_start
                       ORDER BY log.version DESC
                       LIMIT 1
                   ),
                   first_day.battle_ids
            FROM first_day
            WHERE NOT EXISTS (
                SELECT 1 FROM python_processing_jobs_worker AS job
                WHERE job.deduplication_key = 'reconcile:battle-day:'
                    || first_day.player_id::text || ':'
                    || to_char(first_day.ranked_day_start AT TIME ZONE 'UTC',
                               'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            )
            ORDER BY first_day.player_id
            LIMIT %s
            """,
            (max_jobs,),
        ).fetchall()
        # A battle that gained its other side's report now compares the two.
        _refresh_battle_disagreements(
            connection, sorted({int(i) for row in rows for i in row[3]})
        )
        job_ids: list[int] = []
        for player_id, day_start, season_id, _battle_ids in rows:
            day_text = day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                            "last_ranked_day_start": day_text,
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
        "failed_blockers": [],
    }
