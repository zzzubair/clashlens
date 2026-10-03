-- Finished processing jobs grow about 650,000 rows (0.84 GB with their
-- attempts) a day and nothing removed them. A timer now deletes them 48 hours
-- after they finish. It connects with its own NOLOGIN role; ./ops gives that
-- role a fresh password and login on every production `up`. The role can only
-- call one owner-run function, so it cannot read or change anything else.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clashlens_history_retention') THEN
        CREATE ROLE clashlens_history_retention NOLOGIN;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO clashlens_history_retention', current_schema());
END
$$;
ALTER ROLE clashlens_history_retention
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

-- Deleting an attempt clears its id on processing outcomes and profile
-- effects. Without these lookups each deleted attempt reads both whole tables.
-- Only rows whose attempt still exists are listed: about two days of rows once
-- cleanup has caught up. `up` builds them while services are stopped.
CREATE INDEX IF NOT EXISTS observation_processing_outcomes_attempt
    ON observation_processing_outcomes (attempt_id) WHERE attempt_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS player_profile_effects_attempt
    ON player_profile_effects (attempt_id) WHERE attempt_id IS NOT NULL;

-- One batch: the oldest finished jobs past the retention window, skipping any
-- row another transaction holds. Their attempts, events and replay requests go
-- with them. Pending, leased, waiting, failed and cancelled jobs, exports and
-- legacy publication anchors are never selected.
CREATE OR REPLACE FUNCTION clashlens_prune_finished_jobs(
    retention_hours integer, max_jobs integer, apply_changes boolean,
    OUT eligible integer, OUT deleted integer
)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    batch bigint[];
BEGIN
    IF retention_hours IS NULL OR retention_hours NOT BETWEEN 48 AND 672
       OR max_jobs IS NULL OR max_jobs NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'finished-job cleanup needs 48-672 hours and 1-1000 jobs';
    END IF;
    SELECT array_agg(due.id) INTO batch FROM (
        SELECT target.id FROM python_processing_jobs AS target
        WHERE target.status = 'complete'
          AND target.work_type <> 'build_export'
          AND target.updated_at < clock_timestamp() - make_interval(hours => retention_hours)
          AND NOT EXISTS (SELECT 1 FROM boundary_publication_legacy_job_migrations
                          WHERE job_id = target.id)
        ORDER BY target.updated_at, target.id
        LIMIT max_jobs
        FOR UPDATE OF target SKIP LOCKED
    ) AS due;
    eligible := coalesce(cardinality(batch), 0);
    deleted := 0;
    IF apply_changes AND eligible > 0 THEN
        DELETE FROM python_processing_jobs WHERE id = ANY(batch);
        GET DIAGNOSTICS deleted = ROW_COUNT;
    END IF;
END
$$;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_prune_finished_jobs(integer, integer, boolean) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_prune_finished_jobs(integer, integer, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clashlens_prune_finished_jobs(integer, integer, boolean)
    TO clashlens_history_retention;

INSERT INTO clash_lens_schema_migrations(version) VALUES (53)
ON CONFLICT (version) DO NOTHING;
COMMIT;
