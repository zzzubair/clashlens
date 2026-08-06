-- Standalone Python-layer schema fixture for prototype-only local tests.
-- Production tests and deployment use deploy/migrations/0001_collector.sql and
-- deploy/migrations/0002_python_layer.sql. Do not use this fixture in production.

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
    normalized_tag text,
    endpoint text NOT NULL CHECK (endpoint IN ('profile', 'battle_log', 'global_player_rankings')),
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
    deduplication_key text GENERATED ALWAYS AS ('process-observation:' || observation_id::text) STORED,
    input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL CHECK (state IN (
        'pending', 'leased', 'waiting_retry', 'complete', 'failed', 'cancelled'
    )),
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    parser_version text NOT NULL DEFAULT 'supercell-source-parser-v1',
    processing_version text NOT NULL DEFAULT 'clashlens-domain-processing-v1',
    domain_rule_version text NOT NULL DEFAULT 'clashlens-domain-rules-v1',
    analytics_rule_version text NOT NULL DEFAULT 'legend-analytics-v1',
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
    next_due_at timestamptz,
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
    current_league_season_id text,
    previous_league_season_id text,
    eligibility_reason text NOT NULL DEFAULT 'legacy_unknown',
    source_contract_state text NOT NULL DEFAULT 'accepted',
    season_anchor_state text NOT NULL DEFAULT 'conflict',
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

CREATE TABLE IF NOT EXISTS observation_processing_outcomes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    parser_version text NOT NULL,
    processing_version text NOT NULL,
    endpoint text NOT NULL,
    response_hash text NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    source_http_status integer NOT NULL,
    source_observed_at timestamptz NOT NULL,
    outcome text NOT NULL CHECK (outcome IN (
        'processed', 'processed_with_gaps', 'non_success', 'malformed',
        'unsupported', 'integrity_failure'
    )),
    failure_category text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, parser_version, processing_version)
);

CREATE TABLE IF NOT EXISTS parsed_source_payloads (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint text NOT NULL,
    response_hash text NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    parser_version text NOT NULL,
    schema_version text NOT NULL,
    parse_outcome text NOT NULL CHECK (parse_outcome IN ('valid', 'valid_with_gaps')),
    parsed_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (endpoint, response_hash, parser_version)
);

CREATE TABLE IF NOT EXISTS season_anchor_evidence (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_version_id bigint NOT NULL UNIQUE REFERENCES player_profile_versions (id),
    current_league_season_id text,
    previous_league_season_id text,
    current_start timestamptz,
    previous_start timestamptz,
    anchor_rule_version text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN ('accepted', 'conflict', 'not_applicable')),
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS legend_season_anchors (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    current_league_season_id text NOT NULL,
    previous_league_season_id text NOT NULL,
    current_start timestamptz NOT NULL,
    previous_start timestamptz NOT NULL,
    anchor_rule_version text NOT NULL,
    source_profile_version_id bigint NOT NULL REFERENCES player_profile_versions (id),
    state text NOT NULL CHECK (state IN ('confirmed', 'superseded')),
    confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (current_league_season_id, anchor_rule_version)
);
CREATE UNIQUE INDEX IF NOT EXISTS legend_season_anchors_one_confirmed
    ON legend_season_anchors (anchor_rule_version)
    WHERE state = 'confirmed';
