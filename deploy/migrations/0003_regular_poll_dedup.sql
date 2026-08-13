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

-- Stage attribution remains available after the load test: PostgreSQL starts
-- with pg_stat_statements preloaded, and this database installs its views and
-- functions through the forward migration. I/O and WAL timing are container
-- settings because they must be active before workload measurement begins.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

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

-- The scheduler's bounded due-player probe must not walk the full tracked
-- population once most players have been staggered into a future cycle.
CREATE INDEX IF NOT EXISTS players_due_regular_poll_v2
    ON players (next_due_at, id) INCLUDE (normalized_tag)
    WHERE active AND next_due_at IS NOT NULL;

-- The current worker compatibility marker keeps unsupported or future jobs
-- out of the ordered claim indexes. Source compatibility includes the joined
-- observation contract, so a future endpoint/schema backlog cannot sit at the
-- head of the current worker's partial index. The Python contract module still
-- performs the authoritative validation after claim; this stored marker is
-- only the schema-specific planner seam. A future worker contract must advance
-- it in a forward migration before its image is admitted by the deployment
-- ledger.
ALTER TABLE python_processing_jobs
    ADD COLUMN IF NOT EXISTS claim_compatibility_version integer
    DEFAULT 0;

CREATE OR REPLACE FUNCTION clashlens_set_python_claim_compatibility_v3()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.claim_compatibility_version := CASE
        WHEN NEW.processing_version = 'clashlens-domain-processing-v1'
         AND NEW.domain_rule_version = 'clashlens-domain-rules-v1'
         AND (
            (NEW.work_type IN ('process_observation', 'replay_observation')
                AND NEW.parser_version IN (
                    'supercell-source-parser-v1',
                    'supercell-source-parser-v2'
                )
                AND EXISTS (
                    SELECT 1
                    FROM collector_observations AS observation
                    WHERE observation.id = COALESCE(
                        NEW.observation_id, NEW.replay_observation_id
                    )
                      AND (
                        (observation.endpoint = 'profile'
                            AND observation.endpoint_version = 'profile-v1'
                            AND observation.schema_version = 'profile-schema-v1')
                        OR (observation.endpoint = 'battle_log'
                            AND observation.endpoint_version = 'battle-log-v1'
                            AND observation.schema_version = 'battle-log-schema-v1')
                        OR (observation.endpoint = 'global_player_rankings'
                            AND observation.endpoint_version = 'global-player-rankings-v1'
                            AND observation.schema_version = 'global-player-rankings-schema-v1')
                      )
                ))
            OR (NEW.work_type = 'reconcile_ranked_day'
                AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_snapshot'
                AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_analytics'
                AND NEW.analytics_rule_version = 'legend-analytics-v1'
                AND NEW.input_json ? 'snapshot_id'
                AND NEW.input_json ? 'snapshot_version'
                AND NEW.input_json ? 'snapshot_input_hash'
                AND NEW.input_json ? 'source_ranked_day_version_id'
                AND (NEW.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'source_ranked_day_version_id')
                    ~ '^[1-9][0-9]*$'
                AND length(NEW.input_json->>'snapshot_input_hash') > 0)
         )
        THEN 1
        ELSE 0
    END;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS python_processing_jobs_claim_compatibility_v3
    ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_claim_compatibility_v3
BEFORE INSERT OR UPDATE OF
    observation_id, replay_observation_id, work_type, parser_version,
    processing_version, domain_rule_version, analytics_rule_version, input_json,
    claim_compatibility_version
ON python_processing_jobs
FOR EACH ROW
EXECUTE FUNCTION clashlens_set_python_claim_compatibility_v3();

REVOKE ALL ON FUNCTION clashlens_set_python_claim_compatibility_v3()
    FROM PUBLIC, clashlens_collector, clashlens_python_worker,
         clashlens_python_api;

-- Backfill through the trigger so migrated work uses the same rule as new
-- collector/replay inserts. The column becomes non-null only after every row
-- is classified.
UPDATE python_processing_jobs
SET claim_compatibility_version = 0;
ALTER TABLE python_processing_jobs
    ALTER COLUMN claim_compatibility_version SET NOT NULL;

CREATE OR REPLACE VIEW python_processing_jobs_worker AS
SELECT
    id,
    observation_id,
    replay_observation_id,
    work_type,
    deduplication_key,
    input_json,
    priority,
    status AS state,
    due_at,
    parser_version,
    processing_version,
    domain_rule_version,
    snapshot_rule_version,
    analytics_rule_version,
    export_schema_version,
    lease_owner,
    lease_token,
    lease_expires_at,
    lease_generation,
    attempt_count,
    max_attempts,
    outcome,
    failure_category,
    failure_detail,
    last_error,
    created_at,
    updated_at,
    completed_at,
    claim_compatibility_version
FROM python_processing_jobs;

-- Bounded queue probes for the collector claim statement. These cover the
-- declared priorities, expired leases, and the normally-empty unknown-
-- priority fallback without walking the whole queue.
CREATE INDEX IF NOT EXISTS collector_jobs_claim_order_v2
    ON collector_jobs (capacity_pool, status, priority, due_at, created_at, id);
CREATE INDEX IF NOT EXISTS collector_jobs_expired_recovery_v2
    ON collector_jobs (capacity_pool, lease_expires_at, id)
    WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS collector_jobs_expired_claim_v2
    ON collector_jobs (capacity_pool, lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased' AND result_attempt_id IS NULL;
CREATE INDEX IF NOT EXISTS collector_jobs_unknown_priority_v2
    ON collector_jobs (capacity_pool, due_at, created_at, id, priority)
    WHERE status = 'pending' AND priority NOT IN (100, 150, 200, 250, 300, 400);

-- Equivalent bounded probes for the Python processing queue.
CREATE INDEX IF NOT EXISTS python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending', 'waiting_retry')
      AND claim_compatibility_version = 1
      AND attempt_count < max_attempts;
CREATE INDEX IF NOT EXISTS python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased'
      AND claim_compatibility_version = 1
      AND attempt_count < max_attempts;
CREATE INDEX IF NOT EXISTS python_processing_jobs_expired_maintenance_v2
    ON python_processing_jobs (lease_expires_at, id)
    WHERE status = 'leased';
CREATE INDEX IF NOT EXISTS python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending', 'waiting_retry')
      AND claim_compatibility_version = 1
      AND attempt_count < max_attempts
      AND priority NOT IN (100);

-- Reset-baseline evidence refresh locks its immutable collector-owned sweep
-- through one narrow worker-only seam. Keep the prior image's temporary
-- UPDATE(id) grant for this release so the forward migration remains rollback
-- compatible; a later migration may revoke it after that image leaves the
-- supported rollback window.
CREATE OR REPLACE FUNCTION clashlens_lock_reset_baseline_v2(baseline_id bigint)
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM 1
    FROM collector_reset_baseline_sweeps
    WHERE id = baseline_id
    FOR UPDATE;
    RETURN FOUND;
END
$$;

GRANT UPDATE (id) ON TABLE collector_reset_baseline_sweeps
    TO clashlens_python_worker;
ALTER FUNCTION clashlens_lock_reset_baseline_v2(bigint) SECURITY DEFINER;
DO $$
DECLARE
    lock_schema_name text := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_lock_reset_baseline_v2(bigint) SET search_path TO pg_catalog, %I',
        lock_schema_name, lock_schema_name
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_lock_reset_baseline_v2(bigint)
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_lock_reset_baseline_v2(bigint)
    TO clashlens_python_worker;
REVOKE ALL ON FUNCTION clashlens_reset_job_lineage_v2(bigint, bigint)
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_reset_job_lineage_v2(bigint, bigint)
    TO clashlens_python_worker;

-- Account-row deletion is an API concern and remains separate from the
-- worker's persistence privileges.
GRANT DELETE ON TABLE
    account_saved_players,
    account_groups,
    account_group_players
    TO clashlens_python_api;

-- The collector-owned contract stays at version two. Version three would be
-- rejected by the running bridge, and this migration adds no contract that
-- the collector code must negotiate.
INSERT INTO clash_lens_schema_migrations (version)
VALUES (3)
ON CONFLICT (version) DO NOTHING;

COMMIT;
