-- Clash Lens deployment migration 0003.
-- Enforce one active regular poll per player.
--
-- The scheduler coalescing key embeds the cycle timestamp, so a backed-up
-- queue accumulates one active regular_poll job per player per five-minute
-- cycle. This migration cancels the older duplicates (keeping the newest job
-- per player) and installs a partial unique index that makes the
-- one-active-job-per-player rule a database contract. Terminal history is
-- preserved: superseded rows stay visible as cancelled work with their lease
-- fields cleared, exactly like the claim-path cancellation patterns.

BEGIN;

-- Exclude concurrent scheduler inserts and lease updates while the duplicate
-- set is resolved and the unique index is built. SHARE ROW EXCLUSIVE blocks
-- row-changing transactions but leaves readers running. The scheduler never
-- locks collector_jobs before players, so no lock cycle can form.
LOCK TABLE collector_jobs IN SHARE ROW EXCLUSIVE MODE;

-- Identify every active regular_poll job except the newest per player. The
-- temporary sets let the migration terminalize only unfinished attempts and
-- endpoint states before it cancels their jobs. Observed endpoint evidence
-- and every historical row remain unchanged.
CREATE TEMP TABLE superseded_regular_poll_jobs ON COMMIT DROP AS
SELECT id
FROM (
    SELECT id,
           row_number() OVER (PARTITION BY player_id ORDER BY id DESC) AS rank
    FROM collector_jobs
    WHERE work_type = 'regular_poll'
      AND player_id IS NOT NULL
      AND status IN ('pending', 'leased', 'waiting_retry')
) AS ranked
WHERE ranked.rank > 1;

CREATE TEMP TABLE superseded_regular_poll_attempts ON COMMIT DROP AS
SELECT attempt.id
FROM collector_attempts AS attempt
JOIN superseded_regular_poll_jobs AS job ON job.id = attempt.job_id
WHERE attempt.status IN ('running', 'incomplete');

UPDATE collector_endpoint_results AS result
SET outcome = 'failed',
    failure_category = COALESCE(result.failure_category, 'regular_poll_superseded'),
    execution_token = NULL,
    next_retry_at = NULL
FROM superseded_regular_poll_attempts AS attempt
WHERE result.attempt_id = attempt.id
  AND result.outcome IN ('pending', 'retrying');

UPDATE collector_attempts AS attempt
SET status = 'failed',
    completed_at = COALESCE(attempt.completed_at, clock_timestamp()),
    failure_category = COALESCE(attempt.failure_category, 'regular_poll_superseded')
FROM superseded_regular_poll_attempts AS superseded
WHERE attempt.id = superseded.id;

-- The update is idempotent: after the first run no player has more than one
-- active job, so a reapply touches nothing.
UPDATE collector_jobs AS job
SET status = 'cancelled',
    cancel_reason = 'superseded by newer active regular poll',
    lease_owner = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = clock_timestamp()
FROM superseded_regular_poll_jobs AS superseded
WHERE job.id = superseded.id;

CREATE UNIQUE INDEX IF NOT EXISTS collector_jobs_one_active_regular_poll_per_player
    ON collector_jobs (player_id)
    WHERE work_type = 'regular_poll'
      AND player_id IS NOT NULL
      AND status IN ('pending', 'leased', 'waiting_retry');

-- The collector-owned contract stays at version two. Version three would be
-- rejected by the running bridge, and this migration adds no contract that
-- the collector code must negotiate.
INSERT INTO clash_lens_schema_migrations (version)
VALUES (3)
ON CONFLICT (version) DO NOTHING;

COMMIT;
