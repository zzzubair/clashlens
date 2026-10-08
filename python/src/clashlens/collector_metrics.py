"""Collector health queries, sampled by the metrics endpoint."""

from typing import Any


def health_metrics(connection: Any) -> dict[str, int | float]:
    row = connection.execute(
        """WITH active_reset AS (SELECT sweep.id FROM collector_reset_sweeps AS sweep JOIN collector_work AS work ON work.sweep_id = sweep.id WHERE work.kind = 'reset_baseline' AND work.status IN ('pending', 'waiting_retry') ORDER BY sweep.boundary_at DESC, sweep.id DESC LIMIT 1),
        processing AS (
            SELECT job.work_type, count(*) AS pending_count,
                   -- Reset readings, ended-day results and board builds since the latest Reset.
                   count(*) FILTER (WHERE job.priority >= 300 AND job.created_at >= date_bin(
                       interval '1 day', statement_timestamp(), timestamptz '2000-01-01 05:00:00+00')) AS reset_count,
                   greatest(0, extract(epoch FROM clock_timestamp()
                       - min(CASE WHEN job.status = 'pending'
                                  THEN greatest(COALESCE(observation.created_at, job.created_at), job.due_at)
                                  ELSE COALESCE(observation.created_at, job.created_at) END)
                         FILTER (WHERE job.status <> 'pending' OR job.due_at <= clock_timestamp()))) AS age,
                   -- Due and free to claim, so not one that is running or waits on another.
                   greatest(0, extract(epoch FROM clock_timestamp()
                       - min(greatest(COALESCE(observation.created_at, job.created_at), job.due_at))
                         FILTER (WHERE (job.status IN ('pending', 'waiting_retry')
                                        AND job.due_at <= clock_timestamp())
                                    OR (job.status = 'leased'
                                        AND job.lease_expires_at < clock_timestamp())))) AS claimable_age
            FROM python_processing_jobs AS job
            LEFT JOIN collector_observations AS observation
              ON observation.id = job.observation_id
            WHERE job.status IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased')
            GROUP BY job.work_type
        ), failed_jobs AS (
            SELECT count(*) AS failed_count, max(updated_at) AS newest_at, min(updated_at) AS oldest_at
            FROM python_processing_jobs WHERE status = 'failed'
        ), completed AS (
            -- Through the finished-job cleanup lookup: about 1,100 rows on 8 Oct 2026, 11 ms.
            SELECT work_type, count(*) AS completed_count FROM python_processing_jobs
            WHERE status = 'complete' AND updated_at > statement_timestamp() - interval '2 minutes'
            GROUP BY work_type
        ), uploads AS (
            SELECT count(*) AS pending_count, min(created_at) AS oldest_at
            FROM collector_response_uploads
            WHERE state IN ('pending', 'leased')
               OR (state = 'failed' AND next_attempt_at < 'infinity'::timestamptz)
        ), failed_uploads AS (
            SELECT count(*) AS failed_count, max(updated_at) AS newest_at, min(updated_at) AS oldest_at
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
               (SELECT COALESCE(sum(pending_count), 0) FROM processing),
               (SELECT pending_count FROM uploads),
               (SELECT failed_count FROM failed_jobs),
               (SELECT failed_count FROM failed_uploads),
               (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline'),
               (SELECT count(*) FROM collector_work WHERE sweep_id = (SELECT id FROM active_reset) AND kind = 'reset_baseline' AND status IN ('complete', 'failed', 'cancelled')),
               (SELECT CASE WHEN max(last_success_at) IS NULL THEN NULL ELSE greatest(0, extract(epoch FROM clock_timestamp() - max(last_success_at))) END
                FROM collector_response_state),
               -- Publication builds often run for most of an hour, so they are left out.
               COALESCE((SELECT max(age) FROM processing WHERE NOT starts_with(work_type, 'build_')), 0),
               COALESCE((SELECT greatest(0, extract(epoch FROM clock_timestamp() - oldest_at)) FROM uploads), 0),
               (SELECT CASE WHEN newest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - newest_at)) END FROM failed_jobs),
               (SELECT CASE WHEN newest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - newest_at)) END FROM failed_uploads),
               extract(epoch FROM statement_timestamp()),
               -- Responses saved in the minute before this sample, up to 1,000. A
               -- rolled-back save leaves no row; no index covers created_at, so
               -- only the newest rows by id are read.
               (SELECT count(*) FROM (SELECT created_at FROM collector_observations ORDER BY id DESC LIMIT 1000) AS newest
                WHERE created_at > statement_timestamp() - interval '1 minute'),
               checks.samples, checks.missing, checks.p50, checks.p95, checks.maximum,
               (SELECT COALESCE(sum(reset_count), 0) FROM processing),
               (SELECT CASE WHEN oldest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - oldest_at)) END FROM failed_jobs),
               (SELECT CASE WHEN oldest_at IS NOT NULL THEN greatest(0, extract(epoch FROM clock_timestamp() - oldest_at)) END FROM failed_uploads),
               (SELECT COALESCE(sum(completed_count), 0) FROM completed),
               (SELECT json_object_agg(work_type, age) FROM processing),
               (SELECT json_object_agg(work_type, claimable_age) FROM processing),
               (SELECT json_object_agg(work_type, completed_count) FROM completed)
        FROM checks"""
    ).fetchone()
    assert row is not None
    *row, ages, claimable, completed = row
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
        "responses_saved_last_minute",
        "check_age_sample_players",
        "check_age_missing_players",
        "check_age_p50_seconds",
        "check_age_p95_seconds",
        "check_age_max_seconds",
        "reset_work_remaining",
        "oldest_failed_processing_age_seconds",
        "oldest_failed_upload_age_seconds",
        "completed_jobs_2m",
    )
    metrics: dict[str, int | float] = {}
    for name, value in zip(names, row, strict=True):
        if value is not None:
            metrics[name] = float(value) if name.endswith("_seconds") else int(value)
    for work_type, age in (ages or {}).items():
        metrics[f"oldest_job_{work_type}_age_seconds"] = float(age)
    for work_type, age in (claimable or {}).items():
        metrics[f"claimable_job_{work_type}_age_seconds"] = float(age)
    for work_type, count in (completed or {}).items():
        metrics[f"completed_job_{work_type}_2m"] = int(count)
    return metrics
