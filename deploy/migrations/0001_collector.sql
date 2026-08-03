-- Clash Lens deployment migration 0001.
-- This file owns the first deployable collector schema.
-- It is not the prototype test contract in testdata/contract.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS clash_lens_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS clash_lens_contract (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL
);

INSERT INTO clash_lens_contract (singleton, version)
VALUES (true, 1)
ON CONFLICT (singleton) DO NOTHING;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM clash_lens_contract
        WHERE singleton AND version <> 1
    ) THEN
        RAISE EXCEPTION 'Clash Lens contract version is not 1';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS players (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_tag text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT false,
    next_due_at timestamptz
);

CREATE TABLE IF NOT EXISTS collector_reset_sweeps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS collector_reset_sweep_members (
    sweep_id bigint NOT NULL REFERENCES collector_reset_sweeps (id),
    player_id bigint NOT NULL REFERENCES players (id),
    PRIMARY KEY (sweep_id, player_id)
);

CREATE TABLE IF NOT EXISTS collector_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_type text NOT NULL CHECK (work_type IN (
        'regular_poll',
        'initial_collection',
        'live_refresh',
        'reset_profile',
        'endpoint_retry'
    )),
    player_id bigint REFERENCES players (id),
    normalized_tag text NOT NULL,
    capacity_pool text NOT NULL CHECK (capacity_pool IN ('normal', 'interactive')),
    priority integer NOT NULL,
    due_at timestamptz NOT NULL,
    coalescing_key text NOT NULL,
    parent_attempt_id bigint,
    required_endpoint text CHECK (required_endpoint IN ('profile', 'battle_log')),
    sweep_id bigint REFERENCES collector_reset_sweeps (id),
    status text NOT NULL CHECK (status IN ('pending', 'leased', 'waiting_retry', 'complete', 'failed', 'cancelled')),
    lease_owner text,
    lease_token text,
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    cancel_reason text,
    result_attempt_id bigint
);

CREATE UNIQUE INDEX IF NOT EXISTS collector_jobs_one_active_coalescing_key
    ON collector_jobs (coalescing_key)
    WHERE status IN ('pending', 'leased', 'waiting_retry');
CREATE INDEX IF NOT EXISTS collector_jobs_claim_order
    ON collector_jobs (status, due_at, priority, created_at);

CREATE TABLE IF NOT EXISTS collector_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES collector_jobs (id),
    status text NOT NULL CHECK (status IN ('running', 'incomplete', 'complete', 'failed')),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    UNIQUE (job_id, id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'collector_jobs'::regclass
          AND conname = 'collector_jobs_parent_attempt_fk'
    ) THEN
        ALTER TABLE collector_jobs
            ADD CONSTRAINT collector_jobs_parent_attempt_fk
            FOREIGN KEY (parent_attempt_id) REFERENCES collector_attempts (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'collector_jobs'::regclass
          AND conname = 'collector_jobs_result_attempt_fk'
    ) THEN
        ALTER TABLE collector_jobs
            ADD CONSTRAINT collector_jobs_result_attempt_fk
            FOREIGN KEY (result_attempt_id) REFERENCES collector_attempts (id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS collector_endpoint_results (
    attempt_id bigint NOT NULL REFERENCES collector_attempts (id),
    endpoint text NOT NULL CHECK (endpoint IN ('profile', 'battle_log')),
    outcome text NOT NULL CHECK (outcome IN (
        'pending',
        'retrying',
        'observed',
        'transport_failed',
        'storage_failed',
        'failed'
    )),
    request_started_at timestamptz,
    response_completed_at timestamptz,
    http_status integer,
    response_hash text,
    archive_reference text,
    observation_id bigint,
    request_count integer NOT NULL DEFAULT 0,
    execution_token text,
    retry_count integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz,
    failure_category text,
    key_label text,
    PRIMARY KEY (attempt_id, endpoint)
);

CREATE TABLE IF NOT EXISTS collector_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurrence_key text NOT NULL UNIQUE,
    collection_job_id bigint NOT NULL REFERENCES collector_jobs (id),
    attempt_id bigint NOT NULL REFERENCES collector_attempts (id),
    player_id bigint REFERENCES players (id),
    normalized_tag text NOT NULL,
    endpoint text NOT NULL CHECK (endpoint IN ('profile', 'battle_log')),
    request_started_at timestamptz NOT NULL,
    response_completed_at timestamptz NOT NULL,
    http_status integer NOT NULL,
    response_hash text NOT NULL,
    archive_reference text NOT NULL,
    collector_version text NOT NULL,
    key_label text NOT NULL,
    evidence_headers jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'collector_endpoint_results'::regclass
          AND conname = 'collector_endpoint_results_observation_fk'
    ) THEN
        ALTER TABLE collector_endpoint_results
            ADD CONSTRAINT collector_endpoint_results_observation_fk
            FOREIGN KEY (observation_id) REFERENCES collector_observations (id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS python_processing_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL UNIQUE REFERENCES collector_observations (id),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'leased', 'complete', 'failed')),
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS collector_transport_failures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_job_id bigint NOT NULL REFERENCES collector_jobs (id),
    attempt_id bigint NOT NULL REFERENCES collector_attempts (id),
    player_id bigint REFERENCES players (id),
    normalized_tag text NOT NULL,
    endpoint text NOT NULL CHECK (endpoint IN ('profile', 'battle_log')),
    request_started_at timestamptz NOT NULL,
    failed_at timestamptz NOT NULL,
    failure_category text NOT NULL,
    retry_state text NOT NULL,
    key_label text NOT NULL
);

CREATE TABLE IF NOT EXISTS collector_interactive_intent_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    requested_work_type text NOT NULL CHECK (requested_work_type IN ('initial_collection', 'live_refresh')),
    normalized_tag text NOT NULL,
    requested_at timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('created', 'coalesced', 'cooldown_hit', 'partial_retry')),
    result_job_id bigint NOT NULL REFERENCES collector_jobs (id),
    result_attempt_id bigint REFERENCES collector_attempts (id)
);

CREATE INDEX IF NOT EXISTS collector_interactive_intent_events_metrics
    ON collector_interactive_intent_events (requested_work_type, outcome, requested_at);

INSERT INTO clash_lens_schema_migrations (version)
VALUES (1)
ON CONFLICT (version) DO NOTHING;

COMMIT;
