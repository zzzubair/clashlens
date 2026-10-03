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

import argparse
import json
from collections.abc import Callable
from datetime import UTC
from typing import Any

from psycopg.types.json import Jsonb

from . import domain_repair, reset_baselines
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
    _text_value,
)
from .domain import SEASON_ANCHOR_RULE_VERSION
from .reconciliation import RECONCILIATION_RULE_VERSION


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
        lens = "offense" if _text_value(perspective) == "attacker" else "defense"
        moved[(int(from_id), lens)] = int(to_id)
        if not listed:
            unlisted.append(int(report))
    return moved, unlisted


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


def add_republish_command(
    subparsers: Any,
    database_argument: Callable[[argparse.ArgumentParser], None],
    bounded_int: Callable[[str, int, int], Callable[[str], int]],
) -> None:
    """Add the ``republish-current-season`` command to the CLI."""
    republish_current_season = subparsers.add_parser(
        "republish-current-season",
        help="queue a bounded batch of current-season ranked-day republications",
    )
    database_argument(republish_current_season)
    republish_current_season.add_argument(
        "--max-jobs",
        type=bounded_int("republication batch size", 1, 1000),
        default=100,
    )
    # With --campaign, run one action of a Season's repair campaign instead
    # of queueing a batch; see domain_repair.
    republish_current_season.add_argument("--campaign", choices=domain_repair.ACTIONS)
    republish_current_season.add_argument("--season", type=_season_id)


def _season_id(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("season must be an official Season ID")
    return value


def run_republish_command(database_url: str, arguments: argparse.Namespace) -> int:
    """Queue one batch, or run one campaign action, and print its report."""
    if (arguments.campaign is None) != (arguments.season is None):
        raise SystemExit("--campaign and --season go together")
    database = Database(database_url)
    try:
        if arguments.campaign is not None:
            report = domain_repair.run_campaign_command(
                database, arguments.campaign, arguments.season
            )
        else:
            report = enqueue_current_season_republication(
                database, max_jobs=arguments.max_jobs
            )
            report["enqueued_count"] = len(report["job_ids"])
        print(json.dumps(report, sort_keys=True, default=str))
    finally:
        database.close()
    return 0 if "refused" not in report else 1


def enqueue_current_season_republication(
    database: Database,
    *,
    max_jobs: int = 100,
) -> dict[str, Any]:
    """Queue bounded current-season Reset repairs before v3 republication.

    This rebuilds derived ranked-day publications from canonical database
    evidence; it does not replay archived source observations. Repeating
    the call advances past already queued targets, so an operator can drain
    a season in measured batches without an unbounded deployment action.
    A batch that re-checks partial Reset pairs reports only that work; see
    ``reset_baselines.repair_current_season_reset_baselines``.
    """

    if isinstance(max_jobs, bool) or not 1 <= max_jobs <= 1000:
        raise ValueError("current-season republication batch must be 1 to 1000")
    # Days whose battles migration 0057 moved come first. Then days left Live
    # by Reset pairs wrongly recorded as partial: finishing them is what lets
    # those days publish at all.
    moved = enqueue_rebuilds(database, max_jobs=max_jobs)
    if moved["job_ids"]:
        return moved
    repaired = reset_baselines.repair_current_season_reset_baselines(
        database, max_works=max_jobs
    )
    repaired["failed_blockers"][:0] = moved["failed_blockers"]
    if repaired["evaluated_count"]:
        return repaired
    with database.pool.connection() as connection:
        with connection.transaction():
            shield_job_ids = _enqueue_false_shield_rebuilds(connection, max_jobs)
            if shield_job_ids:
                return {**repaired, "job_ids": shield_job_ids}
            candidates = connection.execute(
                """
                WITH current_anchor AS (
                    SELECT current_league_season_id, current_start
                    FROM legend_season_anchors
                    WHERE state = 'confirmed'
                      AND anchor_rule_version = %s
                    ORDER BY current_start DESC
                    LIMIT 1
                ), published AS (
                    SELECT DISTINCT
                           log.player_id,
                           log.ranked_day_start,
                           anchor.current_league_season_id
                    FROM api_player_daily_logs AS log
                    JOIN current_anchor AS anchor
                      ON log.official_season_id =
                         anchor.current_league_season_id
                    WHERE log.ranked_day_start >= anchor.current_start
                      AND log.ranked_day_start <
                          anchor.current_start + interval '28 days'
                )
                SELECT player_id, ranked_day_start,
                       current_league_season_id
                FROM published
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM ranked_day_versions AS version
                    WHERE version.player_id = published.player_id
                      AND version.ranked_day_start =
                          published.ranked_day_start
                      AND version.reconciliation_rule_version = %s
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM python_processing_jobs_worker AS job
                    WHERE job.work_type = 'reconcile_ranked_day'
                      AND job.state IN (
                          'pending', 'waiting_retry', 'waiting_dependency', 'leased'
                      )
                      AND (job.input_json ->> 'player_id')::bigint =
                          published.player_id
                      AND job.input_json ->> 'ranked_day_start' =
                          to_char(
                              published.ranked_day_start AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                          )
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM python_processing_jobs_worker AS job
                    WHERE job.deduplication_key =
                        'reconcile:current-season:'
                        || published.player_id::text || ':'
                        || to_char(
                            published.ranked_day_start AT TIME ZONE 'UTC',
                            'YYYY-MM-DD"T"HH24:MI:SS"Z"'
                        ) || ':' || %s
                )
                ORDER BY player_id, ranked_day_start
                LIMIT %s
                """,
                (
                    SEASON_ANCHOR_RULE_VERSION,
                    RECONCILIATION_RULE_VERSION,
                    RECONCILIATION_RULE_VERSION,
                    max_jobs,
                ),
            ).fetchall()
            job_ids: list[int] = []
            for player_id, ranked_day_start, official_season_id in candidates:
                ranked_day_start_text = ranked_day_start.astimezone(UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                deduplication_key = (
                    f"reconcile:current-season:{int(player_id)}:"
                    f"{ranked_day_start_text}:{RECONCILIATION_RULE_VERSION}"
                )
                row = connection.execute(
                    """
                    INSERT INTO python_processing_jobs_worker (
                        observation_id, work_type, deduplication_key,
                        input_json, state, due_at, parser_version,
                        processing_version, domain_rule_version,
                        analytics_rule_version
                    ) VALUES (
                        NULL, 'reconcile_ranked_day', %s, %s, 'pending',
                        clock_timestamp(), %s, %s, %s, %s
                    )
                    ON CONFLICT (deduplication_key) DO NOTHING
                    RETURNING id
                    """,
                    (
                        deduplication_key,
                        Jsonb(
                            {
                                "player_id": int(player_id),
                                "ranked_day_start": ranked_day_start_text,
                                "official_season_id": _text_value(
                                    official_season_id
                                ),
                                "trigger": "current_season_republication",
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
            return {**repaired, "job_ids": job_ids}


def _enqueue_false_shield_rebuilds(connection: Any, max_jobs: int) -> list[int]:
    """Queue rebuilds of current-season days wrongly inferred shielded.

    Older code inferred a shield on a no-event day even when the next Reset's
    observed trophies differed. Each job rebuilds that day and every later
    saved day in the Season, so later shield durations use the corrected day.
    """
    rows = connection.execute(
        """
        WITH current_anchor AS (
            SELECT current_league_season_id, current_start
            FROM legend_season_anchors
            WHERE state = 'confirmed' AND anchor_rule_version = %s
            ORDER BY current_start DESC
            LIMIT 1
        ), latest AS (
            SELECT DISTINCT ON (log.player_id, log.ranked_day_start)
                   log.player_id, log.ranked_day_start,
                   log.official_season_id, log.ranked_day_version_id
            FROM api_player_daily_logs AS log
            JOIN current_anchor AS anchor
              ON log.official_season_id = anchor.current_league_season_id
            WHERE log.ranked_day_start >= anchor.current_start
              AND log.ranked_day_start < anchor.current_start + interval '28 days'
            ORDER BY log.player_id, log.ranked_day_start, log.version DESC
        )
        SELECT version.id, latest.player_id, latest.ranked_day_start,
               latest.official_season_id
        FROM latest
        JOIN ranked_day_versions AS version
          ON version.id = latest.ranked_day_version_id
        WHERE version.shield_state = 'inferred_shielded'
          AND version.unexplained_residual IS DISTINCT FROM 0
          AND NOT EXISTS (
              SELECT 1
              FROM python_processing_jobs_worker AS job
              WHERE job.deduplication_key =
                  'reconcile:false-shield:' || version.id::text
          )
        ORDER BY latest.player_id, latest.ranked_day_start
        LIMIT %s
        """,
        (SEASON_ANCHOR_RULE_VERSION, max_jobs),
    ).fetchall()
    job_ids: list[int] = []
    for version_id, player_id, ranked_day_start, official_season_id in rows:
        day_text = ranked_day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                f"reconcile:false-shield:{int(version_id)}",
                Jsonb(
                    {
                        "player_id": int(player_id),
                        "ranked_day_start": day_text,
                        "last_ranked_day_start": day_text,
                        "recalculate_season": _text_value(official_season_id),
                        "trigger": "false_shield_rebuild",
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
    return job_ids
