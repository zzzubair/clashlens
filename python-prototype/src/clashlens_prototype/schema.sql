-- ISSUE 29 PYTHON-LAYER PROTOTYPE ONLY.
-- This file is a test/learning contract. It is not migration 0001 or a production migration.
-- The production v1 -> v2 migration and collector bridge remain open work.

CREATE TABLE IF NOT EXISTS clash_lens_contract (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL
);
INSERT INTO clash_lens_contract (singleton, version)
VALUES (true, 2)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS collector_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurrence_key text NOT NULL UNIQUE,
    normalized_tag text NOT NULL,
    endpoint text NOT NULL CHECK (endpoint = 'profile'),
    endpoint_version text NOT NULL,
    schema_version text NOT NULL,
    request_started_at timestamptz NOT NULL,
    response_observed_at timestamptz NOT NULL,
    http_status integer NOT NULL,
    response_hash text NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    archive_reference text NOT NULL,
    collector_version text NOT NULL,
    evidence_headers jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS python_processing_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL UNIQUE REFERENCES collector_observations (id),
    work_type text NOT NULL CHECK (work_type = 'process_observation'),
    state text NOT NULL CHECK (state IN (
        'pending', 'leased', 'waiting_retry', 'complete', 'failed', 'cancelled'
    )),
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    parser_version text NOT NULL,
    processing_version text NOT NULL,
    lease_owner text,
    lease_token text,
    lease_expires_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL DEFAULT 2 CHECK (max_attempts > 0),
    outcome text,
    failure_category text,
    failure_detail text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (state = 'leased' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR state <> 'leased'
    )
);
-- PostgreSQL has no useful priority in this narrow contract. Keep the claim index
-- explicit without adding a product field that the prototype does not need.
CREATE INDEX IF NOT EXISTS python_processing_jobs_claim_order
    ON python_processing_jobs (state, due_at, id);

CREATE TABLE IF NOT EXISTS python_processing_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES python_processing_jobs (id),
    attempt_number integer NOT NULL,
    lease_owner text NOT NULL,
    lease_token text NOT NULL,
    started_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    completed_at timestamptz,
    state text NOT NULL CHECK (state IN ('running', 'waiting_retry', 'complete', 'failed', 'stale')),
    outcome text,
    failure_category text,
    UNIQUE (job_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS python_processing_attempts_job_order
    ON python_processing_attempts (job_id, attempt_number);

CREATE TABLE IF NOT EXISTS players (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_tag text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT false,
    eligibility_state text NOT NULL DEFAULT 'unknown' CHECK (
        eligibility_state IN ('unknown', 'eligible', 'ineligible', 'uncertain')
    ),
    current_profile_version_id bigint,
    current_observed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS player_profile_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    normalized_tag text NOT NULL,
    endpoint_version text NOT NULL,
    schema_version text NOT NULL,
    parser_version text NOT NULL,
    observed_at timestamptz NOT NULL,
    source_http_status integer NOT NULL,
    name text NOT NULL,
    trophies integer NOT NULL,
    league_tier_id bigint NOT NULL,
    league_tier_name text NOT NULL,
    eligibility_state text NOT NULL CHECK (
        eligibility_state IN ('eligible', 'ineligible', 'uncertain')
    ),
    profile_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, parser_version)
);
CREATE INDEX IF NOT EXISTS player_profile_versions_tag_time
    ON player_profile_versions (normalized_tag, observed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS player_profile_effects (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_version_id bigint NOT NULL REFERENCES player_profile_versions (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    effect_kind text NOT NULL CHECK (effect_kind = 'current_profile'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, effect_kind)
);
