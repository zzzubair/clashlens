"""Collector health queries, sampled by the metrics endpoint."""

from typing import Any


def health_metrics(connection: Any) -> dict[str, int | float]:
    row = connection.execute(
        """WITH active_reset AS (SELECT sweep.id FROM collector_reset_sweeps AS sweep JOIN collector_work AS work ON work.sweep_id = sweep.id WHERE work.kind = 'reset_baseline' AND work.status NOT IN ('complete', 'failed', 'cancelled') ORDER BY sweep.boundary_at DESC, sweep.id DESC LIMIT 1),
        processing AS (
            SELECT count(*) AS pending_count,
                   min(COALESCE(observation.created_at, job.created_at)) AS oldest_saved_at
            FROM python_processing_jobs AS job
            LEFT JOIN collector_observations AS observation
              ON observation.id = job.observation_id
            WHERE job.status IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased')
        ), failed_jobs AS (
            SELECT count(*) AS failed_count, max(updated_at) AS newest_at
            FROM python_processing_jobs WHERE status = 'failed'
        ), uploads AS (
            SELECT count(*) AS pending_count, min(created_at) AS oldest_at
            FROM collector_response_uploads
            WHERE state IN ('pending', 'leased')
               OR (state = 'failed' AND next_attempt_at < 'infinity'::timestamptz)
        ), failed_uploads AS (
            SELECT count(*) AS failed_count, max(updated_at) AS newest_at
            FROM collector_response_uploads
            WHERE state = 'failed' AND next_attempt_at = 'infinity'::timestamptz
        ), check_ages AS (
            -- Profile checks only: regular checks skip the battle log on purpose.
            SELECT CASE WHEN profile.last_success_at IS NOT NULL
                        THEN greatest(0, extract(epoch FROM statement_timestamp()
                             - profile.last_success_at))
                   END AS age
            FROM players AS player
            LEFT JOIN collector_response_state AS profile
              ON profile.scope = 'player' AND profile.identity_key = player.normalized_tag
             AND profile.endpoint = 'profile'
            WHERE player.active
              AND NOT (
                  profile.last_not_found_at IS NOT NULL
                  AND (profile.last_success_at IS NULL
                       OR profile.last_not_found_at > profile.last_success_at)
              )
        ), checks AS (
            SELECT count(age) AS samples, count(*) - count(age) AS missing,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY age) AS p50,
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY age) AS p95,
                   max(age) AS maximum
            FROM check_ages
        )
        SELECT (SELECT count(*) FROM players WHERE active = true),
               (SELECT count(*) FROM players WHERE active = true AND next_due_at <= clock_timestamp()),
               COALESCE((SELECT greatest(0, extract(epoch FROM clock_timestamp() - min(next_due_at))) FROM players WHERE active = true AND next_due_at <= clock_timestamp()), 0),
               (SELECT pending_count FROM processing),
               (SELECT pending_count FROM uploads),
               (SELECT failed_count FROM failed_jobs),
               (SELECT failed_count FROM failed_uploads),
               (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline'),
               (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline' AND status IN ('complete', 'failed', 'cancelled')),
               (SELECT CASE WHEN max(last_success_at) IS NULL THEN NULL ELSE greatest(0, extract(epoch FROM clock_timestamp() - max(last_success_at))) END
                FROM collector_response_state),
               COALESCE((SELECT greatest(0, extract(epoch FROM clock_timestamp() - oldest_saved_at)) FROM processing), 0),
               COALESCE((SELECT greatest(0, extract(epoch FROM clock_timestamp() - oldest_at)) FROM uploads), 0),
               (SELECT CASE WHEN newest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - newest_at)) END FROM failed_jobs),
               (SELECT CASE WHEN newest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - newest_at)) END FROM failed_uploads),
               extract(epoch FROM statement_timestamp()),
               checks.samples, checks.missing, checks.p50, checks.p95, checks.maximum
        FROM checks"""
    ).fetchone()
    assert row is not None
    names = (
        "active_players",
        "due_queue_depth",
        "oldest_due_age_seconds",
        "pending_processing",
        "pending_uploads",
        "failed_processing",
        "failed_uploads",
        "reset_total",
        "reset_terminal",
        "last_success_age_seconds",
        "oldest_pending_processing_age_seconds",
        "oldest_pending_upload_age_seconds",
        "newest_failed_processing_age_seconds",
        "newest_failed_upload_age_seconds",
        "metrics_sample_timestamp_seconds",
        "check_age_sample_players",
        "check_age_missing_players",
        "check_age_p50_seconds",
        "check_age_p95_seconds",
        "check_age_max_seconds",
    )
    metrics: dict[str, int | float] = {}
    for name, value in zip(names, row, strict=True):
        if value is not None:
            metrics[name] = float(value) if name.endswith("_seconds") else int(value)
    return metrics
