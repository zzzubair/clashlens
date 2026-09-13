-- PROTOTYPE TEST CONTRACT ONLY. This file is not a migration.
-- The collector checks this schema version but never applies this file.

CREATE TABLE clash_lens_contract (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL
);
INSERT INTO clash_lens_contract (version) VALUES (5);

CREATE TABLE players (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_tag text NOT NULL UNIQUE,
    active boolean NOT NULL DEFAULT false,
    next_due_at timestamptz
);

CREATE TABLE collector_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurrence_key text NOT NULL UNIQUE,
    player_id bigint REFERENCES players (id),
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    normalized_tag text,
    endpoint text NOT NULL CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    request_started_at timestamptz NOT NULL,
    response_completed_at timestamptz NOT NULL,
    http_status integer NOT NULL,
    response_hash text NOT NULL,
    archive_reference text,
    collector_version text NOT NULL,
    key_label text NOT NULL,
    evidence_headers jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE python_processing_jobs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL UNIQUE REFERENCES collector_observations (id),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'leased', 'waiting_retry', 'complete', 'failed')
    ),
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE collector_reset_sweeps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL UNIQUE,
    member_ids bigint[] NOT NULL,
    membership_captured_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE collector_work (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN (
        'initial_collection', 'live_refresh', 'reset_baseline',
        'discovery_profile', 'global_player_rankings'
    )),
    lane text NOT NULL CHECK (lane IN ('interactive', 'reset', 'ordinary')),
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    player_id bigint REFERENCES players (id),
    normalized_tag text,
    sweep_id bigint REFERENCES collector_reset_sweeps (id),
    due_at timestamptz NOT NULL,
    coalescing_key text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('pending', 'waiting_retry', 'complete', 'failed', 'cancelled')
    ),
    profile_status text NOT NULL,
    battle_log_status text NOT NULL,
    profile_observation_id bigint REFERENCES collector_observations (id),
    battle_log_observation_id bigint REFERENCES collector_observations (id),
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE UNIQUE INDEX collector_work_one_active_key
    ON collector_work (coalescing_key)
    WHERE status IN ('pending', 'waiting_retry');
CREATE INDEX collector_work_claim_order
    ON collector_work (lane, status, due_at, id);

CREATE TABLE collector_transport_failures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurrence_key text NOT NULL UNIQUE,
    player_id bigint REFERENCES players (id),
    normalized_tag text,
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    endpoint text NOT NULL CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    request_started_at timestamptz NOT NULL,
    failed_at timestamptz NOT NULL,
    failure_category text NOT NULL,
    retry_state text NOT NULL,
    key_label text NOT NULL
);
