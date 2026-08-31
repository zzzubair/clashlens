#!/usr/bin/env python3
"""Read private runtime facts once and return the objective operating check."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clashlens.operating import (
    COLLECTOR_API_OUTCOMES,
    COLLECTOR_JOB_OUTCOMES,
    COLLECTOR_STAGES,
    LATENCY_BUCKETS_SECONDS,
    RELATION_NAMES,
    build_operating_snapshot,
)

MAX_SOURCE_BYTES = 8 * 1024 * 1024
WORKER_LIMIT = 16
COLLECTOR_WORK_TYPES = {
    "regular_poll",
    "initial_collection",
    "live_refresh",
    "legacy_reset_profile",
    "legacy_unresolved_reset",
    "reset_baseline",
    "discovery_profile",
    "endpoint_retry",
    "global_player_rankings",
}
COLLECTOR_POOLS = {"normal", "interactive"}
COLLECTOR_ENDPOINTS = {"profile", "battle_log", "global_player_rankings"}
IGNORED_COLLECTOR_SCALARS = {
    "clashlens_collector_active_leases",
    "clashlens_collector_expired_leases",
    "clashlens_collector_failed_jobs",
    "clashlens_collector_incomplete_attempts",
    "clashlens_collector_live_refresh_coalesced_total",
    "clashlens_collector_live_refresh_cooldown_hits_total",
    "clashlens_collector_live_refresh_latest_latency_seconds",
    "clashlens_collector_oldest_due_age_seconds",
    "clashlens_collector_pending_remote_verifications",
    "clashlens_collector_queue_depth",
    "clashlens_collector_reset_sweep_elapsed_seconds",
    "clashlens_collector_reset_sweep_members_total",
    "clashlens_collector_reset_sweep_missing",
    "clashlens_collector_reset_sweep_observed",
    "clashlens_collector_waiting_dependencies",
    "clashlens_collector_waiting_retries",
    "clashlens_spool_allocated_bytes",
    "clashlens_spool_orphan_bytes",
    "clashlens_spool_orphan_count",
}
IGNORED_COLLECTOR_LABELS = {
    "clashlens_collector_api_duration_seconds_count": ("endpoint", "pool"),
    "clashlens_collector_api_duration_seconds_sum": ("endpoint", "pool"),
    "clashlens_collector_api_requests_total": ("endpoint", "pool"),
    "clashlens_collector_key_cooldown_seconds": ("pool",),
    "clashlens_collector_key_quarantines_total": ("pool",),
    "clashlens_collector_key_requests_last_second": ("pool",),
    "clashlens_collector_keys_healthy": ("pool",),
    "clashlens_collector_keys_total": ("pool",),
    "clashlens_collector_observation_freshness_seconds": ("endpoint",),
    "clashlens_collector_retries_total": ("endpoint",),
    "clashlens_collector_storage_errors_total": ("category",),
}
COLLECTOR_STORAGE_ERROR_CATEGORIES = {
    "archive_catalogue_contradiction",
    "archive_checksum_mismatch",
    "archive_terminal_configuration",
    "archive_write_failed",
    "database_transaction_failed",
    "degraded_capacity",
}


def _relation_values() -> str:
    return ",".join(f"('{name}')" for name in RELATION_NAMES)


DATABASE_SQL = r"""
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;
WITH
snapshot_clock AS MATERIALIZED (
    SELECT statement_timestamp() AS captured_at
),
collector_queue AS MATERIALIZED (
    SELECT
        count(*) FILTER (WHERE status = 'pending')::bigint AS pending,
        count(*) FILTER (WHERE status = 'leased')::bigint AS leased,
        count(*) FILTER (WHERE status = 'waiting_retry')::bigint AS waiting_retry,
        count(*) FILTER (WHERE status = 'waiting_dependency')::bigint AS waiting_dependency,
        count(*) FILTER (WHERE status = 'complete')::bigint AS complete,
        count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
        count(*) FILTER (WHERE status = 'cancelled')::bigint AS cancelled,
        extract(epoch FROM clock.captured_at - min(due_at) FILTER (
            WHERE status IN ('pending','waiting_retry','waiting_dependency')
              AND due_at <= clock.captured_at
        )) AS oldest_due_seconds,
        count(*) FILTER (WHERE status = 'waiting_retry')::bigint AS retry_jobs,
        count(*) FILTER (WHERE status = 'waiting_dependency')::bigint AS dependency_jobs,
        count(*) FILTER (
            WHERE status = 'leased' AND lease_owner IS NOT NULL
              AND lease_token IS NOT NULL AND lease_expires_at > clock.captured_at
        )::bigint AS valid_leases,
        count(*) FILTER (
            WHERE status = 'leased' AND lease_expires_at <= clock.captured_at
        )::bigint AS expired_recoverable_leases
    FROM snapshot_clock AS clock
    LEFT JOIN collector_jobs AS job ON true
    GROUP BY clock.captured_at
),
python_queue AS MATERIALIZED (
    SELECT
        count(*) FILTER (WHERE status = 'pending')::bigint AS pending,
        count(*) FILTER (WHERE status = 'leased')::bigint AS leased,
        count(*) FILTER (WHERE status = 'waiting_retry')::bigint AS waiting_retry,
        count(*) FILTER (WHERE status = 'waiting_dependency')::bigint AS waiting_dependency,
        count(*) FILTER (WHERE status = 'complete')::bigint AS complete,
        count(*) FILTER (WHERE status = 'failed')::bigint AS failed,
        count(*) FILTER (WHERE status = 'cancelled')::bigint AS cancelled,
        extract(epoch FROM clock.captured_at - min(due_at) FILTER (
            WHERE status IN ('pending','waiting_retry','waiting_dependency')
              AND due_at <= clock.captured_at
        )) AS oldest_due_seconds,
        count(*) FILTER (WHERE status = 'waiting_retry')::bigint AS retry_jobs,
        count(*) FILTER (WHERE status = 'waiting_dependency')::bigint AS dependency_jobs,
        count(*) FILTER (
            WHERE status = 'leased' AND lease_owner IS NOT NULL
              AND lease_token IS NOT NULL AND lease_expires_at > clock.captured_at
        )::bigint AS valid_leases,
        count(*) FILTER (
            WHERE status = 'leased' AND lease_expires_at <= clock.captured_at
              AND attempt_count < max_attempts
        )::bigint AS expired_recoverable_leases,
        count(*) FILTER (
            WHERE status = 'leased' AND lease_expires_at <= clock.captured_at
              AND attempt_count >= max_attempts
        )::bigint AS expired_unrecoverable_leases
    FROM snapshot_clock AS clock
    LEFT JOIN python_processing_jobs AS job ON true
    GROUP BY clock.captured_at
),
retained_failures AS MATERIALIZED (
    SELECT
        (SELECT count(*) FROM collector_transport_failures)::bigint AS transport,
        (
            (SELECT count(*) FROM source_response_parses WHERE outcome <> 'valid')
          + (SELECT count(*) FROM observation_processing_outcomes
             WHERE outcome IN ('non_success','malformed','unsupported','integrity_failure'))
          + (SELECT count(*) FROM processed_observation_versions WHERE outcome = 'failed')
          + (SELECT count(*) FROM ranked_day_versions
             WHERE state IN ('Partial','Inconsistent','Malformed'))
          + (SELECT count(*) FROM battle_army_decodes WHERE status = 'failed')
        )::bigint AS data_quality
),
generation_artifacts_unbounded AS MATERIALIZED (
    SELECT generation.*, 'snapshot'::text AS artifact,
           generation.snapshot_state AS artifact_state
    FROM boundary_publication_generations AS generation
    UNION ALL
    SELECT generation.*, 'army'::text AS artifact,
           generation.army_state AS artifact_state
    FROM boundary_publication_generations AS generation
),
generation_artifacts AS MATERIALIZED (
    SELECT ranked.*
    FROM (
        SELECT artifact.*,
               sum(CASE WHEN artifact_state IN ('published','superseded')
                        THEN 1 ELSE 0 END) OVER (
                   PARTITION BY artifact
                   ORDER BY boundary_at DESC, generation DESC
               ) AS terminal_history_rank
        FROM generation_artifacts_unbounded AS artifact
    ) AS ranked
    WHERE artifact_state NOT IN ('published','superseded')
       OR terminal_history_rank <= 8
),
active_artifacts AS MATERIALIZED (
    SELECT artifact.id AS generation_id, artifact.generation,
           artifact.artifact, artifact.artifact_state AS state,
           artifact.boundary_at, artifact.target_at, artifact.target_rule,
           members.member_count, members.pending_members,
           members.complete_members, members.partial_members,
           members.failed_members, members.missing_members,
           members.unavailable_members, members.inconsistent_members,
           members.malformed_members,
           python_jobs.pending AS python_pending,
           python_jobs.valid_leases AS python_valid_leases,
           python_jobs.due_retries AS python_due_retries,
           python_jobs.due_dependencies AS python_due_dependencies,
           python_jobs.recoverable_expired AS python_recoverable_expired,
           python_jobs.unrecoverable_expired AS python_unrecoverable_expired,
           python_jobs.failed AS python_failed,
           collector_jobs.pending AS collector_pending,
           collector_jobs.valid_leases AS collector_valid_leases,
           collector_jobs.due_retries AS collector_due_retries,
           collector_jobs.due_dependencies AS collector_due_dependencies,
           collector_jobs.expired AS collector_expired,
           collector_jobs.failed AS collector_failed,
           COALESCE(admission.safe_handoff, false) AS safe_handoff,
           artifact.expected_population_count,
           artifact.artifact_state = 'ready' OR (
               artifact.artifact_state = 'pending' AND (
                   artifact.target_at > clock.captured_at
                   OR (COALESCE(admission.safe_handoff, false)
                       AND members.pending_members = 0
                       AND members.member_count = artifact.expected_population_count)
                   OR (members.pending_members > 0 AND (
                       collector_jobs.pending + collector_jobs.valid_leases
                       + collector_jobs.due_retries + collector_jobs.due_dependencies
                       + collector_jobs.expired
                       + python_jobs.pending + python_jobs.valid_leases
                       + python_jobs.due_retries + python_jobs.due_dependencies
                       + python_jobs.recoverable_expired > 0))
               )
           ) AS can_coordinate,
           clock.captured_at
    FROM generation_artifacts AS artifact
    CROSS JOIN snapshot_clock AS clock
    LEFT JOIN collector_boundary_admission AS admission
      ON admission.boundary_at = artifact.boundary_at
    CROSS JOIN LATERAL (
        SELECT count(*)::bigint AS member_count,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'pending')::bigint AS pending_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'complete')::bigint AS complete_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'partial')::bigint AS partial_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'failed')::bigint AS failed_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'missing')::bigint AS missing_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'unavailable')::bigint AS unavailable_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'inconsistent')::bigint AS inconsistent_members,
               count(*) FILTER (WHERE CASE artifact.artifact
                   WHEN 'snapshot' THEN member.snapshot_status
                   ELSE member.army_status END = 'malformed')::bigint AS malformed_members
        FROM boundary_publication_generation_members AS member
        WHERE member.generation_id = artifact.id
    ) AS members
    CROSS JOIN LATERAL (
        SELECT
            count(*) FILTER (WHERE legal.status = 'pending'
                AND legal.due_at <= clock.captured_at)::bigint AS pending,
            count(*) FILTER (WHERE legal.status = 'leased'
                AND legal.lease_expires_at > clock.captured_at)::bigint AS valid_leases,
            count(*) FILTER (WHERE legal.status = 'waiting_retry'
                AND legal.due_at <= clock.captured_at)::bigint AS due_retries,
            count(*) FILTER (WHERE legal.status = 'waiting_dependency'
                AND legal.due_at <= clock.captured_at)::bigint AS due_dependencies,
            count(*) FILTER (WHERE legal.status = 'leased'
                AND legal.lease_expires_at <= clock.captured_at
                AND legal.attempt_count < legal.max_attempts)::bigint AS recoverable_expired,
            count(*) FILTER (WHERE legal.status = 'leased'
                AND legal.lease_expires_at <= clock.captured_at
                AND legal.attempt_count >= legal.max_attempts)::bigint AS unrecoverable_expired,
            count(*) FILTER (WHERE legal.status = 'failed')::bigint AS failed
        FROM (
            -- Population-wide jobs must carry the immutable manifest identity.
            SELECT job.status, job.due_at, job.lease_expires_at,
                   job.attempt_count, job.max_attempts
            FROM python_processing_jobs AS job
            JOIN boundary_publication_manifests AS manifest
              ON manifest.id::text = job.input_json ->> 'manifest_id'
             AND manifest.generation_id = artifact.id
             AND manifest.artifact_kind = artifact.artifact
             AND manifest.digest = job.input_json ->> 'manifest_digest'
            WHERE job.input_json ->> 'generation' = artifact.generation::text
              AND CASE artifact.artifact
                  WHEN 'snapshot' THEN job.work_type IN ('build_snapshot','build_analytics')
                  ELSE job.work_type = 'build_army_analytics' END
              AND job.status IN ('pending','leased','waiting_retry','waiting_dependency','failed')

            UNION ALL

            -- Reset reconciliation is member-scoped. Live reconciliation has
            -- no boundary_at and is deliberately not attributed to a reset.
            SELECT job.status, job.due_at, job.lease_expires_at,
                   job.attempt_count, job.max_attempts
            FROM python_processing_jobs AS job
            JOIN boundary_publication_generation_members AS member
              ON member.generation_id = artifact.id
             AND member.player_id::text = job.input_json ->> 'player_id'
            WHERE job.work_type = 'reconcile_ranked_day'
              AND job.input_json ->> 'boundary_at' = to_char(
                  artifact.boundary_at AT TIME ZONE 'UTC',
                  'YYYY-MM-DD"T"HH24:MI:SS"Z"')
              AND job.input_json ->> 'ranked_day_start' = to_char(
                  (artifact.boundary_at - interval '1 day') AT TIME ZONE 'UTC',
                  'YYYY-MM-DD"T"HH24:MI:SS"Z"')
              AND jsonb_typeof(job.input_json -> 'player_id') = 'number'
              AND job.input_json ->> 'player_id' ~ '^[1-9][0-9]*$'
              AND jsonb_typeof(job.input_json -> 'ranked_day_start') = 'string'
              AND job.input_json ->> 'ranked_day_start'
                    ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
              AND job.status IN ('pending','leased','waiting_retry','waiting_dependency','failed')

            UNION ALL

            -- Observation processing and replay work is relevant only when
            -- its source observation belongs to this reset sweep and member.
            -- The attempt's job must remain in the reset-baseline lineage.
            SELECT job.status, job.due_at, job.lease_expires_at,
                   job.attempt_count, job.max_attempts
            FROM python_processing_jobs AS job
            JOIN collector_observations AS observation
              ON observation.id = COALESCE(job.observation_id, job.replay_observation_id)
            JOIN collector_attempts AS observed_attempt
              ON observed_attempt.id = observation.attempt_id
             AND observed_attempt.job_id = observation.collection_job_id
            JOIN collector_jobs AS observed_job
              ON observed_job.id = observed_attempt.job_id
            JOIN boundary_publication_generation_members AS member
              ON member.generation_id = artifact.id
             AND member.player_id = observation.player_id
             AND member.player_id = observed_job.player_id
            WHERE artifact.sweep_id IS NOT NULL
              AND job.work_type IN ('process_observation','replay_observation')
              AND observed_job.sweep_id = artifact.sweep_id
              AND job.status IN ('pending','leased','waiting_retry','waiting_dependency','failed')
              AND EXISTS (
                  SELECT 1
                  FROM collector_reset_baseline_sweeps AS baseline
                  JOIN collector_jobs AS root_job
                    ON root_job.parent_attempt_id IS NULL
                   AND root_job.work_type IN ('reset_baseline','legacy_reset_profile')
                   AND root_job.sweep_id = baseline.reset_sweep_id
                   AND root_job.reset_baseline_sweep_id = baseline.id
                   AND root_job.player_id = baseline.player_id
                  WHERE baseline.reset_sweep_id = artifact.sweep_id
                    AND baseline.player_id = member.player_id
                    AND observed_job.reset_baseline_sweep_id = baseline.id
                    AND clashlens_reset_job_lineage_v2(
                        observation.collection_job_id, root_job.id)
              )
        ) AS legal
    ) AS python_jobs
    CROSS JOIN LATERAL (
        SELECT
            count(*) FILTER (WHERE job.status = 'pending'
                AND job.due_at <= clock.captured_at)::bigint AS pending,
            count(*) FILTER (WHERE job.status = 'leased'
                AND job.lease_expires_at > clock.captured_at)::bigint AS valid_leases,
            count(*) FILTER (WHERE job.status = 'waiting_retry'
                AND job.due_at <= clock.captured_at)::bigint AS due_retries,
            count(*) FILTER (WHERE job.status = 'waiting_dependency'
                AND job.due_at <= clock.captured_at)::bigint AS due_dependencies,
            count(*) FILTER (WHERE job.status = 'leased'
                AND job.lease_expires_at <= clock.captured_at)::bigint AS expired,
            count(*) FILTER (WHERE job.status = 'failed')::bigint AS failed
        FROM collector_jobs AS job
        WHERE job.sweep_id = artifact.sweep_id
          AND job.status IN ('pending','leased','waiting_retry','waiting_dependency','failed')
    ) AS collector_jobs
),
boundary_json AS MATERIALIZED (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'generation_id', generation_id,
        'generation', generation,
        'artifact', artifact,
        'state', state,
        'boundary_at', boundary_at,
        'target_at', target_at,
        'target_rule', target_rule,
        'member_classifications', jsonb_build_object(
            'complete', complete_members, 'partial', partial_members,
            'failed', failed_members, 'missing', missing_members,
            'unavailable', unavailable_members, 'inconsistent', inconsistent_members,
            'malformed', malformed_members, 'pending', pending_members
        ),
        'publication_outcome', CASE state
            WHEN 'published' THEN 'published'
            WHEN 'superseded' THEN 'superseded'
            WHEN 'failed' THEN 'failed'
            ELSE 'not_published' END,
        'queued_work', python_pending + collector_pending,
        'valid_leases', python_valid_leases + collector_valid_leases,
        'due_retries', python_due_retries + collector_due_retries,
        'dependency_transitions', python_due_dependencies + collector_due_dependencies,
        'recoverable_expired_leases', python_recoverable_expired + collector_expired,
        'unrecoverable_expired_leases', python_unrecoverable_expired,
        'coordinator_transition', can_coordinate,
        'blocking_failures', CASE
            WHEN state = 'failed' THEN 1
            WHEN python_pending + collector_pending + python_valid_leases
               + collector_valid_leases + python_due_retries + collector_due_retries
               + python_due_dependencies + collector_due_dependencies
               + python_recoverable_expired + collector_expired = 0
               AND NOT can_coordinate
            THEN python_failed + collector_failed
            ELSE 0 END
    ) ORDER BY boundary_at, generation, artifact), '[]'::jsonb) AS artifacts,
    count(*) FILTER (WHERE state NOT IN ('published','superseded'))::bigint AS active_artifacts,
    count(*) FILTER (WHERE state = 'failed')::bigint AS failed_artifacts
    FROM active_artifacts
),
known_relations(name) AS (VALUES __RELATION_VALUES__),
relation_sizes AS MATERIALIZED (
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'name', known.name,
        'table_bytes', GREATEST(pg_table_size(class.oid) - CASE
            WHEN class.reltoastrelid = 0 THEN 0
            ELSE pg_total_relation_size(class.reltoastrelid) END, 0),
        'index_bytes', pg_indexes_size(class.oid),
        'toast_bytes', CASE WHEN class.reltoastrelid = 0 THEN 0
            ELSE pg_total_relation_size(class.reltoastrelid) END,
        'total_bytes', pg_total_relation_size(class.oid)
    ) ORDER BY known.name), '[]'::jsonb) AS value
    FROM known_relations AS known
    JOIN pg_class AS class ON class.oid = to_regclass(current_schema() || '.' || known.name)
),
wal AS MATERIALIZED (
    SELECT CASE WHEN has_function_privilege(
        current_user, 'pg_catalog.pg_ls_waldir()', 'EXECUTE'
    ) THEN (SELECT COALESCE(sum(size), 0)::bigint FROM pg_catalog.pg_ls_waldir())
    ELSE NULL::bigint END AS retained_bytes
)
SELECT jsonb_build_object(
    'schema_version', 1,
    'captured_at', (SELECT captured_at FROM snapshot_clock),
    'identity', jsonb_build_object(
        'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
        'database_oid', (SELECT oid::bigint FROM pg_database
                         WHERE datname = current_database())
    ),
    'isolation', 'repeatable_read_read_only',
    'contract_version', (SELECT version FROM clash_lens_contract WHERE singleton),
    'migrations', COALESCE((SELECT jsonb_agg(jsonb_build_object(
        'version', version, 'applied_at', applied_at) ORDER BY version
    ) FROM clash_lens_schema_migrations), '[]'::jsonb),
    'queues', jsonb_build_object(
        'collector', (SELECT jsonb_build_object(
            'by_status', jsonb_build_object(
                'pending', pending, 'leased', leased, 'waiting_retry', waiting_retry,
                'waiting_dependency', waiting_dependency, 'complete', complete,
                'failed', failed, 'cancelled', cancelled),
            'oldest_due_seconds', oldest_due_seconds,
            'retry_jobs', retry_jobs, 'dependency_jobs', dependency_jobs,
            'valid_leases', valid_leases,
            'expired_recoverable_leases', expired_recoverable_leases,
            'expired_unrecoverable_leases', 0) FROM collector_queue),
        'python', (SELECT jsonb_build_object(
            'by_status', jsonb_build_object(
                'pending', pending, 'leased', leased, 'waiting_retry', waiting_retry,
                'waiting_dependency', waiting_dependency, 'complete', complete,
                'failed', failed, 'cancelled', cancelled),
            'oldest_due_seconds', oldest_due_seconds,
            'retry_jobs', retry_jobs, 'dependency_jobs', dependency_jobs,
            'valid_leases', valid_leases,
            'expired_recoverable_leases', expired_recoverable_leases,
            'expired_unrecoverable_leases', expired_unrecoverable_leases) FROM python_queue)
    ),
    'processed', jsonb_build_object(
        'observations', jsonb_build_object(
            'profile', (SELECT count(*) FROM collector_observations WHERE endpoint = 'profile'),
            'battle_log', (SELECT count(*) FROM collector_observations WHERE endpoint = 'battle_log'),
            'global_player_rankings', (SELECT count(*) FROM collector_observations WHERE endpoint = 'global_player_rankings')
        ),
        'facts', jsonb_build_object(
            'ranked_day_complete', (SELECT count(*) FROM ranked_day_versions WHERE state = 'Complete'),
            'ranked_day_partial', (SELECT count(*) FROM ranked_day_versions WHERE state = 'Partial'),
            'ranked_day_inconsistent', (SELECT count(*) FROM ranked_day_versions WHERE state = 'Inconsistent'),
            'ranked_day_malformed', (SELECT count(*) FROM ranked_day_versions WHERE state = 'Malformed'),
            'army_decoded', (SELECT count(*) FROM battle_army_decodes WHERE status = 'decoded'),
            'army_failed', (SELECT count(*) FROM battle_army_decodes WHERE status = 'failed')
        ),
        'results', jsonb_build_object(
            'leaderboard_snapshots', (SELECT count(*) FROM leaderboard_snapshots WHERE state = 'published'),
            'army_analytics_days', (SELECT count(*) FROM army_analytics_completed_days),
            'analytics_summaries', (SELECT count(*) FROM analytics_summaries)
        )
    ),
    'failures', jsonb_build_object(
        'active_boundary_blocking', jsonb_build_object(
            'total', (SELECT failed_artifacts FROM boundary_json),
            'by_category', jsonb_build_object(
                'storage', 0, 'transport', 0, 'lease_expired', 0, 'dependency', 0,
                'unsupported', 0, 'data_quality', 0,
                'other', (SELECT failed_artifacts FROM boundary_json)
            )
        ),
        'retained_historical', (SELECT jsonb_build_object(
            'total', transport + data_quality,
            'by_category', jsonb_build_object(
                'storage', 0, 'transport', transport, 'lease_expired', 0,
                'dependency', 0, 'unsupported', 0, 'data_quality', data_quality,
                'other', 0)
        ) FROM retained_failures)
    ),
    'boundary', jsonb_build_object(
        'active_count', (SELECT active_artifacts FROM boundary_json),
        'artifacts', (SELECT artifacts FROM boundary_json)
    ),
    'storage', jsonb_build_object(
        'relations', (SELECT value FROM relation_sizes),
        'wal', jsonb_build_object('retained_bytes', (SELECT retained_bytes FROM wal)),
        'optional_statistics', jsonb_build_object(
            'statement_timing', jsonb_build_object(
                'value', NULL, 'reason', CASE WHEN EXISTS (
                    SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
                ) THEN 'not_collected' ELSE 'extension_unavailable' END),
            'io_timing', jsonb_build_object('value', NULL, 'reason', 'not_collected')
        )
    )
)::text;
COMMIT;
""".replace("__RELATION_VALUES__", _relation_values())


def _run(command: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_SOURCE_BYTES:
        raise ValueError("source_unavailable")
    return completed.stdout.strip()


def _json_source(value: str) -> dict[str, Any]:
    if not value or len(value.encode()) > MAX_SOURCE_BYTES:
        raise ValueError("source_unavailable")
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise TypeError("source_unavailable")
    return parsed


def _labels(value: str) -> dict[str, str]:
    if not value:
        return {}
    labels: dict[str, str] = {}
    position = 0
    pattern = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"(?:,|$)')
    while position < len(value):
        match = pattern.match(value, position)
        if match is None or match.group(1) in labels:
            raise ValueError("metrics_invalid")
        labels[match.group(1)] = bytes(
            match.group(2), "utf-8"
        ).decode("unicode_escape")
        position = match.end()
    return labels


def _samples(metrics: str) -> list[tuple[str, dict[str, str], float]]:
    result: list[tuple[str, dict[str, str], float]] = []
    pattern = re.compile(
        r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+"
        r"(-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))$"
    )
    for line in metrics.splitlines():
        if not line or line.startswith("#"):
            continue
        match = pattern.fullmatch(line)
        if match is None:
            raise ValueError("metrics_invalid")
        result.append((match.group(1), _labels(match.group(2) or ""), float(match.group(3))))
    return result


def _nonnegative_integer(value: float) -> int:
    if value < 0 or not value.is_integer():
        raise ValueError("metrics_invalid")
    return int(value)


def parse_collector_metrics(metrics: str) -> dict[str, Any]:
    histograms = {
        stage: {
            "count": 0,
            "sum_seconds": 0.0,
            "buckets": [0] * (len(LATENCY_BUCKETS_SECONDS) + 1),
        }
        for stage in COLLECTOR_STAGES
    }
    jobs = dict.fromkeys(COLLECTOR_JOB_OUTCOMES, 0)
    api = dict.fromkeys(COLLECTOR_API_OUTCOMES, 0)
    scalars: dict[str, float] = {}
    process_id: str | None = None
    seen_histogram_parts: set[tuple[str, str, str]] = set()
    scalar_names = {
        "clashlens_collector_process_start_time_seconds",
        "clashlens_collector_database_pool_max_connections",
        "clashlens_collector_database_pool_acquired_connections",
        "clashlens_collector_database_pool_idle_connections",
        "clashlens_collector_database_pool_empty_acquires_total",
        "clashlens_collector_database_pool_cancelled_acquires_total",
        "clashlens_collector_database_pool_acquire_duration_seconds_total",
        "clashlens_spool_final_bytes",
        "clashlens_spool_temporary_bytes",
        "clashlens_spool_abandoned_temporary_bytes",
        "clashlens_spool_final_objects",
        "clashlens_spool_temporary_objects",
        "clashlens_spool_abandoned_temporary_objects",
        "clashlens_spool_reserved_bytes",
        "clashlens_spool_live_reservations",
        "clashlens_spool_high_water_bytes",
        "clashlens_spool_free_bytes",
        "clashlens_spool_free_inodes",
    }
    for name, labels, value in _samples(metrics):
        if name in scalar_names:
            if labels or name in scalars or value < 0:
                raise ValueError("metrics_invalid")
            scalars[name] = value
        elif name == "clashlens_collector_process_identity_info":
            if set(labels) != {"process_id"} or value != 1 or process_id is not None:
                raise ValueError("metrics_invalid")
            process_id = labels["process_id"]
        elif name.startswith("clashlens_collector_stage_duration_seconds_"):
            stage = labels.get("stage")
            if stage not in histograms:
                raise ValueError("metrics_invalid")
            suffix = name.removeprefix("clashlens_collector_stage_duration_seconds_")
            if suffix == "bucket":
                if set(labels) != {"stage", "le"}:
                    raise ValueError("metrics_invalid")
                upper = labels["le"]
                index = (
                    len(LATENCY_BUCKETS_SECONDS)
                    if upper == "+Inf"
                    else next(
                        (
                            item
                            for item, bound in enumerate(LATENCY_BUCKETS_SECONDS)
                            if float(upper) == bound
                        ),
                        -1,
                    )
                )
                if index < 0 or (stage, suffix, upper) in seen_histogram_parts:
                    raise ValueError("metrics_invalid")
                histograms[stage]["buckets"][index] = _nonnegative_integer(value)
                seen_histogram_parts.add((stage, suffix, upper))
            elif suffix in {"count", "sum"}:
                if set(labels) != {"stage"} or (stage, suffix, "") in seen_histogram_parts:
                    raise ValueError("metrics_invalid")
                field = "count" if suffix == "count" else "sum_seconds"
                histograms[stage][field] = (
                    _nonnegative_integer(value) if suffix == "count" else value
                )
                seen_histogram_parts.add((stage, suffix, ""))
            else:
                raise ValueError("metrics_invalid")
        elif name == "clashlens_collector_jobs_total":
            if set(labels) != {"work_type", "pool", "outcome"}:
                raise ValueError("metrics_invalid")
            if (
                labels["work_type"] not in COLLECTOR_WORK_TYPES
                or labels["pool"] not in COLLECTOR_POOLS
                or labels["outcome"] not in jobs
            ):
                raise ValueError("metrics_invalid")
            jobs[labels["outcome"]] += _nonnegative_integer(value)
        elif name == "clashlens_collector_api_outcomes_total":
            if set(labels) != {"endpoint", "outcome"} or labels[
                "endpoint"
            ] not in COLLECTOR_ENDPOINTS:
                raise ValueError("metrics_invalid")
            raw = labels["outcome"]
            category = {
                "2xx": "success",
                "3xx": "success",
                "4xx": "expected_4xx",
                "5xx": "safe_5xx",
            }.get(raw, "transport_failure" if "transport" in raw else "other")
            api[category] += _nonnegative_integer(value)
        elif name in IGNORED_COLLECTOR_SCALARS:
            if labels or (
                value < 0
                and not (
                    name
                    == "clashlens_collector_live_refresh_latest_latency_seconds"
                    and value == -1
                )
            ):
                raise ValueError("metrics_invalid")
        elif name in IGNORED_COLLECTOR_LABELS:
            if set(labels) != set(IGNORED_COLLECTOR_LABELS[name]) or (
                value < 0
                and not (
                    name == "clashlens_collector_observation_freshness_seconds"
                    and value == -1
                )
            ):
                raise ValueError("metrics_invalid")
            if "pool" in labels and labels["pool"] not in COLLECTOR_POOLS:
                raise ValueError("metrics_invalid")
            if "endpoint" in labels and labels["endpoint"] not in COLLECTOR_ENDPOINTS:
                raise ValueError("metrics_invalid")
            if (
                "category" in labels
                and labels["category"] not in COLLECTOR_STORAGE_ERROR_CATEGORIES
            ):
                raise ValueError("metrics_invalid")
        elif name.startswith("clashlens_"):
            raise ValueError("metrics_invalid")
    if set(scalars) != scalar_names or process_id is None:
        raise ValueError("metrics_invalid")
    started_at = datetime.fromtimestamp(
        scalars["clashlens_collector_process_start_time_seconds"], tz=UTC
    ).isoformat()
    return {
        "schema_version": 1,
        "process": {"id": process_id, "started_at": started_at},
        "database_pool": {
            "max_connections": _nonnegative_integer(
                scalars["clashlens_collector_database_pool_max_connections"]
            ),
            "acquired_connections": _nonnegative_integer(
                scalars["clashlens_collector_database_pool_acquired_connections"]
            ),
            "idle_connections": _nonnegative_integer(
                scalars["clashlens_collector_database_pool_idle_connections"]
            ),
            "empty_acquires_total": _nonnegative_integer(
                scalars["clashlens_collector_database_pool_empty_acquires_total"]
            ),
            "cancelled_requests_total": _nonnegative_integer(
                scalars["clashlens_collector_database_pool_cancelled_acquires_total"]
            ),
            "acquire_wait_seconds_total": scalars[
                "clashlens_collector_database_pool_acquire_duration_seconds_total"
            ],
        },
        "stages": histograms,
        "outcomes": {"jobs": jobs, "official_api": api},
        "spool": {
            "final_bytes": _nonnegative_integer(scalars["clashlens_spool_final_bytes"]),
            "final_objects": _nonnegative_integer(scalars["clashlens_spool_final_objects"]),
            "temporary_bytes": _nonnegative_integer(scalars["clashlens_spool_temporary_bytes"]),
            "temporary_objects": _nonnegative_integer(scalars["clashlens_spool_temporary_objects"]),
            "abandoned_temporary_bytes": _nonnegative_integer(
                scalars["clashlens_spool_abandoned_temporary_bytes"]
            ),
            "abandoned_temporary_objects": _nonnegative_integer(
                scalars["clashlens_spool_abandoned_temporary_objects"]
            ),
            "reserved_bytes": _nonnegative_integer(scalars["clashlens_spool_reserved_bytes"]),
            "reserved_objects": _nonnegative_integer(
                scalars["clashlens_spool_live_reservations"]
            ),
            "high_water_bytes": _nonnegative_integer(
                scalars["clashlens_spool_high_water_bytes"]
            ),
            "free_bytes": _nonnegative_integer(scalars["clashlens_spool_free_bytes"]),
            "free_inodes": _nonnegative_integer(scalars["clashlens_spool_free_inodes"]),
        },
    }


def _previous(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("previous_unavailable")
    return _json_source(path.read_text(encoding="utf-8"))


def collect_snapshot(arguments: argparse.Namespace) -> dict[str, Any]:
    collector = parse_collector_metrics(
        _run(
            [
                arguments.curl_bin,
                "--fail",
                "--silent",
                "--max-time",
                "10",
                arguments.collector_metrics_url,
            ]
        )
    )
    python_api = _json_source(
        _run(
            [
                arguments.podman_bin,
                "exec",
                arguments.python_api_container,
                "python",
                "-m",
                "clashlens.cli",
                "probe",
                "--url",
                "http://127.0.0.1:8000/operatorz",
                "--caller",
                arguments.api_hmac_caller,
                "--key-id",
                arguments.api_hmac_key_id,
                "--secret-file",
                arguments.api_hmac_secret_file,
                "--timeout-seconds",
                "10",
            ]
        )
    )
    workers = [
        _json_source(
            _run(
                [
                    arguments.podman_bin,
                    "exec",
                    f"{arguments.python_worker_container}-{replica}",
                    "cat",
                    "/tmp/clashlens-worker-operating.json",
                ]
            )
        )
        for replica in range(1, arguments.worker_replicas + 1)
    ]
    # Capture PostgreSQL last so its clock bounds every cached worker snapshot.
    database = _json_source(
        _run(
            [
                arguments.podman_bin,
                "exec",
                "--interactive",
                arguments.postgres_container,
                "psql",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
                "--username",
                arguments.postgres_user,
                "--dbname",
                arguments.postgres_database,
            ],
            input_text=DATABASE_SQL,
        )
    )
    return build_operating_snapshot(
        database=database,
        collector=collector,
        python_api=python_api,
        python_workers=workers,
        spool_config={
            "max_body_bytes": arguments.spool_max_body_bytes,
            "max_bytes": arguments.spool_max_bytes,
            "max_objects": arguments.spool_max_objects,
            "free_space_floor": arguments.spool_free_space_floor,
            "free_inode_floor": arguments.spool_free_inode_floor,
        },
        previous=_previous(arguments.previous_snapshot),
    )


def _indeterminate() -> dict[str, Any]:
    return build_operating_snapshot(
        database={},
        collector={},
        python_api={},
        python_workers=[],
        spool_config={},
    )


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--podman-bin", default="podman")
    parser.add_argument("--curl-bin", default="curl")
    parser.add_argument("--postgres-container", required=True)
    parser.add_argument("--postgres-user", required=True)
    parser.add_argument("--postgres-database", required=True)
    parser.add_argument("--collector-metrics-url", required=True)
    parser.add_argument("--python-api-container", required=True)
    parser.add_argument("--api-hmac-caller", required=True)
    parser.add_argument("--api-hmac-key-id", required=True)
    parser.add_argument("--api-hmac-secret-file", required=True)
    parser.add_argument("--python-worker-container", required=True)
    parser.add_argument("--worker-replicas", type=int, required=True)
    parser.add_argument("--spool-max-body-bytes", type=int, required=True)
    parser.add_argument("--spool-max-bytes", type=int, required=True)
    parser.add_argument("--spool-max-objects", type=int, required=True)
    parser.add_argument("--spool-free-space-floor", type=int, required=True)
    parser.add_argument("--spool-free-inode-floor", type=int, required=True)
    parser.add_argument("--previous-snapshot", type=Path)
    arguments = parser.parse_args(argv)
    positive = (
        arguments.spool_max_body_bytes,
        arguments.spool_max_bytes,
        arguments.spool_max_objects,
    )
    nonnegative = (
        arguments.spool_free_space_floor,
        arguments.spool_free_inode_floor,
    )
    if (
        not 1 <= arguments.worker_replicas <= WORKER_LIMIT
        or any(value <= 0 for value in positive)
        or any(value < 0 for value in nonnegative)
    ):
        parser.error(
            "operating capacities must be positive, floors nonnegative, "
            "and worker replicas at most 16"
        )
    return arguments


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        result = collect_snapshot(arguments)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        result = _indeterminate()
    print(json.dumps(result, indent=2, sort_keys=True))
    return int(result["check"]["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
