-- Verified raw-evidence catalogue, archive instance contract, and pending handoff.
-- Contract v3 deliberately requires stop -> migrate -> restart: v2 collectors
-- must not publish observations without a catalogue row.
BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE current_version integer;
BEGIN
    SELECT version INTO current_version FROM clash_lens_contract WHERE singleton;
    IF current_version <> 2 THEN
        RAISE EXCEPTION 'raw-evidence migration requires contract version 2 (got %)', current_version;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS archive_instances (
    instance_id text PRIMARY KEY CHECK (instance_id <> ''),
    endpoint text NOT NULL CHECK (endpoint <> ''),
    region text NOT NULL CHECK (region <> ''),
    bucket text NOT NULL CHECK (bucket <> ''),
    marker_key text NOT NULL CHECK (marker_key <> ''),
    marker_hash text NOT NULL CHECK (marker_hash ~ '^[0-9a-f]{64}$'),
    marker_payload_version text NOT NULL CHECK (marker_payload_version <> ''),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS archive_catalogue (
    response_hash text PRIMARY KEY CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    archive_reference text NOT NULL UNIQUE CHECK (archive_reference <> ''),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    archive_instance_id text NOT NULL REFERENCES archive_instances(instance_id),
    first_verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (response_hash, archive_reference)
);

ALTER TABLE collector_observations
    ADD COLUMN IF NOT EXISTS archive_catalogue_hash text;

ALTER TABLE collector_endpoint_results
    ADD COLUMN IF NOT EXISTS pending_remote_verification jsonb;

ALTER TABLE collector_endpoint_results
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_pending_remote_check;
ALTER TABLE collector_endpoint_results
    ADD CONSTRAINT collector_endpoint_results_pending_remote_check CHECK (
        pending_remote_verification IS NULL OR (
            jsonb_typeof(pending_remote_verification) = 'object'
            AND pending_remote_verification ? 'response_hash'
            AND pending_remote_verification ? 'archive_reference'
            AND pending_remote_verification ? 'byte_size'
            AND NOT (pending_remote_verification ? 'body')
            AND NOT (pending_remote_verification ? 'authorization')
            AND NOT (pending_remote_verification ? 'credentials')
        )
    );

-- NOT VALID preserves readable legacy rows while rejecting every new
-- uncatalogued observation after this migration.
ALTER TABLE collector_observations
    DROP CONSTRAINT IF EXISTS collector_observations_catalogue_required_v3,
    DROP CONSTRAINT IF EXISTS collector_observations_catalogue_hash_v3;
ALTER TABLE collector_observations
    ADD CONSTRAINT collector_observations_catalogue_required_v3
        CHECK (archive_catalogue_hash IS NOT NULL) NOT VALID,
    ADD CONSTRAINT collector_observations_catalogue_hash_v3
        CHECK (archive_catalogue_hash = response_hash) NOT VALID;

ALTER TABLE collector_observations
    DROP CONSTRAINT IF EXISTS collector_observations_catalogue_fk_v3;
ALTER TABLE collector_observations
    ADD CONSTRAINT collector_observations_catalogue_fk_v3
    FOREIGN KEY (archive_catalogue_hash, archive_reference)
    REFERENCES archive_catalogue (response_hash, archive_reference)
    NOT VALID;

-- The collector dependency-deferral class mirrors Python's
-- waiting_dependency: archive/capacity deferrals never consume ordinary
-- retries and stay claimable after their backoff.
ALTER TABLE collector_jobs
    DROP CONSTRAINT IF EXISTS collector_jobs_status_check;
ALTER TABLE collector_jobs
    ADD CONSTRAINT collector_jobs_status_check CHECK (status IN (
        'pending', 'leased', 'waiting_retry', 'waiting_dependency',
        'complete', 'failed', 'cancelled'
    ));
ALTER TABLE python_processing_jobs
    ADD COLUMN IF NOT EXISTS dependency_deferral_count integer NOT NULL DEFAULT 0;

-- Denormalize the source contract onto the job so every claim probe is a
-- pure job-side predicate. Contract predicates against the joined
-- observation let the planner flip to sequential scans of
-- collector_observations at production queue depth.
ALTER TABLE python_processing_jobs
    ADD COLUMN IF NOT EXISTS endpoint text,
    ADD COLUMN IF NOT EXISTS endpoint_version text,
    ADD COLUMN IF NOT EXISTS schema_version text;

UPDATE python_processing_jobs AS job
SET endpoint = observation.endpoint,
    endpoint_version = observation.endpoint_version,
    schema_version = observation.schema_version
FROM collector_observations AS observation
WHERE COALESCE(job.observation_id, job.replay_observation_id) = observation.id;

CREATE OR REPLACE FUNCTION clashlens_set_python_job_source_contract()
RETURNS trigger AS $$
BEGIN
    UPDATE python_processing_jobs AS job
    SET endpoint = observation.endpoint,
        endpoint_version = observation.endpoint_version,
        schema_version = observation.schema_version
    FROM collector_observations AS observation
    WHERE job.id = NEW.id
      AND COALESCE(NEW.observation_id, NEW.replay_observation_id) = observation.id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS python_processing_jobs_set_source_contract_v3
    ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_set_source_contract_v3
AFTER INSERT OR UPDATE OF observation_id, replay_observation_id
ON python_processing_jobs
FOR EACH ROW EXECUTE FUNCTION clashlens_set_python_job_source_contract();

-- Re-publish the worker view with the new columns appended so claim probes
-- can read the denormalized contract and dependency counters. The worker
-- claims and updates through this view; it must expose the new deferral
-- counter and contract columns or the non-consuming dependency path breaks.
CREATE OR REPLACE VIEW python_processing_jobs_worker AS
SELECT id, observation_id, replay_observation_id, work_type, deduplication_key,
       input_json, priority, status AS state, due_at, parser_version,
       processing_version, domain_rule_version, snapshot_rule_version,
       analytics_rule_version, export_schema_version, lease_owner, lease_token,
       lease_expires_at, lease_generation, attempt_count, max_attempts, outcome,
       failure_category, failure_detail, last_error, created_at, updated_at,
       completed_at, claim_compatibility_version, dependency_deferral_count,
       endpoint, endpoint_version, schema_version
FROM python_processing_jobs;
ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_status_v2_check;
ALTER TABLE python_processing_jobs
    ADD CONSTRAINT python_processing_jobs_status_v3_check CHECK (status IN (
        'pending', 'leased', 'waiting_retry', 'waiting_dependency', 'complete', 'failed', 'cancelled'
    ));
ALTER TABLE collector_attempt_events
    DROP CONSTRAINT IF EXISTS collector_attempt_events_event_type_check;
ALTER TABLE collector_attempt_events
    ADD CONSTRAINT collector_attempt_events_event_type_check CHECK (event_type IN (
        'claimed', 'lease_expired', 'retry_scheduled', 'dependency_deferred',
        'completed', 'failed', 'cancelled'
    ));

ALTER TABLE python_processing_attempts
    DROP CONSTRAINT IF EXISTS python_processing_attempts_state_check;
ALTER TABLE python_processing_attempts
    ADD CONSTRAINT python_processing_attempts_state_v3_check CHECK (
        state IN ('running', 'waiting_retry', 'waiting_dependency', 'complete', 'failed', 'stale')
    );
ALTER TABLE python_processing_job_events
    DROP CONSTRAINT IF EXISTS python_processing_job_events_to_state_check;
ALTER TABLE python_processing_job_events
    ADD CONSTRAINT python_processing_job_events_to_state_v3_check CHECK (
        to_state IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency', 'complete', 'failed', 'cancelled')
    );

ALTER TABLE collector_endpoint_results
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_outcome_check,
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_outcome_v2_check,
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_outcome_v3_check,
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_pending_outcome_v3;
ALTER TABLE collector_endpoint_results
    ADD CONSTRAINT collector_endpoint_results_pending_outcome_v3 CHECK (
        pending_remote_verification IS NULL OR outcome = 'pending_remote_verification'
    ) NOT VALID;

ALTER TABLE collector_endpoint_results
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_outcome_v3_check,
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_outcome_v3;
-- The claim probes now include waiting_dependency, so the pre-v3 partial
-- indexes are recreated with the extended status list (established
-- drop-and-recreate pattern from migrations 0003/0005).
DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2 ON python_processing_jobs (priority, due_at, created_at, id) WHERE status IN ('pending','waiting_retry','waiting_dependency') AND claim_compatibility_version IN (1,2,3) AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2 ON python_processing_jobs (due_at, created_at, id, priority) WHERE status IN ('pending','waiting_retry') AND claim_compatibility_version IN (1,2,3) AND attempt_count < max_attempts AND priority NOT IN (100);
CREATE INDEX python_processing_jobs_waiting_dependency_claim_v3
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status = 'waiting_dependency' AND claim_compatibility_version IN (1,2,3);
DROP INDEX IF EXISTS collector_jobs_claim_order;
CREATE INDEX collector_jobs_claim_order
    ON collector_jobs (status, due_at, priority, created_at)
    WHERE status IN ('pending', 'waiting_dependency', 'leased', 'waiting_retry');
DROP INDEX IF EXISTS collector_jobs_one_active_coalescing_key;
CREATE UNIQUE INDEX collector_jobs_one_active_coalescing_key
    ON collector_jobs (coalescing_key)
    WHERE status IN ('pending', 'leased', 'waiting_dependency', 'waiting_retry');

-- EXPLAIN evidence at production queue depth: the Python claim SELECT's
-- per-contract observation predicates need an index path so the planner
-- never falls back to a sequential scan of collector_observations.
CREATE INDEX IF NOT EXISTS collector_observations_source_contract_v3
    ON collector_observations (endpoint, endpoint_version, schema_version);

-- Runtime identities remain least-privilege: collector writes the catalogue,
-- Python never reads it to decide evidence safety, and neither identity gets
-- object listing or deletion rights here.
GRANT SELECT ON archive_instances TO clashlens_collector;
GRANT SELECT, INSERT, UPDATE ON archive_catalogue TO clashlens_collector;
-- Python validates the immutable instance contract, but never reads the
-- evidence catalogue to decide whether response bytes are safe.
GRANT SELECT ON archive_instances TO clashlens_python_worker;
REVOKE ALL PRIVILEGES ON archive_catalogue FROM clashlens_python_worker;
ALTER TABLE collector_endpoint_results
    ADD CONSTRAINT collector_endpoint_results_outcome_v3 CHECK (
        outcome IN ('pending', 'retrying', 'observed', 'transport_failed',
                    'storage_failed', 'failed', 'pending_remote_verification')
    ) NOT VALID;

UPDATE clash_lens_contract SET version = 3 WHERE singleton;
INSERT INTO clash_lens_schema_migrations(version) VALUES (9)
ON CONFLICT (version) DO NOTHING;

COMMIT;
