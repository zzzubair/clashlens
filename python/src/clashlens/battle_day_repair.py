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

The ``republish-current-season`` command queues these rebuilds first, then
the other current-Season repairs and republications. With ``--campaign`` it
instead previews, registers or activates a Season's repair campaign.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from .db import DEFAULT_PARSER_VERSION, PYTHON_LIVE_PRIORITY, Database
from .db_rows import text_value
from .domain import SEASON_DURATION
from .job_contracts import (
    ANALYTICS_RULE_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
)


def merged_battles(
    connection: Any, battle_ids: list[int], evidence_ids: list[int]
) -> tuple[dict[tuple[int, str], int], list[int]]:
    """Map each listed (battle, lens) that 0057 moved to its new battle.

    A side with a listed report still saved on its listed battle stays there.

    A Reset's frozen inputs name the battle a report was saved under then;
    0057 may since have moved that report, its decode and facts to another
    battle row and deleted the old one. A Reset frozen after the move, from a
    day not yet rebuilt, names the old battle but no report for the moved
    side, as its row was gone. The second value lists the reports 0057 moved
    for sides with no listed report, to read in place of the missing ones.
    """
    if not battle_ids:
        return {}, []
    moved: dict[tuple[int, str], int] = {}
    unlisted: list[int] = []
    for from_id, perspective, to_id, report, listed in connection.execute(
        """
        SELECT repair.from_battle_id, repair.perspective, repair.to_battle_id,
               repair.evidence_id,
               EXISTS (
                   SELECT 1 FROM battle_evidence AS evidence
                   WHERE evidence.battle_id = repair.to_battle_id
                     AND evidence.perspective = repair.perspective
                     AND evidence.id = ANY(%s::bigint[])
               )
        FROM battle_day_repairs AS repair
        WHERE repair.from_battle_id = ANY(%s::bigint[])
          AND NOT EXISTS (
              SELECT 1 FROM battle_evidence AS evidence
              WHERE evidence.battle_id = repair.from_battle_id
                AND evidence.perspective = repair.perspective
                AND evidence.id = ANY(%s::bigint[])
          )
        """,
        (evidence_ids, battle_ids, evidence_ids),
    ).fetchall():
        lens = "offense" if text_value(perspective) == "attacker" else "defense"
        moved[(int(from_id), lens)] = int(to_id)
        if not listed:
            unlisted.append(int(report))
    return moved, unlisted


# Each published day a listed move is not yet reflected in: the moved
# report's old day still lists it, or its new day lacks the report now
# selected for that battle side.
UNFINISHED_MOVES = """
SELECT own.player_id, own.evidence_id, own.from_day, own.to_day,
       day.ranked_day_start, day.season_id
FROM (
    SELECT CASE repair.perspective
               WHEN 'attacker' THEN repair.attacker_player_id
               ELSE repair.defender_player_id
           END AS player_id,
           repair.evidence_id, repair.from_day, repair.to_day,
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
) AS own
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
    VALUES (own.from_day, from_log.battles, from_log.official_season_id),
           (own.to_day, to_log.battles, to_log.official_season_id)
) AS day(ranked_day_start, battles, season_id)
WHERE day.battles IS NOT NULL
  AND (
      (from_log.battles @> own.moved) IS TRUE
      OR (to_log.battles @> own.shown) IS FALSE
  )
"""


def enqueue_rebuilds(
    database: Database, *, max_jobs: int, season_id: str | None = None,
    queue: bool = True,
) -> dict[str, Any]:
    """Queue at most ``max_jobs`` rebuilds of players not yet done; with
    ``season_id``, as a Season repair, only of that Season's days, so no
    other Season changes, whatever Season the days were saved under, at
    backfill priority.

    A player with reconciliation queued or running that rebuilds one of those
    days waits for a later run. A player whose latest rebuild failed is not
    queued again but listed, at most ``max_jobs`` of them, in
    ``failed_blockers``, and every one in ``failed_job_ids``; deleting the
    failed job lets a later run queue it.
    Returns the queued job ids in the republish command's report shape;
    without ``queue``, writes nothing and lists only those blockers.
    """
    start = datetime.fromtimestamp(int(season_id), UTC) if season_id else None
    season = {"start": start, "end": start + SEASON_DURATION if start else None}
    with database.pool.connection() as connection, connection.transaction():
        if queue:
            # A battle that gained or lost a side's report compares them again.
            refresh_disagreements(
                connection,
                [
                    int(row[0])
                    for row in connection.execute(
                        """
                        SELECT DISTINCT battle_id FROM battle_day_repairs,
                            unnest(ARRAY[from_battle_id, to_battle_id]) AS battle_id
                        WHERE %(start)s::timestamptz IS NULL
                           OR from_day >= %(start)s AND from_day < %(end)s
                           OR to_day >= %(start)s AND to_day < %(end)s
                        ORDER BY battle_id
                        """,
                        season,
                    ).fetchall()
                ],
            )
        rows = connection.execute(
            f"""
            WITH pending AS (
                {UNFINISHED_MOVES}
            ), player AS (
                SELECT pending.player_id,
                       min(pending.ranked_day_start) AS first_day,
                       max(pending.ranked_day_start) AS last_day
                FROM pending
                WHERE %(start)s::timestamptz IS NULL
                   OR pending.ranked_day_start >= %(start)s
                  AND pending.ranked_day_start < %(end)s
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
            WHERE position <= %(max_jobs)s OR job_id IS NOT NULL
            ORDER BY player_id
            """,
            {**season, "max_jobs": max_jobs},
        ).fetchall()
        job_ids: list[int] = []
        for player_id, first_day, last_day, day_season, job_id, *_ in rows:
            if job_id is not None or not queue:
                continue
            day_text = first_day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            row = connection.execute(
                """
                INSERT INTO python_processing_jobs_worker (
                    observation_id, work_type, deduplication_key, input_json,
                    state, due_at, parser_version, processing_version,
                    domain_rule_version, analytics_rule_version, priority
                ) VALUES (
                    NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                    clock_timestamp(), %s, %s, %s, %s, %s
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
                            "recalculate_season": season_id or text_value(day_season),
                            "trigger": (
                                "season_repair" if season_id else "battle_day_repair"
                            ),
                        }
                    ),
                    DEFAULT_PARSER_VERSION,
                    PROCESSING_VERSION,
                    DOMAIN_RULE_VERSION,
                    ANALYTICS_RULE_VERSION,
                    PYTHON_BACKFILL_PRIORITY if season_id else PYTHON_LIVE_PRIORITY,
                ),
            ).fetchone()
            if row is not None:
                job_ids.append(int(row[0]))
    failed = [row for row in rows if row[4] is not None]
    return {
        "job_ids": job_ids,
        "evaluated_count": 0,
        "failure_reasons": {},
        "failed_job_ids": [int(row[4]) for row in failed],
        "failed_blockers": [
            {
                "job_id": int(job_id),
                "player_id": int(player_id),
                "ranked_day_start": job_day,
                "failure_category": (
                    text_value(failure_category) if failure_category else None
                ),
            }
            for player_id, _, _, _, job_id, failure_category, job_day
            in failed[:max_jobs]
        ],
    }


def refresh_disagreements(connection: Any, battle_ids: list[int]) -> None:
    if not battle_ids:
        return
    # Battles are already matched by Legend day, attacker and defender. Keep
    # each report's timestamp as evidence, but compare the army and result:
    # different reported times alone do not make this a different attack.
    connection.execute(
        """
        WITH target AS (
            SELECT unnest(%s::bigint[]) AS battle_id
        ), normalized AS (
            SELECT p.battle_id, p.perspective,
                   e.stars,
                   e.destruction_percentage, e.army_share_code,
                   CASE WHEN p.perspective = 'attacker'
                       THEN e.reporter_trophies ELSE e.opponent_trophies
                   END AS attacker_trophies,
                   CASE WHEN p.perspective = 'attacker'
                       THEN e.opponent_trophies ELSE e.reporter_trophies
                   END AS defender_trophies,
                   e.attacker_gain, e.defender_loss
            FROM battle_perspectives AS p
            JOIN battle_evidence AS e ON e.id = p.evidence_id
            JOIN target ON target.battle_id = p.battle_id
        ), paired AS (
            SELECT target.battle_id, count(normalized.battle_id) AS evidence_count,
                   max(stars) FILTER (WHERE perspective = 'attacker') AS a_stars,
                   max(stars) FILTER (WHERE perspective = 'defender') AS d_stars,
                   max(destruction_percentage) FILTER (WHERE perspective = 'attacker') AS a_destruction,
                   max(destruction_percentage) FILTER (WHERE perspective = 'defender') AS d_destruction,
                   max(army_share_code) FILTER (WHERE perspective = 'attacker') AS a_army,
                   max(army_share_code) FILTER (WHERE perspective = 'defender') AS d_army,
                   max(attacker_trophies) FILTER (WHERE perspective = 'attacker') AS a_attacker_trophies,
                   max(attacker_trophies) FILTER (WHERE perspective = 'defender') AS d_attacker_trophies,
                   max(defender_trophies) FILTER (WHERE perspective = 'attacker') AS a_defender_trophies,
                   max(defender_trophies) FILTER (WHERE perspective = 'defender') AS d_defender_trophies,
                   max(attacker_gain) FILTER (WHERE perspective = 'attacker') AS a_gain,
                   max(attacker_gain) FILTER (WHERE perspective = 'defender') AS d_gain,
                   max(defender_loss) FILTER (WHERE perspective = 'attacker') AS a_loss,
                   max(defender_loss) FILTER (WHERE perspective = 'defender') AS d_loss
            FROM target
            LEFT JOIN normalized ON normalized.battle_id = target.battle_id
            GROUP BY target.battle_id
        ), classified AS (
            SELECT battle_id, evidence_count,
                   array_remove(ARRAY[
                       CASE WHEN a_stars IS DISTINCT FROM d_stars THEN 'stars' END,
                       CASE WHEN a_destruction IS DISTINCT FROM d_destruction THEN 'destruction_percentage' END,
                       CASE WHEN a_army IS DISTINCT FROM d_army THEN 'army_share_code' END,
                       CASE WHEN a_attacker_trophies IS NOT NULL
                                  AND d_attacker_trophies IS NOT NULL
                                  AND a_attacker_trophies IS DISTINCT FROM d_attacker_trophies
                            THEN 'attacker_trophies' END,
                       CASE WHEN a_defender_trophies IS NOT NULL
                                  AND d_defender_trophies IS NOT NULL
                                  AND a_defender_trophies IS DISTINCT FROM d_defender_trophies
                            THEN 'defender_trophies' END,
                       CASE WHEN a_gain IS DISTINCT FROM d_gain THEN 'attacker_gain' END,
                       CASE WHEN a_loss IS DISTINCT FROM d_loss THEN 'defender_loss' END
                   ], NULL)::text[] AS fields
            FROM paired
        ), resolved AS (
            SELECT battle_id,
                   CASE
                       WHEN evidence_count < 2 THEN 'single_perspective'
                       WHEN cardinality(fields) = 0 THEN 'agreed'
                       ELSE 'disagreement'
                   END AS state,
                   CASE
                       WHEN evidence_count < 2 THEN ARRAY[]::text[]
                       ELSE fields
                   END AS fields
            FROM classified
        )
        UPDATE legend_battles AS battle
        SET disagreement_state = resolved.state,
            disagreement_fields = resolved.fields,
            updated_at = clock_timestamp()
        FROM resolved
        WHERE battle.id = resolved.battle_id
          -- A re-report that leaves the result unchanged writes nothing.
          AND (battle.disagreement_state, battle.disagreement_fields)
              IS DISTINCT FROM (resolved.state, resolved.fields)
        """,
        (battle_ids,),
    )
