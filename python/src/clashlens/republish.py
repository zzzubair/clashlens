"""Operator entry point for bounded Season repairs and republication."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC
from typing import Any

from psycopg.types.json import Jsonb

from . import (
    battle_day_repair,
    boundary,
    domain_repair,
    first_battle_log,
    reset_baselines,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
)
from .db_rows import text_value
from .domain import SEASON_ANCHOR_RULE_VERSION
from .reconciliation import RECONCILIATION_RULE_VERSION


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
    # With --repair, run one action of the Season repair, recalculating every
    # saved day and board of the Season under the current rules with a
    # before and after receipt; see domain_repair.season_repair.
    republish_current_season.add_argument(
        "--repair", choices=domain_repair.REPAIR_ACTIONS
    )
    # With --players, the Season repair recalculates only the players a rule
    # change can affect; see domain_repair._scoped_players.
    republish_current_season.add_argument("--players", choices=domain_repair.REPAIR_SCOPES)
    # With --campaign, run one action of a Season's repair campaign instead
    # of queueing a batch; see domain_repair.
    republish_current_season.add_argument("--campaign", choices=domain_repair.ACTIONS)
    # With --first-logs, preview or queue the days that players first tracked
    # during the Season can now fill; see first_battle_log.backfill.
    republish_current_season.add_argument("--first-logs", choices=("preview", "queue"))
    # With --boards, preview or queue rebuilds of the Season's Reset boards
    # still ranking a reading the board now leaves out or whose entries the
    # board rule now changes; see boundary.queue_board_rebuilds.
    republish_current_season.add_argument("--boards", choices=("preview", "queue"))
    # With --armies, preview or queue army rebuilds of the Season's Resets
    # that published a side as unread although an army is now saved for it,
    # at most --max-jobs at once; see boundary.queue_army_corrections.
    republish_current_season.add_argument("--armies", choices=("preview", "queue"))
    republish_current_season.add_argument("--season", type=_season_id)


def _season_id(value: str) -> str:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("season must be an official Season ID")
    return value


def run_republish_command(database_url: str, arguments: argparse.Namespace) -> int:
    """Queue one batch, or run one campaign action, and print its report."""
    repair = getattr(arguments, "repair", None)
    players = getattr(arguments, "players", None)
    first_logs = getattr(arguments, "first_logs", None)
    boards = getattr(arguments, "boards", None)
    armies = getattr(arguments, "armies", None)
    modes = [mode for mode in (repair, arguments.campaign, first_logs, boards, armies)
             if mode is not None]
    if len(modes) > 1:
        raise SystemExit(
            "--repair, --campaign, --first-logs, --boards and --armies"
            " are separate runs"
        )
    if players is not None and repair is None:
        raise SystemExit("--players goes with --repair")
    if (not modes) != (arguments.season is None):
        raise SystemExit(
            "--repair, --campaign, --first-logs, --boards or --armies and --season"
            " go together"
        )
    database = Database(database_url)
    try:
        if repair is not None:
            report = domain_repair.season_repair(
                database, arguments.season, repair, max_jobs=arguments.max_jobs,
                scope=players,
            )
        elif boards is not None:
            report = boundary.queue_board_rebuilds(
                database, arguments.season, queue=boards == "queue"
            )
        elif armies is not None:
            report = boundary.queue_army_corrections(
                database, arguments.season, queue=armies == "queue",
                max_jobs=arguments.max_jobs,
            )
        elif first_logs is not None:
            report = first_battle_log.backfill(
                database, arguments.season, queue=first_logs == "queue",
                max_jobs=arguments.max_jobs,
            )
        elif arguments.campaign is not None:
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
    moved = battle_day_repair.enqueue_rebuilds(database, max_jobs=max_jobs)
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
            live_job_ids, live_blockers = _enqueue_ended_live_rebuilds(
                connection, max_jobs
            )
            repaired["failed_blockers"].extend(live_blockers)
            if live_job_ids:
                return {**repaired, "job_ids": live_job_ids}
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
                                "official_season_id": text_value(
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
                        "recalculate_season": text_value(official_season_id),
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


def _enqueue_ended_live_rebuilds(
    connection: Any, max_jobs: int
) -> tuple[list[int], list[dict[str, Any]]]:
    """Queue rebuilds of ended days whose latest published result is Live,
    or has no published net despite 8 undisputed attacks and 8 undisputed
    defenses.

    A day calculated before its Reset stays Live until it is calculated
    again. Failed Reset evidence did not do that, and a day with no Reset
    check never had it done. A day with every attack and defense recorded
    has a known net, which older calculations left out. Each job rebuilds a
    player's oldest such day not yet requested and every later saved day of
    its Season, so later days read the finished one; the result may honestly
    be Partial with no end total. The previous Season is covered too, so its
    last days can still be finished after the anchor moves on. The key names
    the version replaced, so a later such result is repaired again even after
    an earlier request finished. A failed request whose result is still
    latest is not queued again but returned, at most ``max_jobs`` of them, as
    a blocker; deleting the failed job lets a later run queue it.
    """
    rows = connection.execute(
        """
        WITH anchor AS (
            SELECT current_league_season_id, previous_league_season_id,
                   previous_start
            FROM legend_season_anchors
            WHERE state = 'confirmed' AND anchor_rule_version = %s
            ORDER BY current_start DESC
            LIMIT 1
        ), latest AS (
            SELECT DISTINCT ON (log.player_id, log.ranked_day_start)
                   log.player_id, log.ranked_day_start,
                   log.official_season_id, log.ranked_day_version_id,
                   log.net_trophy_change
            FROM api_player_daily_logs AS log
            JOIN anchor
              ON log.official_season_id IN (
                  anchor.current_league_season_id,
                  anchor.previous_league_season_id
              )
            WHERE log.ranked_day_start >= anchor.previous_start
              AND log.ranked_day_start + interval '1 day' <= clock_timestamp()
            ORDER BY log.player_id, log.ranked_day_start, log.version DESC
        ), unfinished AS (
            SELECT version.id, latest.player_id, latest.ranked_day_start,
                   latest.official_season_id, job.id AS job_id,
                   job.state AS job_state, job.failure_category
            FROM latest
            JOIN ranked_day_versions AS version
              ON version.id = latest.ranked_day_version_id
            LEFT JOIN python_processing_jobs_worker AS job
              ON job.deduplication_key = 'reconcile:ended-live:' || version.id::text
            WHERE version.state = 'Live'
               OR (
                  latest.net_trophy_change IS NULL
                  AND version.attack_count = 8
                  AND version.defense_count = 8
                  AND NOT version.failure_reasons ?| ARRAY[
                      'perspective_disagreement',
                      'duplicate_contribution_disagreement'
                  ]
               )
        ), oldest AS (
            SELECT DISTINCT ON (player_id) *
            FROM unfinished
            WHERE job_id IS NULL
            ORDER BY player_id, ranked_day_start
        ), candidates AS (
            SELECT * FROM oldest
            WHERE NOT EXISTS (
                SELECT 1
                FROM python_processing_jobs_worker AS active
                WHERE active.work_type = 'reconcile_ranked_day'
                  AND active.state IN (
                      'pending', 'waiting_retry', 'waiting_dependency', 'leased'
                  )
                  AND (active.input_json ->> 'player_id')::bigint = oldest.player_id
                  AND (
                      (active.input_json ->> 'ranked_day_start')::timestamptz
                          = oldest.ranked_day_start
                      OR active.input_json ->> 'recalculate_season'
                          = oldest.official_season_id
                  )
            )
            UNION ALL
            SELECT * FROM unfinished WHERE job_state = 'failed'
        )
        SELECT id, player_id, ranked_day_start, official_season_id,
               job_id, failure_category
        FROM (
            SELECT candidates.*,
                   row_number() OVER (
                       PARTITION BY job_id IS NULL
                       ORDER BY ranked_day_start, player_id
                   ) AS position
            FROM candidates
        ) AS ranked
        WHERE position <= %s
        ORDER BY ranked_day_start, player_id
        """,
        (SEASON_ANCHOR_RULE_VERSION, max_jobs),
    ).fetchall()
    job_ids: list[int] = []
    blockers: list[dict[str, Any]] = []
    for version_id, player_id, day_start, season_id, job_id, category in rows:
        day_text = day_start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if job_id is not None:
            blockers.append(
                {
                    "job_id": int(job_id),
                    "player_id": int(player_id),
                    "ranked_day_start": day_text,
                    "failure_category": text_value(category) if category else None,
                }
            )
            continue
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
                f"reconcile:ended-live:{int(version_id)}",
                Jsonb(
                    {
                        "player_id": int(player_id),
                        "ranked_day_start": day_text,
                        "last_ranked_day_start": day_text,
                        "recalculate_season": text_value(season_id),
                        "trigger": "ended_live_rebuild",
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
    return job_ids, blockers
