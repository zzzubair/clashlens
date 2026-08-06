-- Add the durable Python processing contract and versioned player profiles.
-- Migration 0001 remains unchanged so the bridge collector can start first.

BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE
    current_version integer;
BEGIN
    SELECT version INTO current_version
    FROM clash_lens_contract
    WHERE singleton;

    IF current_version NOT IN (1, 2) THEN
        RAISE EXCEPTION 'contract version must be 1 or 2 before migration 0002';
    END IF;
END
$$;

-- Collector contract version 2 adds checked global work and paired per-player
-- reset baselines. Version-1 reset rows remain visible as legacy evidence.
ALTER TABLE collector_jobs
    DROP CONSTRAINT IF EXISTS collector_jobs_work_type_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_required_endpoint_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_work_type_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_required_endpoint_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_scope_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_work_scope_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_reset_identity_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_lease_generation_v2_check;
ALTER TABLE collector_endpoint_results
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_endpoint_check,
    DROP CONSTRAINT IF EXISTS collector_endpoint_results_endpoint_v2_check;
ALTER TABLE collector_observations
    DROP CONSTRAINT IF EXISTS collector_observations_endpoint_check,
    DROP CONSTRAINT IF EXISTS collector_observations_endpoint_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_scope_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_hash_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_request_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_archive_v2_check;
ALTER TABLE collector_transport_failures
    DROP CONSTRAINT IF EXISTS collector_transport_failures_endpoint_check,
    DROP CONSTRAINT IF EXISTS collector_transport_failures_endpoint_v2_check,
    DROP CONSTRAINT IF EXISTS collector_transport_failures_scope_v2_check,
    DROP CONSTRAINT IF EXISTS collector_transport_failures_request_v2_check;

ALTER TABLE collector_jobs
    ALTER COLUMN normalized_tag DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'player',
    ADD COLUMN IF NOT EXISTS reset_baseline_sweep_id bigint,
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0;

INSERT INTO players (normalized_tag, active)
SELECT DISTINCT normalized_tag, false
FROM collector_jobs
WHERE player_id IS NULL AND normalized_tag IS NOT NULL
ON CONFLICT (normalized_tag) DO NOTHING;

UPDATE collector_jobs AS job
SET player_id = player.id
FROM players AS player
WHERE job.scope = 'player'
  AND job.player_id IS NULL
  AND player.normalized_tag = job.normalized_tag;

CREATE TABLE IF NOT EXISTS collector_reset_baseline_sweeps (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    reset_sweep_id bigint NOT NULL REFERENCES collector_reset_sweeps (id),
    player_id bigint NOT NULL REFERENCES players (id),
    boundary_at timestamptz NOT NULL,
    evidence_kind text NOT NULL CHECK (
        evidence_kind IN ('paired_v2', 'legacy_profile_only_v1')
    ),
    state text NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'incomplete', 'complete', 'failed', 'cancelled')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    UNIQUE (reset_sweep_id, player_id),
    UNIQUE (id, player_id),
    CHECK (date_part('hour', boundary_at AT TIME ZONE 'UTC') = 5),
    CHECK (date_part('minute', boundary_at AT TIME ZONE 'UTC') = 0),
    CHECK (date_part('second', boundary_at AT TIME ZONE 'UTC') = 0)
);

INSERT INTO collector_reset_baseline_sweeps (
    reset_sweep_id,
    player_id,
    boundary_at,
    evidence_kind,
    state
)
SELECT DISTINCT
    job.sweep_id,
    job.player_id,
    sweep.boundary_at,
    'legacy_profile_only_v1',
    CASE job.status
        WHEN 'complete' THEN 'complete'
        WHEN 'failed' THEN 'failed'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'pending'
    END
FROM collector_jobs AS job
JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
WHERE job.work_type = 'reset_profile'
  AND job.player_id IS NOT NULL
ON CONFLICT (reset_sweep_id, player_id) DO NOTHING;

UPDATE collector_jobs AS job
SET work_type = 'legacy_reset_profile',
    reset_baseline_sweep_id = baseline.id
FROM collector_reset_baseline_sweeps AS baseline
WHERE job.work_type = 'reset_profile'
  AND baseline.reset_sweep_id = job.sweep_id
  AND baseline.player_id = job.player_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'collector_jobs'::regclass
          AND conname = 'collector_jobs_reset_baseline_sweep_v2_fk'
    ) THEN
        ALTER TABLE collector_jobs
            ADD CONSTRAINT collector_jobs_reset_baseline_sweep_v2_fk
            FOREIGN KEY (reset_baseline_sweep_id)
            REFERENCES collector_reset_baseline_sweeps (id);
    END IF;
END
$$;

ALTER TABLE collector_jobs
    ADD CONSTRAINT collector_jobs_work_type_v2_check CHECK (work_type IN (
        'regular_poll',
        'initial_collection',
        'live_refresh',
        'legacy_reset_profile',
        'reset_baseline',
        'global_player_rankings',
        'endpoint_retry'
    )),
    ADD CONSTRAINT collector_jobs_required_endpoint_v2_check CHECK (
        required_endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    ADD CONSTRAINT collector_jobs_scope_v2_check CHECK (
        (scope = 'player' AND player_id IS NOT NULL AND normalized_tag IS NOT NULL)
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL)
    ),
    ADD CONSTRAINT collector_jobs_work_scope_v2_check CHECK (
        (work_type = 'global_player_rankings'
            AND scope = 'global'
            AND capacity_pool = 'normal'
            AND required_endpoint = 'global_player_rankings'
            AND sweep_id IS NULL
            AND reset_baseline_sweep_id IS NULL)
        OR (work_type <> 'global_player_rankings' AND (
            work_type = 'endpoint_retry'
            OR scope = 'player'
        ))
    ),
    ADD CONSTRAINT collector_jobs_reset_identity_v2_check CHECK (
        (work_type IN ('legacy_reset_profile', 'reset_baseline')
            AND sweep_id IS NOT NULL
            AND reset_baseline_sweep_id IS NOT NULL)
        OR work_type NOT IN ('legacy_reset_profile', 'reset_baseline')
    ),
    ADD CONSTRAINT collector_jobs_lease_generation_v2_check CHECK (
        lease_generation >= 0
    );

ALTER TABLE collector_attempts
    ADD COLUMN IF NOT EXISTS attempt_number integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_token text,
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure_category text,
    DROP CONSTRAINT IF EXISTS collector_attempts_attempt_number_v2_check,
    DROP CONSTRAINT IF EXISTS collector_attempts_lease_generation_v2_check,
    DROP CONSTRAINT IF EXISTS collector_attempts_failure_category_v2_check,
    ADD CONSTRAINT collector_attempts_attempt_number_v2_check CHECK (attempt_number > 0),
    ADD CONSTRAINT collector_attempts_lease_generation_v2_check CHECK (lease_generation >= 0),
    ADD CONSTRAINT collector_attempts_failure_category_v2_check CHECK (
        length(COALESCE(failure_category, '')) <= 128
        AND COALESCE(failure_category, '') !~ '[\r\n]'
    );

CREATE UNIQUE INDEX IF NOT EXISTS collector_attempts_job_attempt_number_v2
    ON collector_attempts (job_id, attempt_number);

CREATE TABLE IF NOT EXISTS collector_attempt_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES collector_jobs (id),
    attempt_id bigint NOT NULL REFERENCES collector_attempts (id),
    event_type text NOT NULL CHECK (event_type IN (
        'claimed', 'lease_expired', 'retry_scheduled', 'completed',
        'failed', 'cancelled'
    )),
    from_status text,
    to_status text,
    lease_owner text,
    lease_token text,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    failure_category text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (length(COALESCE(failure_category, '')) <= 128),
    CHECK (COALESCE(failure_category, '') !~ '[\r\n]'),
    UNIQUE (attempt_id, event_type, lease_generation)
);
CREATE INDEX IF NOT EXISTS collector_attempt_events_job_order_v2
    ON collector_attempt_events (job_id, id);

CREATE OR REPLACE FUNCTION clashlens_validate_reset_baseline_job()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    baseline_kind text;
    baseline_player_id bigint;
    baseline_reset_sweep_id bigint;
BEGIN
    IF NEW.work_type NOT IN ('legacy_reset_profile', 'reset_baseline') THEN
        RETURN NEW;
    END IF;

    SELECT evidence_kind, player_id, reset_sweep_id
    INTO baseline_kind, baseline_player_id, baseline_reset_sweep_id
    FROM collector_reset_baseline_sweeps
    WHERE id = NEW.reset_baseline_sweep_id;

    IF baseline_kind IS NULL
       OR baseline_player_id <> NEW.player_id
       OR baseline_reset_sweep_id <> NEW.sweep_id THEN
        RAISE EXCEPTION 'collector reset job baseline identity does not match';
    END IF;
    IF NEW.work_type = 'reset_baseline' AND baseline_kind <> 'paired_v2' THEN
        RAISE EXCEPTION 'paired reset work requires paired_v2 evidence identity';
    END IF;
    IF NEW.work_type = 'legacy_reset_profile' AND baseline_kind <> 'legacy_profile_only_v1' THEN
        RAISE EXCEPTION 'legacy reset work requires legacy evidence identity';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS collector_jobs_validate_reset_baseline_v2 ON collector_jobs;
CREATE TRIGGER collector_jobs_validate_reset_baseline_v2
BEFORE INSERT OR UPDATE OF work_type, player_id, sweep_id, reset_baseline_sweep_id
ON collector_jobs
FOR EACH ROW
EXECUTE FUNCTION clashlens_validate_reset_baseline_job();

ALTER TABLE collector_endpoint_results
    ADD CONSTRAINT collector_endpoint_results_endpoint_v2_check CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    ADD COLUMN IF NOT EXISTS request_method text,
    ADD COLUMN IF NOT EXISTS request_path text,
    ADD COLUMN IF NOT EXISTS request_query text,
    ADD COLUMN IF NOT EXISTS paging_envelope_state text,
    ADD COLUMN IF NOT EXISTS source_adapter_version text;

ALTER TABLE collector_observations
    ALTER COLUMN normalized_tag DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'player',
    ADD COLUMN IF NOT EXISTS request_method text,
    ADD COLUMN IF NOT EXISTS request_path text,
    ADD COLUMN IF NOT EXISTS request_query text,
    ADD COLUMN IF NOT EXISTS paging_envelope_state text,
    ADD COLUMN IF NOT EXISTS source_adapter_version text;

ALTER TABLE collector_transport_failures
    ALTER COLUMN normalized_tag DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'player',
    ADD COLUMN IF NOT EXISTS request_method text,
    ADD COLUMN IF NOT EXISTS request_path text,
    ADD COLUMN IF NOT EXISTS request_query text,
    ADD COLUMN IF NOT EXISTS paging_envelope_state text,
    ADD COLUMN IF NOT EXISTS source_adapter_version text;

UPDATE collector_endpoint_results AS endpoint_result
SET request_method = COALESCE(endpoint_result.request_method, 'GET'),
    request_path = COALESCE(
        endpoint_result.request_path,
        '/v1/players/%23' || substring(job.normalized_tag FROM 2)
            || CASE endpoint_result.endpoint WHEN 'battle_log' THEN '/battlelog' ELSE '' END
    ),
    request_query = COALESCE(endpoint_result.request_query, ''),
    paging_envelope_state = COALESCE(endpoint_result.paging_envelope_state, 'not_applicable'),
    source_adapter_version = COALESCE(
        endpoint_result.source_adapter_version,
        CASE endpoint_result.endpoint
            WHEN 'profile' THEN 'player-profile-v1'
            WHEN 'battle_log' THEN 'battle-log-v1'
        END
    )
FROM collector_attempts AS attempt
JOIN collector_jobs AS job ON job.id = attempt.job_id
WHERE endpoint_result.attempt_id = attempt.id
  AND endpoint_result.request_count > 0;

UPDATE collector_observations
SET request_method = COALESCE(request_method, 'GET'),
    request_path = COALESCE(
        request_path,
        '/v1/players/%23' || substring(normalized_tag FROM 2)
            || CASE endpoint WHEN 'battle_log' THEN '/battlelog' ELSE '' END
    ),
    request_query = COALESCE(request_query, ''),
    paging_envelope_state = COALESCE(paging_envelope_state, 'not_applicable'),
    source_adapter_version = COALESCE(
        source_adapter_version,
        CASE endpoint
            WHEN 'profile' THEN 'player-profile-v1'
            WHEN 'battle_log' THEN 'battle-log-v1'
        END
    );

UPDATE collector_transport_failures
SET request_method = COALESCE(request_method, 'GET'),
    request_path = COALESCE(
        request_path,
        '/v1/players/%23' || substring(normalized_tag FROM 2)
            || CASE endpoint WHEN 'battle_log' THEN '/battlelog' ELSE '' END
    ),
    request_query = COALESCE(request_query, ''),
    paging_envelope_state = COALESCE(paging_envelope_state, 'unknown_no_response'),
    source_adapter_version = COALESCE(
        source_adapter_version,
        CASE endpoint
            WHEN 'profile' THEN 'player-profile-v1'
            WHEN 'battle_log' THEN 'battle-log-v1'
        END
    );

ALTER TABLE collector_observations
    ALTER COLUMN request_method SET NOT NULL,
    ALTER COLUMN request_path SET NOT NULL,
    ALTER COLUMN request_query SET NOT NULL,
    ALTER COLUMN paging_envelope_state SET NOT NULL,
    ALTER COLUMN source_adapter_version SET NOT NULL,
    ADD CONSTRAINT collector_observations_endpoint_v2_check CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    ADD CONSTRAINT collector_observations_scope_v2_check CHECK (
        (scope = 'player' AND player_id IS NOT NULL
            AND normalized_tag IS NOT NULL
            AND endpoint IN ('profile', 'battle_log'))
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL
            AND endpoint = 'global_player_rankings')
    ),
    ADD CONSTRAINT collector_observations_hash_v2_check CHECK (
        response_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT collector_observations_request_v2_check CHECK (
        request_method = 'GET'
        AND request_path <> ''
        AND request_query !~ '[[:space:]]'
        AND (
            (endpoint = 'global_player_rankings'
                AND request_path = '/v1/locations/global/rankings/players'
                AND request_query = 'limit=200'
                AND paging_envelope_state IN ('not_present', 'cursor_present', 'malformed'))
            OR (endpoint IN ('profile', 'battle_log')
                AND request_query = ''
                AND paging_envelope_state = 'not_applicable')
        )
    ),
    ADD CONSTRAINT collector_observations_archive_v2_check CHECK (
        archive_reference <> ''
        AND collector_version <> ''
        AND source_adapter_version <> ''
    );

ALTER TABLE collector_transport_failures
    ALTER COLUMN request_method SET NOT NULL,
    ALTER COLUMN request_path SET NOT NULL,
    ALTER COLUMN request_query SET NOT NULL,
    ALTER COLUMN paging_envelope_state SET NOT NULL,
    ALTER COLUMN source_adapter_version SET NOT NULL,
    ADD CONSTRAINT collector_transport_failures_endpoint_v2_check CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    ADD CONSTRAINT collector_transport_failures_scope_v2_check CHECK (
        (scope = 'player' AND player_id IS NOT NULL
            AND normalized_tag IS NOT NULL
            AND endpoint IN ('profile', 'battle_log'))
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL
            AND endpoint = 'global_player_rankings')
    ),
    ADD CONSTRAINT collector_transport_failures_request_v2_check CHECK (
        request_method = 'GET'
        AND request_path <> ''
        AND request_query !~ '[[:space:]]'
        AND paging_envelope_state = 'unknown_no_response'
    );

ALTER TABLE collector_transport_failures
    ADD COLUMN IF NOT EXISTS evidence_key text;
UPDATE collector_transport_failures
SET evidence_key = 'legacy-transport:' || id::text
WHERE evidence_key IS NULL;
ALTER TABLE collector_transport_failures
    ALTER COLUMN evidence_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS collector_transport_failures_evidence_key_v2
    ON collector_transport_failures (evidence_key);

CREATE OR REPLACE FUNCTION clashlens_fill_collector_provenance_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.request_method := COALESCE(NEW.request_method, 'GET');
    IF NEW.endpoint = 'global_player_rankings' THEN
        NEW.scope := 'global';
        NEW.request_path := COALESCE(
            NEW.request_path,
            '/v1/locations/global/rankings/players'
        );
        NEW.request_query := COALESCE(NEW.request_query, 'limit=200');
        NEW.paging_envelope_state := COALESCE(NEW.paging_envelope_state, 'malformed');
        NEW.source_adapter_version := COALESCE(
            NEW.source_adapter_version,
            'global-player-rankings-v1'
        );
    ELSE
        NEW.scope := COALESCE(NEW.scope, 'player');
        NEW.request_path := COALESCE(
            NEW.request_path,
            '/v1/players/%23' || substring(NEW.normalized_tag FROM 2)
                || CASE NEW.endpoint WHEN 'battle_log' THEN '/battlelog' ELSE '' END
        );
        NEW.request_query := COALESCE(NEW.request_query, '');
        NEW.paging_envelope_state := COALESCE(
            NEW.paging_envelope_state,
            CASE TG_TABLE_NAME
                WHEN 'collector_transport_failures' THEN 'unknown_no_response'
                ELSE 'not_applicable'
            END
        );
        NEW.source_adapter_version := COALESCE(
            NEW.source_adapter_version,
            CASE NEW.endpoint
                WHEN 'profile' THEN 'player-profile-v1'
                WHEN 'battle_log' THEN 'battle-log-v1'
            END
        );
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS collector_observations_fill_provenance_v2
    ON collector_observations;
CREATE TRIGGER collector_observations_fill_provenance_v2
BEFORE INSERT ON collector_observations
FOR EACH ROW
EXECUTE FUNCTION clashlens_fill_collector_provenance_v2();

DROP TRIGGER IF EXISTS collector_transport_failures_fill_provenance_v2
    ON collector_transport_failures;
CREATE TRIGGER collector_transport_failures_fill_provenance_v2
BEFORE INSERT ON collector_transport_failures
FOR EACH ROW
EXECUTE FUNCTION clashlens_fill_collector_provenance_v2();

ALTER TABLE collector_observations
    ADD COLUMN IF NOT EXISTS endpoint_version text
        GENERATED ALWAYS AS (
            CASE endpoint
                WHEN 'profile' THEN 'profile-v1'
                WHEN 'battle_log' THEN 'battle-log-v1'
                WHEN 'global_player_rankings' THEN 'global-player-rankings-v1'
            END
        ) STORED,
    ADD COLUMN IF NOT EXISTS schema_version text
        GENERATED ALWAYS AS (
            CASE endpoint
                WHEN 'profile' THEN 'profile-schema-v1'
                WHEN 'battle_log' THEN 'battle-log-schema-v1'
                WHEN 'global_player_rankings' THEN 'global-player-rankings-schema-v1'
            END
        ) STORED,
    ADD COLUMN IF NOT EXISTS response_observed_at timestamptz
        GENERATED ALWAYS AS (response_completed_at) STORED;

ALTER TABLE python_processing_jobs
    ADD COLUMN IF NOT EXISTS work_type text NOT NULL DEFAULT 'process_observation',
    ADD COLUMN IF NOT EXISTS replay_observation_id bigint REFERENCES collector_observations (id),
    ADD COLUMN IF NOT EXISTS deduplication_key text,
    ADD COLUMN IF NOT EXISTS input_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS priority integer NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS parser_version text NOT NULL DEFAULT 'supercell-source-parser-v1',
    ADD COLUMN IF NOT EXISTS processing_version text NOT NULL DEFAULT 'clashlens-domain-processing-v1',
    ADD COLUMN IF NOT EXISTS domain_rule_version text NOT NULL DEFAULT 'clashlens-domain-rules-v1',
    ADD COLUMN IF NOT EXISTS snapshot_rule_version text NOT NULL DEFAULT 'tracked-player-order-v1',
    ADD COLUMN IF NOT EXISTS analytics_rule_version text NOT NULL DEFAULT 'legend-analytics-v1',
    ADD COLUMN IF NOT EXISTS export_schema_version text NOT NULL DEFAULT 'account-export-v1',
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_token text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_generation bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 3,
    ADD COLUMN IF NOT EXISTS outcome text,
    ADD COLUMN IF NOT EXISTS failure_category text,
    ADD COLUMN IF NOT EXISTS failure_detail text,
    ADD COLUMN IF NOT EXISTS completed_at timestamptz;

ALTER TABLE python_processing_jobs
    ALTER COLUMN parser_version SET DEFAULT 'supercell-source-parser-v1',
    ALTER COLUMN processing_version SET DEFAULT 'clashlens-domain-processing-v1',
    ALTER COLUMN domain_rule_version SET DEFAULT 'clashlens-domain-rules-v1',
    ALTER COLUMN snapshot_rule_version SET DEFAULT 'tracked-player-order-v1',
    ALTER COLUMN analytics_rule_version SET DEFAULT 'legend-analytics-v1',
    ALTER COLUMN export_schema_version SET DEFAULT 'account-export-v1';

UPDATE python_processing_jobs
SET parser_version = CASE parser_version
        WHEN 'profile-parser-v1' THEN 'supercell-source-parser-v1'
        ELSE parser_version
    END,
    processing_version = CASE processing_version
        WHEN 'python-processing-prototype-v1' THEN 'clashlens-domain-processing-v1'
        ELSE processing_version
    END,
    domain_rule_version = CASE domain_rule_version
        WHEN 'domain-v1' THEN 'clashlens-domain-rules-v1'
        ELSE domain_rule_version
    END,
    snapshot_rule_version = CASE snapshot_rule_version
        WHEN 'snapshot-v1' THEN 'tracked-player-order-v1'
        ELSE snapshot_rule_version
    END,
    analytics_rule_version = CASE analytics_rule_version
        WHEN 'analytics-v1' THEN 'legend-analytics-v1'
        ELSE analytics_rule_version
    END,
    export_schema_version = CASE export_schema_version
        WHEN 'export-v1' THEN 'account-export-v1'
        ELSE export_schema_version
    END
WHERE parser_version IN ('profile-parser-v1', 'supercell-source-parser-v1')
   OR processing_version IN ('python-processing-prototype-v1', 'clashlens-domain-processing-v1')
   OR domain_rule_version IN ('domain-v1', 'clashlens-domain-rules-v1')
   OR snapshot_rule_version IN ('snapshot-v1', 'tracked-player-order-v1')
   OR analytics_rule_version IN ('analytics-v1', 'legend-analytics-v1')
   OR export_schema_version IN ('export-v1', 'account-export-v1');

ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_status_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_status_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_work_type_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_lease_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_attempt_count_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_identity_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_versions_v2_check,
    DROP CONSTRAINT IF EXISTS python_processing_jobs_failure_v2_check;

UPDATE python_processing_jobs
SET deduplication_key = 'process-observation:' || observation_id::text
WHERE deduplication_key IS NULL;

CREATE OR REPLACE FUNCTION clashlens_set_python_job_deduplication_key()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.deduplication_key IS NULL THEN
        IF NEW.work_type = 'process_observation' AND NEW.observation_id IS NOT NULL THEN
            NEW.deduplication_key := 'process-observation:' || NEW.observation_id::text;
        ELSE
            RAISE EXCEPTION 'deduplication_key is required for non-default Python work';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS python_processing_jobs_set_deduplication_key
    ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_set_deduplication_key
BEFORE INSERT ON python_processing_jobs
FOR EACH ROW
EXECUTE FUNCTION clashlens_set_python_job_deduplication_key();

ALTER TABLE python_processing_jobs
    ALTER COLUMN observation_id DROP NOT NULL,
    ALTER COLUMN deduplication_key SET NOT NULL,
    ADD CONSTRAINT python_processing_jobs_status_v2_check CHECK (status IN (
        'pending', 'leased', 'waiting_retry', 'complete', 'failed', 'cancelled'
    )),
    ADD CONSTRAINT python_processing_jobs_work_type_v2_check CHECK (work_type IN (
        'process_observation',
        'replay_observation',
        'reconcile_ranked_day',
        'build_snapshot',
        'build_analytics',
        'build_export'
    )),
    ADD CONSTRAINT python_processing_jobs_lease_v2_check CHECK (
        (status = 'leased'
            AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL)
        OR (status <> 'leased'
            AND lease_owner IS NULL
            AND lease_token IS NULL
            AND lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT python_processing_jobs_attempt_count_v2_check CHECK (
        attempt_count >= 0
        AND max_attempts BETWEEN 1 AND 100
        AND lease_generation >= 0
    ),
    ADD CONSTRAINT python_processing_jobs_identity_v2_check CHECK (
        (work_type = 'process_observation'
            AND observation_id IS NOT NULL
            AND replay_observation_id IS NULL)
        OR (work_type = 'replay_observation'
            AND observation_id IS NULL
            AND replay_observation_id IS NOT NULL)
        OR (work_type NOT IN ('process_observation', 'replay_observation')
            AND observation_id IS NULL
            AND replay_observation_id IS NULL)
    ),
    ADD CONSTRAINT python_processing_jobs_input_v2_check CHECK (
        jsonb_typeof(input_json) = 'object'
        AND COALESCE(CASE work_type
            WHEN 'process_observation' THEN input_json = '{}'::jsonb
            WHEN 'replay_observation' THEN
                jsonb_typeof(input_json -> 'replay_request_id') = 'number'
                AND (input_json ->> 'replay_request_id')::bigint > 0
            WHEN 'reconcile_ranked_day' THEN
                jsonb_typeof(input_json -> 'player_id') = 'number'
                AND (input_json ->> 'player_id')::bigint > 0
                AND input_json ->> 'ranked_day_start'
                    ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
            WHEN 'build_snapshot' THEN input_json ->> 'boundary_at'
                ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
            WHEN 'build_analytics' THEN
                (jsonb_typeof(input_json -> 'snapshot_id') = 'number'
                        AND (input_json ->> 'snapshot_id')::bigint > 0)
                OR jsonb_typeof(input_json -> 'selection') = 'object'
            WHEN 'build_export' THEN
                jsonb_typeof(input_json -> 'export_request_id') = 'number'
                AND (input_json ->> 'export_request_id')::bigint > 0
            ELSE false
        END, false)
    ),
    ADD CONSTRAINT python_processing_jobs_versions_v2_check CHECK (
        parser_version <> ''
        AND processing_version <> ''
        AND domain_rule_version <> ''
        AND snapshot_rule_version <> ''
        AND analytics_rule_version <> ''
        AND export_schema_version <> ''
    ),
    ADD CONSTRAINT python_processing_jobs_failure_v2_check CHECK (
        length(COALESCE(failure_category, '')) <= 128
        AND length(COALESCE(failure_detail, '')) <= 1024
        AND COALESCE(failure_category, '') !~ '[\r\n]'
        AND COALESCE(failure_detail, '') !~ '[\r\n]'
        AND length(deduplication_key) BETWEEN 1 AND 512
    );

CREATE UNIQUE INDEX IF NOT EXISTS python_processing_jobs_deduplication_key_v2
    ON python_processing_jobs (deduplication_key);
DROP INDEX IF EXISTS python_processing_jobs_one_initial_observation_v2;
CREATE INDEX IF NOT EXISTS python_processing_jobs_claim_order_v2
    ON python_processing_jobs (status, due_at, priority DESC, created_at, id);
CREATE INDEX IF NOT EXISTS python_processing_jobs_supported_claim_v2
    ON python_processing_jobs (
        work_type,
        parser_version,
        processing_version,
        domain_rule_version,
        due_at
    )
    WHERE status IN ('pending', 'waiting_retry', 'leased');

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
    completed_at
FROM python_processing_jobs;

CREATE TABLE IF NOT EXISTS python_processing_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES python_processing_jobs (id),
    attempt_number integer NOT NULL,
    lease_owner text NOT NULL,
    lease_token text NOT NULL,
    lease_generation bigint NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    started_at timestamptz NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    completed_at timestamptz,
    state text NOT NULL CHECK (
        state IN ('running', 'waiting_retry', 'complete', 'failed', 'stale')
    ),
    outcome text,
    failure_category text,
    failure_detail text,
    retry_due_at timestamptz,
    CHECK (length(COALESCE(failure_category, '')) <= 128),
    CHECK (length(COALESCE(failure_detail, '')) <= 1024),
    UNIQUE (job_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS python_processing_attempts_job_order_v2
    ON python_processing_attempts (job_id, attempt_number);

CREATE TABLE IF NOT EXISTS python_processing_job_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES python_processing_jobs (id),
    event_type text NOT NULL CHECK (
        event_type IN ('claimed', 'renewed', 'retry_scheduled', 'completed', 'failed',
                       'cancelled', 'operator_reset')
    ),
    from_state text,
    to_state text NOT NULL CHECK (
        to_state IN ('pending', 'leased', 'waiting_retry', 'complete', 'failed', 'cancelled')
    ),
    lease_token text,
    operator_identity text,
    reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (length(COALESCE(reason, '')) <= 1024),
    CHECK ((event_type = 'operator_reset') = (operator_identity IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS python_processing_job_events_job_order_v2
    ON python_processing_job_events (job_id, id);

CREATE OR REPLACE FUNCTION clashlens_advance_python_job_fence_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = 'leased'
       AND (OLD.status <> 'leased' OR NEW.lease_token IS DISTINCT FROM OLD.lease_token) THEN
        NEW.lease_generation := OLD.lease_generation + 1;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS python_processing_jobs_advance_fence_v2
    ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_advance_fence_v2
BEFORE UPDATE OF status, lease_token ON python_processing_jobs
FOR EACH ROW
EXECUTE FUNCTION clashlens_advance_python_job_fence_v2();

CREATE OR REPLACE FUNCTION clashlens_fill_python_attempt_fence_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    SELECT lease_generation INTO NEW.lease_generation
    FROM python_processing_jobs
    WHERE id = NEW.job_id;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS python_processing_attempts_fill_fence_v2
    ON python_processing_attempts;
CREATE TRIGGER python_processing_attempts_fill_fence_v2
BEFORE INSERT ON python_processing_attempts
FOR EACH ROW
EXECUTE FUNCTION clashlens_fill_python_attempt_fence_v2();

CREATE OR REPLACE FUNCTION clashlens_operator_reset_python_job(
    requested_job_id bigint,
    requested_operator text,
    requested_reason text
)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
    prior_state text;
BEGIN
    IF requested_operator !~ '^[A-Za-z0-9._:@-]{1,128}$'
       OR length(requested_reason) NOT BETWEEN 1 AND 1024
       OR requested_reason ~ '[\r\n]' THEN
        RAISE EXCEPTION 'invalid operator reset audit fields';
    END IF;
    SELECT status INTO prior_state
    FROM python_processing_jobs
    WHERE id = requested_job_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF prior_state NOT IN ('failed', 'cancelled') THEN
        RAISE EXCEPTION 'only terminal Python jobs can be reset';
    END IF;
    UPDATE python_processing_jobs
    SET status = 'pending',
        due_at = clock_timestamp(),
        lease_owner = NULL,
        lease_token = NULL,
        lease_expires_at = NULL,
        outcome = NULL,
        failure_category = NULL,
        failure_detail = NULL,
        completed_at = NULL,
        updated_at = clock_timestamp()
    WHERE id = requested_job_id;
    INSERT INTO python_processing_job_events (
        job_id, event_type, from_state, to_state,
        operator_identity, reason
    ) VALUES (
        requested_job_id, 'operator_reset', prior_state, 'pending',
        requested_operator, requested_reason
    );
    RETURN true;
END
$$;

ALTER TABLE players
    ADD COLUMN IF NOT EXISTS eligibility_state text NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS current_profile_version_id bigint,
    ADD COLUMN IF NOT EXISTS current_observed_at timestamptz,
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT clock_timestamp();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'players'::regclass
          AND conname = 'players_eligibility_state_v2_check'
    ) THEN
        ALTER TABLE players
            ADD CONSTRAINT players_eligibility_state_v2_check
            CHECK (eligibility_state IN ('unknown', 'eligible', 'ineligible', 'uncertain'));
    END IF;
END
$$;

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
    profile_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, parser_version)
);
CREATE INDEX IF NOT EXISTS player_profile_versions_tag_time_v2
    ON player_profile_versions (normalized_tag, observed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS player_profile_effects (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    profile_version_id bigint NOT NULL REFERENCES player_profile_versions (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    effect_kind text NOT NULL CHECK (effect_kind = 'current_profile'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, effect_kind)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'players'::regclass
          AND conname = 'players_current_profile_version_v2_fk'
    ) THEN
        ALTER TABLE players
            ADD CONSTRAINT players_current_profile_version_v2_fk
            FOREIGN KEY (current_profile_version_id)
            REFERENCES player_profile_versions (id);
    END IF;
END
$$;

-- Replay creation is an audited host-operator action. Application roles can
-- consume a replay job but do not need a path that creates this audit row.
CREATE TABLE IF NOT EXISTS python_replay_requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    operator_identity text NOT NULL,
    reason text NOT NULL,
    target_parser_version text NOT NULL,
    target_domain_rule_version text NOT NULL,
    selection_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    status text NOT NULL DEFAULT 'requested' CHECK (
        status IN ('requested', 'enqueued', 'complete', 'failed', 'cancelled')
    ),
    job_id bigint UNIQUE REFERENCES python_processing_jobs (id),
    completed_at timestamptz,
    CHECK (operator_identity ~ '^[A-Za-z0-9._:@-]{1,128}$'),
    CHECK (length(reason) BETWEEN 1 AND 1024),
    CHECK (reason !~ '[\r\n]'),
    CHECK (target_parser_version <> '' AND target_domain_rule_version <> ''),
    CHECK (jsonb_typeof(selection_json) = 'object'),
    UNIQUE (observation_id, target_parser_version, target_domain_rule_version)
);

-- One shared-key gate is used by Go interactive collection and Python token
-- verification. Fingerprints are SHA-256 of exact ASCII bearer-token bytes.
CREATE TABLE IF NOT EXISTS shared_api_credentials (
    credential_fingerprint text PRIMARY KEY CHECK (
        credential_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    credential_kind text NOT NULL DEFAULT 'supercell_interactive' CHECK (
        credential_kind = 'supercell_interactive'
    ),
    go_budget integer NOT NULL DEFAULT 29 CHECK (go_budget = 29),
    python_budget integer NOT NULL DEFAULT 1 CHECK (python_budget = 1),
    total_budget integer NOT NULL DEFAULT 30 CHECK (total_budget = 30),
    state text NOT NULL DEFAULT 'active' CHECK (
        state IN ('active', 'cooldown', 'quarantined', 'retired')
    ),
    cooldown_until timestamptz,
    quarantine_reason text,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (go_budget + python_budget = total_budget),
    CHECK ((state = 'cooldown') = (cooldown_until IS NOT NULL)),
    CHECK ((state = 'quarantined') = (quarantine_reason IS NOT NULL)),
    CHECK (length(COALESCE(quarantine_reason, '')) <= 256),
    CHECK (COALESCE(quarantine_reason, '') !~ '[\r\n]')
);

CREATE TABLE IF NOT EXISTS shared_api_permits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    credential_fingerprint text NOT NULL REFERENCES shared_api_credentials (
        credential_fingerprint
    ),
    caller text NOT NULL CHECK (caller IN ('go', 'python')),
    permitted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS shared_api_permits_window_v2
    ON shared_api_permits (credential_fingerprint, permitted_at, caller);

CREATE TABLE IF NOT EXISTS shared_api_credential_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    credential_fingerprint text NOT NULL REFERENCES shared_api_credentials (
        credential_fingerprint
    ),
    event_type text NOT NULL CHECK (
        event_type IN ('registered', 'cooldown', 'quarantined', 'operator_reset', 'retired')
    ),
    actor text NOT NULL,
    reason text,
    cooldown_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (actor ~ '^[A-Za-z0-9._:@-]{1,128}$'),
    CHECK (length(COALESCE(reason, '')) <= 256),
    CHECK (COALESCE(reason, '') !~ '[\r\n]')
);
CREATE INDEX IF NOT EXISTS shared_api_credential_events_order_v2
    ON shared_api_credential_events (credential_fingerprint, id);

CREATE OR REPLACE FUNCTION clashlens_acquire_shared_api_permit(
    requested_fingerprint text,
    requested_caller text
)
RETURNS TABLE (
    granted boolean,
    database_time timestamptz,
    next_eligible_at timestamptz,
    credential_state text
)
LANGUAGE plpgsql
AS $$
DECLARE
    credential shared_api_credentials%ROWTYPE;
    now_at timestamptz;
    caller_count integer;
    total_count integer;
    caller_budget integer;
    caller_next timestamptz;
    total_next timestamptz;
BEGIN
    IF requested_caller NOT IN ('go', 'python') THEN
        RAISE EXCEPTION 'shared API caller must be go or python';
    END IF;

    SELECT * INTO credential
    FROM shared_api_credentials
    WHERE credential_fingerprint = requested_fingerprint
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared API credential is not registered';
    END IF;

    now_at := clock_timestamp();
    IF credential.state = 'cooldown' AND credential.cooldown_until <= now_at THEN
        UPDATE shared_api_credentials
        SET state = 'active', cooldown_until = NULL, updated_at = now_at
        WHERE credential_fingerprint = requested_fingerprint;
        credential.state := 'active';
        credential.cooldown_until := NULL;
    END IF;

    IF credential.state <> 'active' THEN
        RETURN QUERY SELECT
            false,
            now_at,
            CASE credential.state
                WHEN 'cooldown' THEN credential.cooldown_until
                ELSE NULL::timestamptz
            END,
            credential.state;
        RETURN;
    END IF;

    caller_budget := CASE requested_caller
        WHEN 'go' THEN credential.go_budget
        ELSE credential.python_budget
    END;
    SELECT
        count(*) FILTER (WHERE caller = requested_caller),
        count(*)
    INTO caller_count, total_count
    FROM shared_api_permits
    WHERE credential_fingerprint = requested_fingerprint
      AND permitted_at > now_at - interval '1 second';

    IF caller_count < caller_budget AND total_count < credential.total_budget THEN
        INSERT INTO shared_api_permits (credential_fingerprint, caller, permitted_at)
        VALUES (requested_fingerprint, requested_caller, now_at);
        RETURN QUERY SELECT true, now_at, NULL::timestamptz, credential.state;
        RETURN;
    END IF;

    IF caller_count >= caller_budget THEN
        SELECT min(permitted_at) + interval '1 second'
        INTO caller_next
        FROM shared_api_permits
        WHERE credential_fingerprint = requested_fingerprint
          AND caller = requested_caller
          AND permitted_at > now_at - interval '1 second';
    END IF;
    IF total_count >= credential.total_budget THEN
        SELECT min(permitted_at) + interval '1 second'
        INTO total_next
        FROM shared_api_permits
        WHERE credential_fingerprint = requested_fingerprint
          AND permitted_at > now_at - interval '1 second';
    END IF;

    RETURN QUERY SELECT
        false,
        now_at,
        GREATEST(COALESCE(caller_next, now_at), COALESCE(total_next, now_at)),
        credential.state;
END
$$;

CREATE OR REPLACE FUNCTION clashlens_cleanup_shared_api_permits(batch_size integer)
RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    deleted_count integer;
BEGIN
    IF batch_size < 1 OR batch_size > 10000 THEN
        RAISE EXCEPTION 'permit cleanup batch must be between 1 and 10000';
    END IF;
    WITH expired AS (
        SELECT id
        FROM shared_api_permits
        WHERE permitted_at < clock_timestamp() - interval '10 minutes'
        ORDER BY id
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    )
    DELETE FROM shared_api_permits AS permit
    USING expired
    WHERE permit.id = expired.id;
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END
$$;

-- Parsed evidence and occurrence processing remain separate so identical bytes
-- can be parsed once while every source occurrence stays attributable.
CREATE TABLE IF NOT EXISTS source_response_parses (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint text NOT NULL CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    response_hash text NOT NULL CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    parser_version text NOT NULL,
    source_adapter_version text NOT NULL,
    outcome text NOT NULL CHECK (
        outcome IN ('valid', 'non_success', 'malformed_json', 'unsupported_schema',
                    'identity_conflict', 'integrity_failure')
    ),
    parsed_json jsonb,
    failure_category text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (parser_version <> '' AND source_adapter_version <> ''),
    CHECK (length(COALESCE(failure_category, '')) <= 128),
    UNIQUE (endpoint, response_hash, parser_version, source_adapter_version)
);

CREATE TABLE IF NOT EXISTS processed_observation_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    parse_id bigint NOT NULL REFERENCES source_response_parses (id),
    processing_version text NOT NULL,
    outcome text NOT NULL CHECK (
        outcome IN ('applied', 'classified', 'failed', 'superseded')
    ),
    failure_category text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, processing_version)
);

CREATE TABLE IF NOT EXISTS player_discovery_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    normalized_tag text NOT NULL,
    source text NOT NULL CHECK (
        source IN ('submitted_tag', 'official_global_ranking',
                   'official_player_reference', 'account_link')
    ),
    source_observation_id bigint REFERENCES collector_observations (id),
    deduplication_key text NOT NULL UNIQUE,
    discovered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (normalized_tag ~ '^#[0289PYLQGRJCUV]+$'),
    CHECK (length(deduplication_key) BETWEEN 1 AND 512),
    CHECK ((source = 'official_global_ranking') = (source_observation_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS player_discovery_events_player_time_v2
    ON player_discovery_events (player_id, discovered_at DESC, id DESC);


-- Python-owned domain-processing relations. These names are authoritative for
-- the integrated worker; obsolete domain fragment alternatives are not loaded.
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

ALTER TABLE player_profile_versions
    ADD COLUMN IF NOT EXISTS eligibility_reason text NOT NULL DEFAULT 'legacy_unknown',
    ADD COLUMN IF NOT EXISTS source_contract_state text NOT NULL DEFAULT 'accepted',
    ADD COLUMN IF NOT EXISTS season_anchor_state text NOT NULL DEFAULT 'conflict';

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

CREATE TABLE IF NOT EXISTS known_player_discoveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    source_kind text NOT NULL CHECK (source_kind IN ('battle_opponent', 'official_ranking')),
    discovered_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, source_row_index, source_kind, player_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS known_player_discoveries_player_source
    ON known_player_discoveries (player_id, source_kind);

CREATE TABLE IF NOT EXISTS battle_log_observations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    player_id bigint NOT NULL REFERENCES players (id),
    parser_version text NOT NULL,
    observed_at timestamptz NOT NULL,
    row_count integer NOT NULL CHECK (row_count BETWEEN 0 AND 50),
    has_row_gap boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, parser_version)
);

CREATE TABLE IF NOT EXISTS battle_source_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    battle_log_observation_id bigint NOT NULL REFERENCES battle_log_observations (id),
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    outcome text NOT NULL CHECK (outcome IN (
        'valid_legend', 'ignored_non_legend', 'malformed_legend_row'
    )),
    failure_category text,
    source_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (battle_log_observation_id, source_row_index)
);

CREATE TABLE IF NOT EXISTS legend_battles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ranked_day_start timestamptz NOT NULL,
    attacker_player_id bigint NOT NULL REFERENCES players (id),
    defender_player_id bigint NOT NULL REFERENCES players (id),
    disagreement_state text NOT NULL DEFAULT 'single_perspective'
        CHECK (disagreement_state IN ('single_perspective', 'agreed', 'disagreement')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (attacker_player_id <> defender_player_id),
    UNIQUE (ranked_day_start, attacker_player_id, defender_player_id)
);

CREATE TABLE IF NOT EXISTS battle_evidence (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    battle_id bigint NOT NULL REFERENCES legend_battles (id),
    source_row_id bigint NOT NULL UNIQUE REFERENCES battle_source_rows (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    reporting_player_id bigint NOT NULL REFERENCES players (id),
    perspective text NOT NULL CHECK (perspective IN ('attacker', 'defender')),
    battle_timestamp timestamptz NOT NULL,
    stars integer NOT NULL CHECK (stars BETWEEN 0 AND 3),
    destruction_percentage integer NOT NULL CHECK (destruction_percentage BETWEEN 0 AND 100),
    army_share_code text NOT NULL,
    reporter_trophies integer,
    opponent_trophies integer,
    attacker_gain integer NOT NULL CHECK (attacker_gain >= 0),
    defender_loss integer NOT NULL CHECK (defender_loss >= 0),
    trophy_rule_version text NOT NULL,
    source_observed_at timestamptz NOT NULL,
    parser_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS battle_evidence_battle_perspective_time
    ON battle_evidence (battle_id, perspective, source_observed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS battle_perspectives (
    battle_id bigint NOT NULL REFERENCES legend_battles (id),
    perspective text NOT NULL CHECK (perspective IN ('attacker', 'defender')),
    evidence_id bigint NOT NULL UNIQUE REFERENCES battle_evidence (id),
    source_observed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (battle_id, perspective)
);

CREATE TABLE IF NOT EXISTS reset_baseline_evidence (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sweep_id bigint REFERENCES collector_reset_sweeps (id),
    player_id bigint NOT NULL REFERENCES players (id),
    boundary_at timestamptz NOT NULL,
    profile_observation_id bigint REFERENCES collector_observations (id),
    battle_log_observation_id bigint REFERENCES collector_observations (id),
    profile_valid boolean NOT NULL DEFAULT false,
    battle_log_valid boolean NOT NULL DEFAULT false,
    legacy_profile_only boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (sweep_id, player_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS reset_baseline_evidence_boundary_player
    ON reset_baseline_evidence (boundary_at, player_id)
    WHERE sweep_id IS NULL;

-- Versioned paired reset evidence is the only reset-baseline proof consumed by
-- ranked-day reconciliation. Existing profile-only or manually linked rows
-- remain readable, but they cannot become a paired complete baseline.
ALTER TABLE legend_battles
    ADD COLUMN IF NOT EXISTS disagreement_fields text[] NOT NULL DEFAULT ARRAY[]::text[];

ALTER TABLE reset_baseline_evidence
    DROP CONSTRAINT IF EXISTS reset_baseline_evidence_sweep_id_player_id_key,
    ADD COLUMN IF NOT EXISTS reset_baseline_sweep_id bigint,
    ADD COLUMN IF NOT EXISTS collection_job_id bigint,
    ADD COLUMN IF NOT EXISTS attempt_id bigint,
    ADD COLUMN IF NOT EXISTS profile_processing_outcome_id bigint,
    ADD COLUMN IF NOT EXISTS battle_log_processing_outcome_id bigint,
    ADD COLUMN IF NOT EXISTS parser_version text NOT NULL DEFAULT 'supercell-source-parser-v1',
    ADD COLUMN IF NOT EXISTS processing_version text NOT NULL DEFAULT 'clashlens-domain-processing-v1',
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS supersedes_id bigint,
    ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'partial',
    ADD COLUMN IF NOT EXISTS failure_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS evidence_key text,
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT clock_timestamp();

UPDATE reset_baseline_evidence AS evidence
SET reset_baseline_sweep_id = baseline.id
FROM collector_reset_baseline_sweeps AS baseline
WHERE evidence.reset_baseline_sweep_id IS NULL
  AND evidence.sweep_id = baseline.reset_sweep_id
  AND evidence.player_id = baseline.player_id;

UPDATE reset_baseline_evidence
SET state = 'partial',
    failure_reasons = CASE
        WHEN legacy_profile_only THEN '["legacy_profile_only"]'::jsonb
        WHEN collection_job_id IS NULL OR attempt_id IS NULL
            THEN '["unbound_reset_evidence"]'::jsonb
        ELSE failure_reasons
    END
WHERE collection_job_id IS NULL
  AND (
        state = 'complete'
        OR (profile_valid AND battle_log_valid)
      );

UPDATE reset_baseline_evidence
SET evidence_key = format(
    'legacy-reset-baseline:%s:%s:%s',
    COALESCE(sweep_id, 0), player_id, id
)
WHERE evidence_key IS NULL;

ALTER TABLE reset_baseline_evidence
    ALTER COLUMN evidence_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_reset_sweep_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_reset_sweep_v2_fk
            FOREIGN KEY (reset_baseline_sweep_id)
            REFERENCES collector_reset_baseline_sweeps (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_collection_job_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_collection_job_v2_fk
            FOREIGN KEY (collection_job_id)
            REFERENCES collector_jobs (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_attempt_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_attempt_v2_fk
            FOREIGN KEY (attempt_id)
            REFERENCES collector_attempts (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_profile_processing_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_profile_processing_v2_fk
            FOREIGN KEY (profile_processing_outcome_id)
            REFERENCES observation_processing_outcomes (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_battle_processing_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_battle_processing_v2_fk
            FOREIGN KEY (battle_log_processing_outcome_id)
            REFERENCES observation_processing_outcomes (id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_supersedes_v2_fk'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_supersedes_v2_fk
            FOREIGN KEY (supersedes_id)
            REFERENCES reset_baseline_evidence (id);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_state_v2_check'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_state_v2_check
            CHECK (state IN ('partial', 'complete', 'failed'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_version_v2_check'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_version_v2_check
            CHECK (version > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_reason_array_v2_check'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_reason_array_v2_check
            CHECK (jsonb_typeof(failure_reasons) = 'array');
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reset_baseline_evidence'::regclass
          AND conname = 'reset_baseline_evidence_complete_v2_check'
    ) THEN
        ALTER TABLE reset_baseline_evidence
            ADD CONSTRAINT reset_baseline_evidence_complete_v2_check
            CHECK (
                state <> 'complete'
                OR (
                    reset_baseline_sweep_id IS NOT NULL
                    AND collection_job_id IS NOT NULL
                    AND attempt_id IS NOT NULL
                    AND profile_observation_id IS NOT NULL
                    AND battle_log_observation_id IS NOT NULL
                    AND profile_processing_outcome_id IS NOT NULL
                    AND battle_log_processing_outcome_id IS NOT NULL
                    AND profile_valid
                    AND battle_log_valid
                    AND NOT legacy_profile_only
                    AND jsonb_array_length(failure_reasons) = 0
                )
            );
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'legend_battles'::regclass
          AND conname = 'legend_battles_disagreement_fields_v2_check'
    ) THEN
        ALTER TABLE legend_battles
            ADD CONSTRAINT legend_battles_disagreement_fields_v2_check
            CHECK (
                disagreement_fields <@ ARRAY[
                    'battle_timestamp', 'stars', 'destruction_percentage',
                    'army_share_code', 'attacker_trophies', 'defender_trophies',
                    'attacker_gain', 'defender_loss'
                ]::text[]
            );
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS reset_baseline_evidence_sweep_version_v2
    ON reset_baseline_evidence (reset_baseline_sweep_id, version)
    WHERE reset_baseline_sweep_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS reset_baseline_evidence_sweep_key_v2
    ON reset_baseline_evidence (reset_baseline_sweep_id, evidence_key)
    WHERE reset_baseline_sweep_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS reset_baseline_evidence_lookup_v2
    ON reset_baseline_evidence (player_id, boundary_at, state, version DESC);

CREATE OR REPLACE FUNCTION clashlens_reset_job_lineage_v2(
    observed_job_id bigint,
    root_job_id bigint
)
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    WITH RECURSIVE lineage(id, parent_attempt_id) AS (
        SELECT job.id, job.parent_attempt_id
        FROM collector_jobs AS job
        WHERE job.id = observed_job_id
        UNION
        SELECT parent_job.id, parent_job.parent_attempt_id
        FROM lineage AS child
        JOIN collector_attempts AS parent_attempt
          ON parent_attempt.id = child.parent_attempt_id
        JOIN collector_jobs AS parent_job
          ON parent_job.id = parent_attempt.job_id
    )
    SELECT EXISTS (SELECT 1 FROM lineage WHERE id = root_job_id)
$$;

CREATE OR REPLACE FUNCTION clashlens_validate_reset_baseline_evidence_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    baseline collector_reset_baseline_sweeps%ROWTYPE;
    root_job collector_jobs%ROWTYPE;
    attempt_job_id bigint;
    source_observation collector_observations%ROWTYPE;
    processing observation_processing_outcomes%ROWTYPE;
    profile_version_exists boolean;
    battle_log_valid boolean;
BEGIN
    IF NEW.evidence_key IS NULL THEN
        NEW.evidence_key := format(
            'reset-baseline:%s:%s:%s:%s:%s:%s:%s',
            COALESCE(NEW.reset_baseline_sweep_id, 0),
            NEW.version,
            COALESCE(NEW.profile_observation_id, 0),
            COALESCE(NEW.battle_log_observation_id, 0),
            NEW.state,
            NEW.profile_valid,
            NEW.battle_log_valid
        );
    END IF;

    IF NEW.reset_baseline_sweep_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT * INTO baseline
    FROM collector_reset_baseline_sweeps
    WHERE id = NEW.reset_baseline_sweep_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reset evidence references an unknown baseline sweep';
    END IF;
    IF NEW.sweep_id IS DISTINCT FROM baseline.reset_sweep_id
       OR NEW.player_id IS DISTINCT FROM baseline.player_id
       OR NEW.boundary_at IS DISTINCT FROM baseline.boundary_at THEN
        RAISE EXCEPTION 'reset evidence identity does not match its immutable sweep';
    END IF;

    IF NEW.collection_job_id IS NULL THEN
        IF NEW.state = 'complete' THEN
            RAISE EXCEPTION 'complete reset evidence requires a collection job';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO root_job
    FROM collector_jobs
    WHERE id = NEW.collection_job_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reset evidence references an unknown collection job';
    END IF;
    IF root_job.work_type NOT IN ('reset_baseline', 'legacy_reset_profile')
       OR root_job.reset_baseline_sweep_id IS DISTINCT FROM NEW.reset_baseline_sweep_id
       OR root_job.sweep_id IS DISTINCT FROM baseline.reset_sweep_id
       OR root_job.player_id IS DISTINCT FROM NEW.player_id THEN
        RAISE EXCEPTION 'reset evidence collection job identity does not match';
    END IF;
    IF NEW.attempt_id IS NULL THEN
        IF NEW.state = 'complete' THEN
            RAISE EXCEPTION 'complete reset evidence requires an attempt';
        END IF;
    ELSE
        SELECT job_id INTO attempt_job_id
        FROM collector_attempts
        WHERE id = NEW.attempt_id;
        IF attempt_job_id IS DISTINCT FROM root_job.id THEN
            RAISE EXCEPTION 'reset evidence attempt is not the root attempt';
        END IF;
    END IF;

    IF NEW.profile_observation_id IS NOT NULL THEN
        SELECT * INTO source_observation
        FROM collector_observations
        WHERE id = NEW.profile_observation_id;
        IF NOT FOUND OR source_observation.endpoint <> 'profile' THEN
            RAISE EXCEPTION 'reset evidence profile reference is not a profile observation';
        END IF;
        IF NEW.state = 'complete' THEN
            IF source_observation.attempt_id IS DISTINCT FROM NEW.attempt_id
               OR source_observation.player_id IS DISTINCT FROM NEW.player_id
               OR source_observation.response_completed_at < baseline.boundary_at
               OR NOT clashlens_reset_job_lineage_v2(
                    source_observation.collection_job_id, root_job.id
               ) THEN
                RAISE EXCEPTION 'complete reset evidence profile is outside its job attempt';
            END IF;
        END IF;
    END IF;

    IF NEW.battle_log_observation_id IS NOT NULL THEN
        SELECT * INTO source_observation
        FROM collector_observations
        WHERE id = NEW.battle_log_observation_id;
        IF NOT FOUND OR source_observation.endpoint <> 'battle_log' THEN
            RAISE EXCEPTION 'reset evidence battle-log reference is not a battle-log observation';
        END IF;
        IF NEW.state = 'complete' THEN
            IF source_observation.attempt_id IS DISTINCT FROM NEW.attempt_id
               OR source_observation.player_id IS DISTINCT FROM NEW.player_id
               OR source_observation.response_completed_at < baseline.boundary_at
               OR NOT clashlens_reset_job_lineage_v2(
                    source_observation.collection_job_id, root_job.id
               ) THEN
                RAISE EXCEPTION 'complete reset evidence battle log is outside its job attempt';
            END IF;
        END IF;
    END IF;

    IF NEW.state = 'complete' THEN
        SELECT * INTO processing
        FROM observation_processing_outcomes
        WHERE id = NEW.profile_processing_outcome_id;
        IF NOT FOUND OR processing.endpoint <> 'profile'
           OR processing.observation_id IS DISTINCT FROM NEW.profile_observation_id
           OR processing.outcome <> 'processed' THEN
            RAISE EXCEPTION 'complete reset evidence profile was not successfully processed';
        END IF;
        SELECT * INTO processing
        FROM observation_processing_outcomes
        WHERE id = NEW.battle_log_processing_outcome_id;
        IF NOT FOUND OR processing.endpoint <> 'battle_log'
           OR processing.observation_id IS DISTINCT FROM NEW.battle_log_observation_id
           OR processing.outcome <> 'processed' THEN
            RAISE EXCEPTION 'complete reset evidence battle log was not successfully processed';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM player_profile_versions AS profile
            WHERE profile.observation_id = NEW.profile_observation_id
              AND profile.parser_version = NEW.parser_version
              AND profile.source_contract_state = 'accepted'
              AND profile.eligibility_state = 'eligible'
        ) INTO profile_version_exists;
        IF NOT profile_version_exists THEN
            RAISE EXCEPTION 'complete reset evidence profile is not accepted Legend I evidence';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM battle_log_observations AS log
            WHERE log.observation_id = NEW.battle_log_observation_id
              AND log.parser_version = NEW.parser_version
              AND NOT log.has_row_gap
        ) INTO battle_log_valid;
        IF NOT battle_log_valid THEN
            RAISE EXCEPTION 'complete reset evidence battle log contains a parse gap';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM battle_evidence AS evidence
            JOIN legend_battles AS battle ON battle.id = evidence.battle_id
            JOIN collector_observations AS profile_observation
              ON profile_observation.id = NEW.profile_observation_id
            WHERE (
                    battle.attacker_player_id = NEW.player_id
                    OR battle.defender_player_id = NEW.player_id
                )
              AND evidence.battle_timestamp >= baseline.boundary_at
              AND evidence.battle_timestamp < baseline.boundary_at + interval '1 day'
              AND evidence.battle_timestamp <= profile_observation.response_completed_at
        ) THEN
            RAISE EXCEPTION 'complete reset evidence profile does not precede the first retained event';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM collector_endpoint_results AS result
            WHERE result.attempt_id = NEW.attempt_id
              AND result.endpoint = 'profile'
              AND result.observation_id = NEW.profile_observation_id
              AND result.outcome = 'observed'
        ) OR NOT EXISTS (
            SELECT 1 FROM collector_endpoint_results AS result
            WHERE result.attempt_id = NEW.attempt_id
              AND result.endpoint = 'battle_log'
              AND result.observation_id = NEW.battle_log_observation_id
              AND result.outcome = 'observed'
        ) THEN
            RAISE EXCEPTION 'complete reset evidence does not match both endpoint results';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS reset_baseline_evidence_validate_v2
    ON reset_baseline_evidence;
CREATE TRIGGER reset_baseline_evidence_validate_v2
BEFORE INSERT OR UPDATE ON reset_baseline_evidence
FOR EACH ROW
EXECUTE FUNCTION clashlens_validate_reset_baseline_evidence_v2();

CREATE TABLE IF NOT EXISTS ranked_day_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    ranked_day_start timestamptz NOT NULL,
    ranked_day_end timestamptz NOT NULL,
    official_season_id text NOT NULL,
    season_day_number integer NOT NULL CHECK (season_day_number BETWEEN 1 AND 28),
    season_anchor_rule_version text NOT NULL,
    reconciliation_rule_version text NOT NULL,
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL CHECK (version > 0),
    replaces_version_id bigint REFERENCES ranked_day_versions (id),
    state text NOT NULL CHECK (state IN ('Live', 'Complete', 'Partial')),
    confidence text NOT NULL CHECK (confidence IN ('exact', 'inferred', 'partial', 'uncertain')),
    failure_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    start_trophies integer,
    final_trophies_before_reset integer,
    next_start_trophies integer,
    attack_count integer NOT NULL DEFAULT 0 CHECK (attack_count BETWEEN 0 AND 8),
    defense_count integer NOT NULL DEFAULT 0 CHECK (defense_count BETWEEN 0 AND 8),
    attack_gain integer NOT NULL DEFAULT 0 CHECK (attack_gain >= 0),
    observed_defense_loss integer NOT NULL DEFAULT 0 CHECK (observed_defense_loss >= 0),
    automatic_defense_loss integer,
    evidence_complete boolean NOT NULL DEFAULT false,
    reconciled boolean NOT NULL DEFAULT false,
    shield_state text NOT NULL DEFAULT 'not_inferred'
        CHECK (shield_state IN (
            'not_inferred', 'not_shielded', 'inferred_shielded', 'uncertain_sequence'
        )),
    shield_duration_days integer,
    start_baseline_id bigint REFERENCES reset_baseline_evidence (id),
    end_baseline_id bigint REFERENCES reset_baseline_evidence (id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (player_id, ranked_day_start, reconciliation_rule_version, version),
    UNIQUE (player_id, ranked_day_start, reconciliation_rule_version, result_hash)
);
CREATE INDEX IF NOT EXISTS ranked_day_versions_latest
    ON ranked_day_versions (player_id, ranked_day_start, reconciliation_rule_version, version DESC);

CREATE TABLE IF NOT EXISTS ranked_day_adjustments (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ranked_day_version_id bigint NOT NULL REFERENCES ranked_day_versions (id),
    adjustment_type text NOT NULL CHECK (adjustment_type IN (
        'automatic_defense', 'weekly_reset', 'season_reset'
    )),
    amount integer NOT NULL,
    evidence_state text NOT NULL CHECK (evidence_state IN ('calculated', 'confirmed', 'official_rule')),
    rule_version text NOT NULL,
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (ranked_day_version_id, adjustment_type)
);

CREATE TABLE IF NOT EXISTS official_top200_attempts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    parser_version text NOT NULL,
    outcome text NOT NULL CHECK (outcome IN (
        'official_observed', 'official_partial', 'official_contract_changed',
        'non_success', 'malformed', 'unsupported', 'integrity_failure'
    )),
    failure_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
    observed_at timestamptz NOT NULL,
    season_provenance text NOT NULL DEFAULT 'not_supplied',
    official_season_id text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (observation_id, parser_version)
);

CREATE TABLE IF NOT EXISTS official_top200_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    attempt_id bigint NOT NULL UNIQUE REFERENCES official_top200_attempts (id),
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    observed_at timestamptz NOT NULL,
    parser_version text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS official_top200_entries (
    version_id bigint NOT NULL REFERENCES official_top200_versions (id),
    rank integer NOT NULL CHECK (rank BETWEEN 1 AND 200),
    player_id bigint NOT NULL REFERENCES players (id),
    normalized_tag text NOT NULL,
    source_json jsonb NOT NULL,
    PRIMARY KEY (version_id, rank),
    UNIQUE (version_id, player_id),
    UNIQUE (version_id, normalized_tag)
);

CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_kind text NOT NULL CHECK (snapshot_kind IN ('frozen', 'live')),
    boundary_at timestamptz NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    correction_of_id bigint REFERENCES leaderboard_snapshots (id),
    ordering_rule_version text NOT NULL,
    freshness_rule_version text NOT NULL,
    state text NOT NULL CHECK (state IN ('building', 'published', 'superseded')),
    source_ranked_day_version_id bigint REFERENCES ranked_day_versions (id),
    measured_coverage numeric(6,5) NOT NULL CHECK (measured_coverage BETWEEN 0 AND 1),
    stale_entry_count integer NOT NULL CHECK (stale_entry_count >= 0),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_kind, boundary_at, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS leaderboard_snapshots_current_published
    ON leaderboard_snapshots (snapshot_kind, boundary_at)
    WHERE state = 'published';

CREATE TABLE IF NOT EXISTS leaderboard_snapshot_entries (
    snapshot_id bigint NOT NULL REFERENCES leaderboard_snapshots (id),
    position integer NOT NULL CHECK (position > 0),
    player_id bigint NOT NULL REFERENCES players (id),
    trophies integer NOT NULL,
    trophy_observation_id bigint NOT NULL REFERENCES collector_observations (id),
    trophy_observed_at timestamptz NOT NULL,
    observation_age_seconds integer NOT NULL CHECK (observation_age_seconds >= 0),
    freshness text NOT NULL CHECK (freshness IN ('fresh', 'stale')),
    confidence text NOT NULL CHECK (confidence IN ('confirmed', 'uncertain')),
    tie_hash text NOT NULL CHECK (tie_hash ~ '^[0-9a-f]{64}$'),
    official_rank integer,
    official_rank_version_id bigint REFERENCES official_top200_versions (id),
    official_rank_observed_at timestamptz,
    PRIMARY KEY (snapshot_id, position),
    UNIQUE (snapshot_id, player_id)
);

CREATE TABLE IF NOT EXISTS analytics_summaries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id bigint REFERENCES leaderboard_snapshots (id),
    lens text NOT NULL CHECK (lens IN ('offense', 'defense')),
    population_filter jsonb NOT NULL,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    measured_coverage numeric(6,5) NOT NULL CHECK (measured_coverage BETWEEN 0 AND 1),
    freshness text NOT NULL,
    classification_version text NOT NULL,
    classification_confidence text NOT NULL,
    unclassified_count integer NOT NULL CHECK (unclassified_count >= 0),
    disagreement_count integer NOT NULL CHECK (disagreement_count >= 0),
    analytics_rule_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (snapshot_id, lens, population_filter, period_start, period_end, analytics_rule_version)
);

CREATE TABLE IF NOT EXISTS analytics_breakdowns (
    summary_id bigint NOT NULL REFERENCES analytics_summaries (id),
    army_archetype text NOT NULL,
    attack_count integer NOT NULL CHECK (attack_count >= 0),
    three_star_count integer NOT NULL CHECK (three_star_count >= 0),
    usage_rate numeric(8,7),
    three_star_rate numeric(8,7),
    PRIMARY KEY (summary_id, army_archetype)
);

-- Python API and account relations. Discord and export delivery remain dormant;
-- these tables only provide durable request and account seams.
CREATE TABLE IF NOT EXISTS clash_lens_accounts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    username text NOT NULL,
    normalized_username text NOT NULL UNIQUE CHECK (
        normalized_username ~ '^[a-z][a-z0-9_]{2,31}$'
    ),
    display_name text NOT NULL CHECK (
        char_length(display_name) BETWEEN 1 AND 80
    ),
    preferences jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(preferences) = 'object'
        AND octet_length(preferences::text) <= 4096
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS account_provider_identities (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider = 'google'),
    provider_subject text NOT NULL CHECK (
        char_length(provider_subject) BETWEEN 1 AND 255
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (provider, provider_subject),
    UNIQUE (account_id, provider)
);

CREATE TABLE IF NOT EXISTS account_saved_players (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, player_id)
);

CREATE TABLE IF NOT EXISTS account_groups (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
    normalized_name text NOT NULL CHECK (char_length(normalized_name) BETWEEN 1 AND 80),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (account_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS account_group_players (
    group_id bigint NOT NULL REFERENCES account_groups (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (group_id, player_id)
);

CREATE TABLE IF NOT EXISTS private_api_requests (
    request_id uuid PRIMARY KEY,
    caller text NOT NULL,
    provider text NOT NULL,
    provider_subject text NOT NULL,
    account_id bigint REFERENCES clash_lens_accounts (id),
    operation text NOT NULL,
    method text NOT NULL CHECK (method ~ '^[A-Z]+$'),
    request_target text NOT NULL,
    identity_json jsonb NOT NULL CHECK (
        jsonb_typeof(identity_json) = 'object'
        AND octet_length(identity_json::text) <= 4096
    ),
    state text NOT NULL CHECK (state IN ('reserved', 'complete')),
    response_status integer,
    response_json jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (
        (state = 'reserved' AND response_status IS NULL AND response_json IS NULL)
        OR (state = 'complete' AND response_status IS NOT NULL AND response_json IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS private_api_requests_account_time_v2
    ON private_api_requests (account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_refresh_requests (
    public_id uuid PRIMARY KEY,
    collector_job_id bigint NOT NULL UNIQUE REFERENCES collector_jobs (id),
    normalized_tag text NOT NULL,
    initial_outcome text NOT NULL CHECK (
        initial_outcome IN ('created', 'coalesced', 'cooldown_hit', 'partial_retry')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS verified_player_links (
    player_id bigint PRIMARY KEY REFERENCES players (id) ON DELETE CASCADE,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    verification_request_id uuid NOT NULL UNIQUE REFERENCES private_api_requests (request_id),
    verified_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS verified_player_links_account_v2
    ON verified_player_links (account_id, verified_at, player_id);

CREATE TABLE IF NOT EXISTS player_link_verification_audits (
    request_id uuid PRIMARY KEY REFERENCES private_api_requests (request_id),
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id),
    player_id bigint NOT NULL REFERENCES players (id),
    outcome text NOT NULL CHECK (
        outcome IN (
            'pending',
            'verified',
            'linked',
            'already_linked',
            'support_required',
            'invalid_token',
            'verification_unavailable'
        )
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS player_link_verification_audits_account_time_v2
    ON player_link_verification_audits (account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS support_player_link_transfer_candidates (
    verification_request_id uuid PRIMARY KEY
        REFERENCES player_link_verification_audits (request_id),
    player_id bigint NOT NULL REFERENCES players (id),
    from_account_id bigint NOT NULL REFERENCES clash_lens_accounts (id),
    to_account_id bigint NOT NULL REFERENCES clash_lens_accounts (id),
    verified_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'completed', 'expired')),
    completed_at timestamptz,
    CHECK (from_account_id <> to_account_id),
    CHECK (expires_at > verified_at)
);

CREATE TABLE IF NOT EXISTS support_player_link_transfer_audits (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    verification_request_id uuid NOT NULL UNIQUE
        REFERENCES support_player_link_transfer_candidates (verification_request_id),
    player_id bigint NOT NULL REFERENCES players (id),
    from_account_id bigint NOT NULL REFERENCES clash_lens_accounts (id),
    to_account_id bigint NOT NULL REFERENCES clash_lens_accounts (id),
    operator_identity text NOT NULL CHECK (char_length(operator_identity) BETWEEN 1 AND 255),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 8 AND 500),
    transferred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS account_export_requests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format IN ('google_sheets_scaffold', 'csv_scaffold')),
    state text NOT NULL CHECK (
        state IN ('pending', 'leased', 'complete', 'failed', 'cancelled')
    ),
    result_reference text,
    safe_failure text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS account_export_requests_account_time_v2
    ON account_export_requests (account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS api_player_daily_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    ranked_day_start timestamptz NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    state text NOT NULL CHECK (state IN ('Live', 'Complete', 'Partial')),
    coverage text NOT NULL,
    adjustments jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(adjustments) = 'array'
        AND octet_length(adjustments::text) <= 65536
    ),
    battles jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(battles) = 'array'
        AND octet_length(battles::text) <= 262144
    ),
    partial_reasons jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(partial_reasons) = 'array'
        AND octet_length(partial_reasons::text) <= 16384
    ),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (player_id, ranked_day_start, version)
);
CREATE INDEX IF NOT EXISTS api_player_daily_logs_current_v2
    ON api_player_daily_logs (player_id, ranked_day_start DESC, version DESC);

CREATE TABLE IF NOT EXISTS api_frozen_leaderboards (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL UNIQUE,
    boundary_at timestamptz NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    ordering_rule_version text NOT NULL,
    coverage jsonb NOT NULL CHECK (jsonb_typeof(coverage) = 'object'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    supersedes_public_id uuid,
    UNIQUE (boundary_at, version)
);

CREATE TABLE IF NOT EXISTS api_frozen_leaderboard_entries (
    leaderboard_id bigint NOT NULL REFERENCES api_frozen_leaderboards (id) ON DELETE CASCADE,
    position integer NOT NULL CHECK (position > 0),
    player_id bigint NOT NULL REFERENCES players (id),
    trophies integer NOT NULL,
    observed_at timestamptz NOT NULL,
    freshness text NOT NULL,
    confidence text NOT NULL,
    official_rank integer CHECK (official_rank BETWEEN 1 AND 200),
    PRIMARY KEY (leaderboard_id, position),
    UNIQUE (leaderboard_id, player_id)
);

CREATE OR REPLACE FUNCTION clashlens_enqueue_interactive(
    requested_type text,
    requested_tag text,
    cooldown_seconds integer DEFAULT 300
)
RETURNS TABLE (job_id bigint, attempt_id bigint, outcome text, reused boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    selected_player_id bigint;
    selected_job_id bigint;
    selected_attempt_id bigint;
    selected_outcome text;
    selected_reused boolean;
    selected_key text;
BEGIN
    IF requested_type NOT IN ('initial_collection', 'live_refresh') THEN
        RAISE EXCEPTION 'unsupported interactive work type';
    END IF;
    IF requested_tag !~ '^#[0289PYLQGRJCUV]{3,15}$' THEN
        RAISE EXCEPTION 'invalid normalized player tag';
    END IF;
    IF cooldown_seconds < 0 OR cooldown_seconds > 3600 THEN
        RAISE EXCEPTION 'interactive cooldown is outside the supported range';
    END IF;

    INSERT INTO players (normalized_tag, active)
    VALUES (requested_tag, false)
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id INTO selected_player_id
    FROM players
    WHERE normalized_tag = requested_tag;

    selected_key := requested_type || ':' || requested_tag;
    SELECT j.id, j.result_attempt_id
    INTO selected_job_id, selected_attempt_id
    FROM collector_jobs AS j
    WHERE j.coalescing_key = selected_key
      AND j.status IN ('pending', 'leased', 'waiting_retry')
    ORDER BY j.id DESC
    LIMIT 1
    FOR UPDATE;

    IF selected_job_id IS NOT NULL THEN
        selected_outcome := 'coalesced';
        selected_reused := true;
    ELSE
        SELECT j.id, j.result_attempt_id
        INTO selected_job_id, selected_attempt_id
        FROM collector_jobs AS j
        WHERE j.coalescing_key = selected_key
          AND j.status = 'complete'
          AND j.updated_at >= clock_timestamp() - make_interval(secs => cooldown_seconds)
        ORDER BY j.updated_at DESC, j.id DESC
        LIMIT 1;
        IF selected_job_id IS NOT NULL THEN
            selected_outcome := 'cooldown_hit';
            selected_reused := true;
        ELSE
            BEGIN
                INSERT INTO collector_jobs (
                    work_type, player_id, normalized_tag, capacity_pool,
                    priority, due_at, coalescing_key, status
                ) VALUES (
                    requested_type, selected_player_id, requested_tag, 'interactive',
                    300, clock_timestamp(), selected_key, 'pending'
                )
                RETURNING id INTO selected_job_id;
                selected_attempt_id := NULL;
                selected_outcome := 'created';
                selected_reused := false;
            EXCEPTION WHEN unique_violation THEN
                SELECT j.id, j.result_attempt_id
                INTO selected_job_id, selected_attempt_id
                FROM collector_jobs AS j
                WHERE j.coalescing_key = selected_key
                  AND j.status IN ('pending', 'leased', 'waiting_retry')
                ORDER BY j.id DESC
                LIMIT 1;
                selected_outcome := 'coalesced';
                selected_reused := true;
            END;
        END IF;
    END IF;

    INSERT INTO collector_interactive_intent_events (
        requested_work_type, normalized_tag, requested_at, outcome,
        result_job_id, result_attempt_id
    ) VALUES (
        requested_type, requested_tag, clock_timestamp(), selected_outcome,
        selected_job_id, selected_attempt_id
    );

    RETURN QUERY SELECT
        selected_job_id, selected_attempt_id, selected_outcome, selected_reused;
END
$$;

-- Keep the collector-owned contract at version two after all Python-owned
-- relations are present. This is safe to repeat on a populated v1 database.
UPDATE clash_lens_contract
SET version = 2
WHERE singleton;

INSERT INTO clash_lens_schema_migrations (version)
VALUES (2)
ON CONFLICT (version) DO NOTHING;

COMMIT;
