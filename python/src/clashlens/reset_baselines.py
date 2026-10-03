from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary, reset_settlement
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Claim,
    Database,
    _text_value,
)
from .domain import SEASON_ANCHOR_RULE_VERSION, battle_window

# Reset work stops collecting at 04:55 UTC the next day, as in the collector.
RESET_COLLECTION_WINDOW = timedelta(hours=23, minutes=55)


def _refresh_reset_baseline_evidence(
    database: Database,
    connection: Any,
    claim: Claim,
    *,
    failure_category: str | None = None,
    failure_retryable: bool = False,
) -> None:
    if claim.observation_id is None:
        return
    _evaluate_reset_baseline(
        database,
        connection,
        observation_id=claim.observation_id,
        observation_endpoint=claim.endpoint,
        parser_version=claim.parser_version,
        processing_version=claim.processing_version,
        failure_category=failure_category,
        failure_retryable=failure_retryable,
    )


def repair_current_season_reset_baselines(
    database: Database, *, max_works: int
) -> dict[str, Any]:
    """Re-check current-season Reset pairs left partial with both results saved.

    Until profiles and battle logs were read under their own parser versions,
    every such pair stayed partial, so its Legend day was never finished. One
    batch of at most ``max_works`` pairs is re-checked from saved results, each
    in its own short transaction. Returns the end-of-day reconciliation jobs
    queued, how many pairs were checked, and how often each failure reason was
    seen; a checked count of zero means no such pair is left. The season's
    opening Reset is re-checked as day 1's starting evidence but queues no
    leaderboard, army or day rebuild for the previous season. A completed pair
    also rebuilds the ended current-season day it starts, which may already
    have been finished without it. Within the same batch limit, repairs of
    complete pairs that failed and left an ended day Live are queued again
    (counted as checked); ``failed_blockers`` lists those it cannot retry.
    """

    with database.pool.connection() as connection:
        with connection.transaction():
            candidates = connection.execute(
                """
                WITH current_anchor AS (
                    SELECT current_start, current_league_season_id
                    FROM legend_season_anchors
                    WHERE state = 'confirmed' AND anchor_rule_version = %s
                    ORDER BY current_start DESC
                    LIMIT 1
                )
                SELECT work.profile_observation_id, (
                    SELECT outcome.parser_version
                    FROM observation_processing_outcomes AS outcome
                    WHERE outcome.observation_id = work.profile_observation_id
                      AND outcome.processing_version = %s
                    ORDER BY outcome.id DESC
                    LIMIT 1
                ), sweep.boundary_at > anchor.current_start,
                sweep.boundary_at < anchor.current_start + interval '28 days'
                AND sweep.boundary_at + interval '1 day' <= clock_timestamp(),
                anchor.current_league_season_id
                FROM collector_work AS work
                JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
                JOIN current_anchor AS anchor
                  ON sweep.boundary_at >= anchor.current_start
                 AND sweep.boundary_at <= anchor.current_start + interval '28 days'
                WHERE work.kind = 'reset_baseline'
                  AND (
                      SELECT evidence.state
                      FROM reset_baseline_evidence AS evidence
                      WHERE evidence.collector_work_id = work.id
                      ORDER BY evidence.version DESC, evidence.id DESC
                      LIMIT 1
                  ) = 'partial'
                  AND EXISTS (
                      SELECT 1 FROM observation_processing_outcomes AS outcome
                      WHERE outcome.observation_id = work.profile_observation_id
                        AND outcome.processing_version = %s
                  )
                  AND EXISTS (
                      SELECT 1 FROM observation_processing_outcomes AS outcome
                      WHERE outcome.observation_id = work.battle_log_observation_id
                        AND outcome.processing_version = %s
                  )
                ORDER BY work.id
                LIMIT %s
                """,
                (
                    SEASON_ANCHOR_RULE_VERSION,
                    PROCESSING_VERSION,
                    PROCESSING_VERSION,
                    PROCESSING_VERSION,
                    max_works,
                ),
            ).fetchall()
        job_ids = []
        failure_reasons: Counter[str] = Counter()
        for (
            profile_observation_id,
            profile_parser_version,
            ends_day,
            starts_ended_day,
            official_season_id,
        ) in candidates:
            with connection.transaction():
                pair_job_ids, reasons = _evaluate_reset_baseline(
                    database,
                    connection,
                    observation_id=int(profile_observation_id),
                    observation_endpoint="profile",
                    parser_version=_text_value(profile_parser_version),
                    processing_version=PROCESSING_VERSION,
                    ends_day=bool(ends_day),
                    starts_ended_day=bool(starts_ended_day),
                    recalculate_season=_text_value(official_season_id),
                )
            job_ids.extend(pair_job_ids)
            failure_reasons.update(reasons)
        with connection.transaction():
            recovered, failed_blockers = _recover_failed_reset_repairs(
                connection, limit=max_works - len(candidates)
            )
    return {
        "job_ids": job_ids + recovered,
        "evaluated_count": len(candidates) + len(recovered),
        "failure_reasons": dict(sorted(failure_reasons.items())),
        "failed_blockers": failed_blockers,
    }


# Failures from running out of time or retries, not from bad input.
TRANSIENT_REPAIR_FAILURES = ("lease_expired_max_attempts", "database_deadlock")


def _recover_failed_reset_repairs(
    connection: Any, *, limit: int
) -> tuple[list[int], list[dict[str, Any]]]:
    """Re-queue Reset repairs that failed while a day they rebuild stays Live.

    The pair stays complete when its repair job fails, so the partial-pair
    check above never finds it again. A repair that failed from running out
    of time or retries is queued once more with its original inputs under a
    new key naming the failed job, which stays as it was. Every other such
    failure, including a failed re-queue, is returned as a blocker. A repair
    sharing a day with other queued or running work, or with a recovery
    queued earlier in this batch, waits for a later run. Recoveries and
    blockers are each limited to ``limit``.
    """

    if limit <= 0:
        return [], []
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('reset-recovery', 0))"
    )
    rows = connection.execute(
        """
        WITH current_anchor AS (
            SELECT current_start
            FROM legend_season_anchors
            WHERE state = 'confirmed' AND anchor_rule_version = %s
            ORDER BY current_start DESC
            LIMIT 1
        ), failed AS (
            SELECT job.id, job.deduplication_key, job.failure_category,
                   job.input_json,
                   (job.input_json ->> 'player_id')::bigint AS player_id
            FROM python_processing_jobs_worker AS job
            JOIN current_anchor AS anchor
              ON (job.input_json ->> 'ranked_day_start')::timestamptz
                 >= anchor.current_start
             AND (job.input_json ->> 'ranked_day_start')::timestamptz
                 < anchor.current_start + interval '28 days'
            WHERE job.work_type = 'reconcile_ranked_day'
              AND job.state = 'failed'
              AND (
                  job.deduplication_key LIKE 'reconcile:reset-baseline:%%'
                  OR job.deduplication_key LIKE 'reconcile:reset-recovery:%%'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM python_processing_jobs_worker AS recovery
                  WHERE recovery.deduplication_key =
                      'reconcile:reset-recovery:' || job.id::text
              )
        ), jobs AS (
            SELECT failed.id, failed.input_json, false AS active FROM failed
            UNION ALL
            SELECT job.id, job.input_json, true
            FROM python_processing_jobs_worker AS job
            WHERE job.work_type = 'reconcile_ranked_day'
              AND job.state IN (
                  'pending', 'waiting_retry', 'waiting_dependency', 'leased'
              )
              AND (job.input_json ->> 'player_id')::bigint IN (
                  SELECT failed.player_id FROM failed
              )
        ), rebuilds AS (
            SELECT job.id, job.active,
                   (job.input_json ->> 'player_id')::bigint AS player_id,
                   day.ranked_day_start
            FROM jobs AS job
            CROSS JOIN LATERAL (
                SELECT (job.input_json ->> 'ranked_day_start')::timestamptz
                UNION
                SELECT (job.input_json ->> 'last_ranked_day_start')::timestamptz
                WHERE job.input_json ? 'recalculate_season'
                UNION
                SELECT log.ranked_day_start
                FROM api_player_daily_logs AS log
                WHERE log.player_id = (job.input_json ->> 'player_id')::bigint
                  AND log.ranked_day_start
                      >= (job.input_json ->> 'ranked_day_start')::timestamptz
                  AND log.official_season_id =
                      job.input_json ->> 'recalculate_season'
            ) AS day (ranked_day_start)
        ), candidates AS (
            SELECT failed.id, failed.failure_category, failed.input_json,
                   coalesce(
                       failed.deduplication_key LIKE 'reconcile:reset-baseline:%%'
                       AND failed.failure_category = ANY(%s)
                       AND (
                           SELECT latest.id = (failed.input_json ->> 'reset_baseline_id')::bigint
                                  AND latest.state = 'complete'
                           FROM reset_baseline_evidence AS original
                           JOIN reset_baseline_evidence AS latest
                             ON latest.collector_work_id = original.collector_work_id
                           WHERE original.id = (failed.input_json ->> 'reset_baseline_id')::bigint
                           ORDER BY latest.version DESC, latest.id DESC
                           LIMIT 1
                       ),
                       false
                   ) AS retryable,
                   (
                       SELECT array_agg(own.ranked_day_start)
                       FROM rebuilds AS own
                       WHERE own.id = failed.id
                   ) AS days
            FROM failed
            WHERE NOT EXISTS (
                SELECT 1
                FROM rebuilds AS own
                JOIN rebuilds AS other
                  ON other.active
                 AND other.player_id = own.player_id
                 AND other.ranked_day_start = own.ranked_day_start
                WHERE own.id = failed.id
            )
              AND EXISTS (
                SELECT 1
                FROM rebuilds AS own
                WHERE own.id = failed.id
                  AND own.ranked_day_start + interval '1 day' <= clock_timestamp()
                  AND (
                      SELECT version.state
                      FROM ranked_day_versions AS version
                      WHERE version.player_id = own.player_id
                        AND version.ranked_day_start = own.ranked_day_start
                      ORDER BY version.version DESC, version.id DESC
                      LIMIT 1
                  ) = 'Live'
            )
        )
        SELECT id, failure_category, input_json, retryable, days
        FROM (
            SELECT candidates.*,
                   row_number() OVER (
                       PARTITION BY retryable ORDER BY id
                   ) AS position
            FROM candidates
        ) AS ranked
        WHERE position <= %s
        ORDER BY id
        """,
        (SEASON_ANCHOR_RULE_VERSION, list(TRANSIENT_REPAIR_FAILURES), limit),
    ).fetchall()
    job_ids: list[int] = []
    blockers: list[dict[str, Any]] = []
    queued_days: dict[int, set[datetime]] = {}
    for job_id, failure_category, input_json, retryable, days in rows:
        player_id = int(input_json["player_id"])
        if not retryable:
            blockers.append(
                {
                    "job_id": int(job_id),
                    "player_id": player_id,
                    "ranked_day_start": input_json["ranked_day_start"],
                    "failure_category": (
                        _text_value(failure_category) if failure_category else None
                    ),
                }
            )
            continue
        player_days = queued_days.setdefault(player_id, set())
        if player_days.intersection(days):
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
                f"reconcile:reset-recovery:{int(job_id)}",
                Jsonb({**input_json, "recovers_job_id": int(job_id)}),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
            ),
        ).fetchone()
        if row is not None:
            job_ids.append(int(row[0]))
            player_days.update(days)
    return job_ids, blockers


def settle_failed_reset_work(database: Database, *, max_works: int = 100) -> int:
    """Record evidence for Reset work the collector gave up on.

    Work that failed before any response arrived has no processing job, and
    work whose last response was processed while it was still retrying holds
    only partial evidence. Either would hold its publication forever. This
    re-checks such work from every Reset, at most ``max_works`` at a time, so
    a worker restart picks it up again however long it was stopped. Missing
    or failed responses make the evidence ``failed``; a response collected
    later is never used in place of the missed one.
    """

    with database.pool.connection() as connection:
        with connection.transaction():
            work_ids = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT work.id
                    FROM collector_reset_sweeps AS sweep
                    JOIN collector_work AS work ON work.sweep_id = sweep.id
                    LEFT JOIN LATERAL (
                        SELECT evidence.state, evidence.failure_reasons
                        FROM reset_baseline_evidence AS evidence
                        WHERE evidence.collector_work_id = work.id
                        ORDER BY evidence.version DESC, evidence.id DESC
                        LIMIT 1
                    ) AS latest ON true
                    WHERE work.kind = 'reset_baseline'
                      AND work.status = 'failed'
                      AND COALESCE(latest.state, 'partial') = 'partial'
                      -- A saved response not yet processed re-checks its
                      -- pair when its own job finishes; this pass cannot.
                      AND NOT COALESCE(
                          latest.failure_reasons
                          ?| array['unprocessed_profile', 'unprocessed_battle_log'],
                          false
                      )
                    ORDER BY work.id
                    LIMIT %s
                    """,
                    (max_works,),
                ).fetchall()
            ]
        for work_id in work_ids:
            with connection.transaction():
                _evaluate_reset_baseline(
                    database,
                    connection,
                    observation_id=None,
                    observation_endpoint=None,
                    parser_version=DEFAULT_PARSER_VERSION,
                    processing_version=PROCESSING_VERSION,
                    work_id=work_id,
                )
    return len(work_ids)


def _evaluate_reset_baseline(
    database: Database,
    connection: Any,
    *,
    observation_id: int | None,
    observation_endpoint: str | None,
    parser_version: str,
    processing_version: str,
    failure_category: str | None = None,
    failure_retryable: bool = False,
    ends_day: bool = True,
    starts_ended_day: bool = False,
    recalculate_season: str | None = None,
    work_id: int | None = None,
) -> tuple[list[int], list[str]]:
    """Record Reset pair evidence and return queued job IDs and failure reasons.

    A repair's selected ended days share one job so their dependent results
    are rebuilt oldest first in one transaction, including later saved days
    in ``recalculate_season``.
    """
    context = _load_reset_baseline_context(connection, observation_id, work_id)
    if context is None:
        return [], []
    work_id, player_id, normalized_tag, sweep_id, boundary_at = context
    # The profile and battle-log jobs of one Reset pair often run at the same
    # time. Lock before reading so the later job sees the earlier job's
    # committed result instead of each recording only its own endpoint.
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"reset-baseline:{work_id}",),
    )
    endpoints = {
        endpoint: _load_reset_endpoint_evidence(database, 
            connection,
            endpoint=endpoint,
            work_id=int(work_id),
            player_id=int(player_id),
            normalized_tag=_text_value(normalized_tag),
            boundary_at=boundary_at,
            parser_version=parser_version,
            processing_version=processing_version,
            source_observation_id=observation_id,
            source_endpoint=observation_endpoint,
            failure_category=failure_category,
            failure_retryable=failure_retryable,
        )
        for endpoint in ("profile", "battle_log")
    }
    reasons = list(
        dict.fromkeys(
            reason
            for endpoint in endpoints.values()
            for reason in endpoint["reasons"]
        )
    )
    profile = endpoints["profile"]
    battle_log = endpoints["battle_log"]
    profile_valid = bool(profile["valid"])
    battle_log_valid = bool(battle_log["valid"])
    hard_failure = any(endpoint["hard_failure"] for endpoint in endpoints.values())
    # The profile proves the Reset only if the battle log was collected at or
    # after it, so the log shows every battle before the profile.
    if (
        profile["collected_at"] is not None
        and battle_log["collected_at"] is not None
        and battle_log["collected_at"] < profile["collected_at"]
    ):
        retrying = profile["work_status"] in {"pending", "waiting_retry"}
        reasons.append(f"battle_log_before_profile{'_retrying' if retrying else ''}")
        profile_valid = False
        hard_failure = hard_failure or not retrying
    if profile_valid and battle_log_valid and not hard_failure:
        state = "complete"
        reasons = []
    elif hard_failure:
        state = "failed"
    else:
        state = "partial"

    evidence_json = {
        "collector_work_id": int(work_id),
        "reset_sweep_id": int(sweep_id),
        "profile": {
            "observation_id": profile["observation_id"],
            "processing_outcome_id": profile["processing_outcome_id"],
            "processing_outcome": profile["processing_outcome"],
        },
        "battle_log": {
            "observation_id": battle_log["observation_id"],
            "processing_outcome_id": battle_log["processing_outcome_id"],
            "processing_outcome": battle_log["processing_outcome"],
        },
        "failure_reasons": reasons,
    }
    fingerprint = {
        **evidence_json,
        "profile_valid": profile_valid,
        "battle_log_valid": battle_log_valid,
        "state": state,
        "parser_version": parser_version,
        "processing_version": processing_version,
    }
    evidence_key = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    existing = connection.execute(
        """
        SELECT id, version
        FROM reset_baseline_evidence
        WHERE collector_work_id = %s AND evidence_key = %s
        """,
        (work_id, evidence_key),
    ).fetchone()
    if existing is not None:
        evidence_id, version = int(existing[0]), int(existing[1])
    else:
        prior = connection.execute(
            """
            SELECT id, version
            FROM reset_baseline_evidence
            WHERE collector_work_id = %s
            ORDER BY version DESC, id DESC
            LIMIT 1
            FOR UPDATE
            """,
            (work_id,),
        ).fetchone()
        version = int(prior[1]) + 1 if prior is not None else 1
        inserted = connection.execute(
            """
            INSERT INTO reset_baseline_evidence (
                sweep_id, player_id, boundary_at, collector_work_id,
                profile_observation_id, battle_log_observation_id,
                profile_valid, battle_log_valid,
                profile_processing_outcome_id, battle_log_processing_outcome_id,
                parser_version, processing_version, version, supersedes_id,
                state, failure_reasons, evidence_json, evidence_key
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                sweep_id,
                player_id,
                boundary_at,
                work_id,
                profile["observation_id"],
                battle_log["observation_id"],
                profile_valid,
                battle_log_valid,
                profile["processing_outcome_id"],
                battle_log["processing_outcome_id"],
                parser_version,
                processing_version,
                version,
                prior[0] if prior is not None else None,
                state,
                Jsonb(reasons),
                Jsonb(evidence_json),
                evidence_key,
            ),
        ).fetchone()
        assert inserted is not None
        evidence_id = int(inserted[0])

    reset_settlement.record_provisional_boundary(
        connection,
        player_id=int(player_id),
        boundary_at=boundary_at,
        sweep_id=int(sweep_id),
        early_baseline_id=evidence_id,
        early_state=state,
        reasons=reasons,
    )
    if state in {"complete", "failed"} and ends_day:
        _record_boundary_baseline(database, 
            connection,
            boundary_at=boundary_at,
            reset_sweep_id=int(sweep_id),
            player_id=int(player_id),
            state=state,
        )
    if state != "complete":
        return [], reasons
    day_starts = [boundary_at - timedelta(days=1)] if ends_day else []
    if starts_ended_day:
        day_starts.append(boundary_at)
    if not day_starts:
        return [], reasons
    job_id = _enqueue_reset_reconciliation(
        connection,
        baseline_id=evidence_id,
        baseline_version=version,
        player_id=int(player_id),
        boundary_at=boundary_at,
        ranked_day_start=day_starts[0],
        last_ranked_day_start=day_starts[-1],
        recalculate_season=recalculate_season,
    )
    return [job_id] if job_id is not None else [], reasons


def _record_boundary_baseline(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    reset_sweep_id: int,
    player_id: int,
    state: str,
) -> None:
    """Record one terminal reset result and reevaluate both artifacts."""
    boundary_at = boundary_at.astimezone(UTC)
    boundary.lock_boundary_publication(connection, boundary_at)
    sweep = connection.execute(
        """
        SELECT id, member_ids
        FROM collector_reset_sweeps
        WHERE id = %s AND boundary_at = %s
        """,
        (reset_sweep_id, boundary_at),
    ).fetchone()
    if sweep is None:
        return
    member_ids = [int(value) for value in (sweep[1] or [])]
    if player_id not in member_ids:
        return
    generation = connection.execute(
        """
        SELECT id, snapshot_state, army_state
        FROM boundary_publication_generations
        WHERE boundary_at = %s AND generation = 1
        FOR UPDATE
        """,
        (boundary_at,),
    ).fetchone()
    if generation is None:
        generation_id, _generation = boundary._create_boundary_generation(database, 
            connection,
            boundary_at=boundary_at,
            sweep_id=reset_sweep_id,
            player_ids=member_ids,
            generation=1,
            supersedes_id=None,
        )
        generation = (generation_id, "pending", "pending")
    if (
        _text_value(generation[1]) == "superseded"
        and _text_value(generation[2]) == "superseded"
    ):
        return

    if state == "failed":
        snapshot_status = army_status = "unavailable"
    else:
        ranked_state = connection.execute(
            """
            SELECT ranked.id
            FROM boundary_publication_generation_members AS member
            JOIN ranked_day_versions AS ranked
              ON ranked.id = member.ranked_day_version_id
            WHERE member.generation_id = %s AND member.player_id = %s
            """,
            (generation[0], player_id),
        ).fetchone()
        ranked_version_id = int(ranked_state[0]) if ranked_state else None
        snapshot_status = (
            boundary._boundary_snapshot_status(
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_version_id,
                boundary_at=boundary_at,
            )
            if ranked_version_id is not None
            else "pending"
        )
        army_status = (
            boundary._boundary_army_status(database, 
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_version_id,
                snapshot_status=snapshot_status,
            )
            if ranked_version_id is not None
            else "pending"
        )
    member_status = (
        "unavailable"
        if snapshot_status == "unavailable"
        else ("terminal" if snapshot_status != "pending" else "pending")
    )
    connection.execute(
        """
        UPDATE boundary_publication_generation_members
        SET status = %s, snapshot_status = %s, army_status = %s,
            updated_at = clock_timestamp()
        WHERE generation_id = %s AND player_id = %s
        """,
        (member_status, snapshot_status, army_status, generation[0], player_id),
    )
    boundary._try_enqueue_boundary_artifacts(database, 
        connection, boundary_at=boundary_at, generation_id=int(generation[0])
    )


def _load_reset_baseline_context(
    connection: Any,
    observation_id: int | None,
    work_id: int | None = None,
) -> tuple[Any, ...] | None:
    match = (
        "work.id = %s"
        if work_id is not None
        else "(work.profile_observation_id = %s OR work.battle_log_observation_id = %s)"
    )
    row = connection.execute(
        f"""
        SELECT work.id, work.player_id, work.normalized_tag,
               work.sweep_id, sweep.boundary_at
        FROM collector_work AS work
        JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
        WHERE work.kind = 'reset_baseline' AND {match}
        """,
        (work_id,) if work_id is not None else (observation_id, observation_id),
    ).fetchone()
    return None if row is None else tuple(row)


def _load_reset_endpoint_evidence(
    database: Database,
    connection: Any,
    *,
    endpoint: str,
    work_id: int,
    player_id: int,
    normalized_tag: str,
    boundary_at: datetime,
    parser_version: str,
    processing_version: str,
    source_observation_id: int,
    source_endpoint: str | None,
    failure_category: str | None,
    failure_retryable: bool,
) -> dict[str, Any]:
    profile_join = (
        """
        LEFT JOIN player_profile_effects AS profile_effect
          ON profile_effect.observation_id = observed.id
         AND profile_effect.parser_version = processing.parser_version
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = profile_effect.profile_version_id
        """
        if getattr(database, "_supports_content_dedup", False)
        else """
        LEFT JOIN player_profile_versions AS profile
          ON profile.observation_id = observed.id
         AND profile.parser_version = processing.parser_version
        """
    )
    endpoint_column = (
        "work.profile_observation_id"
        if endpoint == "profile"
        else "work.battle_log_observation_id"
    )
    endpoint_status = (
        "work.profile_status"
        if endpoint == "profile"
        else "work.battle_log_status"
    )
    row = connection.execute(
        f"""
        SELECT {endpoint_status}, observed.id, observed.player_id,
               observed.normalized_tag, observed.response_completed_at,
               observed.http_status, processing.id, processing.outcome,
               processing.failure_category, profile.id,
               profile.source_contract_state, profile.eligibility_state,
               battle_log.id, battle_log.has_row_gap, work.status,
               work.failure_category
        FROM collector_work AS work
        LEFT JOIN collector_observations AS observed
          ON observed.id = {endpoint_column}
        -- Profiles and battle logs have different parser versions, so the
        -- other endpoint's result is its own latest outcome, not one under
        -- this job's parser version.
        LEFT JOIN LATERAL (
            SELECT outcome.id, outcome.outcome, outcome.failure_category,
                   outcome.parser_version
            FROM observation_processing_outcomes AS outcome
            WHERE outcome.observation_id = observed.id
              AND outcome.processing_version = %s
            ORDER BY (
                outcome.observation_id = %s AND outcome.parser_version = %s
            ) DESC, outcome.id DESC
            LIMIT 1
        ) AS processing ON true
        {profile_join}
        LEFT JOIN battle_log_observations AS battle_log
          ON battle_log.observation_id = observed.id
         AND battle_log.parser_version = processing.parser_version
        WHERE work.id = %s
        """,
        (
            processing_version,
            source_observation_id,
            parser_version,
            work_id,
        ),
    ).fetchone()
    if row is None:
        raise RuntimeError("reset work disappeared while processing its evidence")

    observation_id = int(row[1]) if row[1] is not None else None
    processing_id = int(row[6]) if row[6] is not None else None
    processing_outcome = _text_value(row[7]) if row[7] is not None else None
    reasons: list[str] = []
    hard_failure = False
    missing = observation_id is None
    if missing:
        reasons.append(f"missing_{endpoint}_observation")
        hard_failure = _text_value(row[14]) == "failed"
    else:
        if int(row[2]) != player_id or _text_value(row[3]) != normalized_tag:
            reasons.append(f"{endpoint}_wrong_player")
            hard_failure = True
        if row[4] is None or row[4] < boundary_at:
            reasons.append(f"{endpoint}_stale")
            hard_failure = True
        elif row[4] >= boundary_at + RESET_COLLECTION_WINDOW:
            reasons.append(f"{endpoint}_late")
            hard_failure = True
        if processing_outcome is None:
            missing = True
            if (
                source_endpoint == endpoint
                and source_observation_id == observation_id
                and failure_category is not None
            ):
                suffix = f"_{failure_category}"
                reasons.append(
                    f"{endpoint}{suffix}{'_retrying' if failure_retryable else ''}"
                )
                hard_failure = not failure_retryable
            else:
                reasons.append(f"unprocessed_{endpoint}")
        elif processing_outcome == "non_success":
            # A server error or rate limit is final only once the collector
            # stops retrying; a later response replaces it on the work row.
            retrying = (row[5] == 429 or row[5] >= 500) and _text_value(
                row[14]
            ) in {"pending", "waiting_retry"}
            reasons.append(
                f"{endpoint}_non_success{'_retrying' if retrying else ''}"
            )
            hard_failure = not retrying
        elif processing_outcome != "processed":
            category = (
                _text_value(row[8]) if row[8] is not None else processing_outcome
            )
            reasons.append(f"{endpoint}_{category}")
            hard_failure = True
        elif endpoint == "profile":
            if (
                row[9] is None
                or _text_value(row[10]) != "accepted"
                or _text_value(row[11]) != "eligible"
            ):
                reasons.append("profile_invalid")
                hard_failure = True
            else:
                first_event = connection.execute(
                    """
                    SELECT min(evidence.battle_timestamp)
                    FROM battle_evidence AS evidence
                    JOIN legend_battles AS battle
                      ON battle.id = evidence.battle_id
                    WHERE (
                        battle.attacker_player_id = %s
                        OR battle.defender_player_id = %s
                    )
                      AND evidence.battle_timestamp >= %s
                      AND evidence.battle_timestamp < %s
                    """,
                    (player_id, player_id, *battle_window(boundary_at)),
                ).fetchone()[0]
                if first_event is not None and row[4] >= first_event:
                    reasons.append("profile_after_first_event")
                    hard_failure = True
        elif row[12] is None or bool(row[13]):
            reasons.append("battle_log_malformed")
            hard_failure = True

    return {
        "observation_id": observation_id,
        "collected_at": row[4],
        "work_status": _text_value(row[14]),
        "processing_outcome_id": processing_id,
        "processing_outcome": processing_outcome,
        "reasons": reasons,
        "hard_failure": hard_failure,
        "valid": not reasons and not missing and not hard_failure,
    }


def _load_reset_baseline(
    database: Database,
    connection: Any,
    player_id: int,
    boundary_at: datetime,
    processing_version: str,
) -> dict[str, Any] | None:
    dedup = getattr(database, "_supports_content_dedup", False)
    profile_observed = "profile_effect.observed_at" if dedup else "profile.observed_at"
    profile_join = (
        """
        LEFT JOIN player_profile_effects AS profile_effect
          ON profile_effect.observation_id = evidence.profile_observation_id
         AND profile_effect.parser_version = profile_processing.parser_version
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = profile_effect.profile_version_id
        """
        if dedup
        else """
        LEFT JOIN player_profile_versions AS profile
          ON profile.observation_id = evidence.profile_observation_id
         AND profile.parser_version = profile_processing.parser_version
        """
    )
    row = connection.execute(
        f"""
        SELECT evidence.id, evidence.version, evidence.state,
               evidence.sweep_id, evidence.collector_work_id,
               evidence.profile_observation_id,
               evidence.battle_log_observation_id,
               evidence.profile_processing_outcome_id,
               evidence.battle_log_processing_outcome_id,
               evidence.profile_valid, evidence.battle_log_valid,
               evidence.failure_reasons, evidence.evidence_json,
               evidence.evidence_key, evidence.boundary_at,
               profile.id, profile.trophies, profile.eligibility_state,
               profile.source_contract_state, {profile_observed},
               battle_log.id, battle_log.row_count, battle_log.has_row_gap,
               profile_observation.response_hash,
               battle_observation.response_hash
        FROM reset_baseline_evidence AS evidence
        -- Each endpoint is read under the parser version that processed it.
        LEFT JOIN observation_processing_outcomes AS profile_processing
          ON profile_processing.id = evidence.profile_processing_outcome_id
        LEFT JOIN observation_processing_outcomes AS battle_processing
          ON battle_processing.id = evidence.battle_log_processing_outcome_id
        {profile_join}
        LEFT JOIN collector_observations AS profile_observation
          ON profile_observation.id = evidence.profile_observation_id
         AND profile_observation.endpoint = 'profile'
        LEFT JOIN battle_log_observations AS battle_log
          ON battle_log.observation_id = evidence.battle_log_observation_id
         AND battle_log.parser_version = battle_processing.parser_version
        LEFT JOIN collector_observations AS battle_observation
          ON battle_observation.id = evidence.battle_log_observation_id
         AND battle_observation.endpoint = 'battle_log'
        WHERE evidence.player_id = %s
          AND evidence.boundary_at = %s
          AND evidence.processing_version = %s
        ORDER BY evidence.version DESC, evidence.id DESC
        LIMIT 1
        """,
        (player_id, boundary_at, processing_version),
    ).fetchone()
    if row is None:
        return None

    state = _text_value(row[2])
    profile_valid = bool(row[9])
    battle_log_valid = bool(row[10])
    profile_accepted = row[15] is not None and _text_value(row[18]) == "accepted"
    profile_eligible = _text_value(row[17]) == "eligible"
    battle_log_valid_evidence = row[20] is not None and not bool(row[22])
    complete = bool(
        state == "complete"
        and row[4] is not None
        and profile_valid
        and battle_log_valid
        and profile_accepted
        and profile_eligible
        and battle_log_valid_evidence
        and all(row[index] is not None for index in (5, 6, 7, 8))
    )
    failure_reasons = row[11] if isinstance(row[11], list) else []
    stored_evidence = row[12] if isinstance(row[12], dict) else {}
    evidence = {
        "id": int(row[0]),
        "version": int(row[1]),
        "state": state,
        "sweep_id": int(row[3]) if row[3] is not None else None,
        "collector_work_id": int(row[4]) if row[4] is not None else None,
        "profile_observation_id": int(row[5]) if row[5] is not None else None,
        "battle_log_observation_id": int(row[6]) if row[6] is not None else None,
        "profile_processing_outcome_id": (
            int(row[7]) if row[7] is not None else None
        ),
        "battle_log_processing_outcome_id": (
            int(row[8]) if row[8] is not None else None
        ),
        "profile_valid": profile_valid,
        "battle_log_valid": battle_log_valid,
        "failure_reasons": list(failure_reasons),
        "evidence_key": _text_value(row[13]),
        "boundary_at": row[14].astimezone(UTC).isoformat(),
        "profile": {
            "id": int(row[15]) if row[15] is not None else None,
            "trophies": int(row[16]) if row[16] is not None else None,
            "eligibility_state": (
                _text_value(row[17]) if row[17] is not None else None
            ),
            "source_contract_state": (
                _text_value(row[18]) if row[18] is not None else None
            ),
            "observed_at": (
                row[19].astimezone(UTC).isoformat() if row[19] is not None else None
            ),
            "response_hash": _text_value(row[23]),
        },
        "battle_log": {
            "id": int(row[20]) if row[20] is not None else None,
            "row_count": int(row[21]) if row[21] is not None else None,
            "has_row_gap": bool(row[22]) if row[22] is not None else None,
            "response_hash": _text_value(row[24]),
        },
        "stored_evidence": stored_evidence,
    }
    return {
        "id": int(row[0]),
        "version": int(row[1]),
        "state": state,
        "complete": complete,
        "trophies": int(row[16]) if row[16] is not None else None,
        "eligibility_state": (
            _text_value(row[17]) if row[17] is not None else None
        ),
        "evidence": evidence,
    }


def _enqueue_reset_reconciliation(
    connection: Any,
    *,
    baseline_id: int,
    baseline_version: int,
    player_id: int,
    boundary_at: datetime,
    ranked_day_start: datetime,
    last_ranked_day_start: datetime,
    recalculate_season: str | None,
) -> int | None:
    boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ranked_day_start_text = ranked_day_start.astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    deduplication_key = (
        f"reconcile:reset-baseline:{baseline_id}:v{baseline_version}"
    )
    if ranked_day_start == boundary_at:
        deduplication_key += f":{ranked_day_start_text}"
    row = connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
            %s, %s, %s, %s
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
                    "boundary_at": boundary_text,
                    "reset_baseline_id": int(baseline_id),
                    "reset_baseline_version": int(baseline_version),
                    **(
                        {
                            "last_ranked_day_start": (
                                last_ranked_day_start.astimezone(UTC).strftime(
                                    "%Y-%m-%dT%H:%M:%SZ"
                                )
                            ),
                            "recalculate_season": recalculate_season,
                        }
                        if recalculate_season is not None
                        else {}
                    ),
                }
            ),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
        ),
    ).fetchone()
    return int(row[0]) if row is not None else None
