-- Clash Lens deployment migration 0026.
-- The Python collector owns regular fetch admission.  Keep one compact
-- freshness row per endpoint identity, one durable row per changed response,
-- and one upload state per raw response hash.  The retired collector admission
-- tree is replaced below; no live collector rows need to be preserved.
BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE current_version integer;
BEGIN
    SELECT version INTO current_version
    FROM clash_lens_contract
    WHERE singleton;
    IF current_version <> 5 THEN
        RAISE EXCEPTION 'Python collector storage requires contract version 5 (got %)', current_version;
    END IF;
END
$$;

DROP TABLE IF EXISTS collector_regular_admission_evidence;
DROP TABLE IF EXISTS collector_regular_admission_evidence_runs;
DROP TABLE IF EXISTS collector_endpoint_budgets;

-- The Python collector now owns the shared interactive key.  Keep the
-- thirty-request rolling window, but move its 29-request lane out of the
-- retired Go owner.
ALTER TABLE shared_api_credentials
    ADD COLUMN IF NOT EXISTS collector_budget integer NOT NULL DEFAULT 29;
ALTER TABLE shared_api_credentials
    DROP CONSTRAINT IF EXISTS shared_api_credentials_collector_budget_v4_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_budget_split_v4_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_go_budget_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_go_interactive_budget_v3_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_go_recovery_budget_v3_check,
    DROP CONSTRAINT IF EXISTS shared_api_credentials_budget_split_v3_check,
    ADD CONSTRAINT shared_api_credentials_collector_budget_v4_check
        CHECK (collector_budget = 29),
    ADD CONSTRAINT shared_api_credentials_budget_split_v4_check
        CHECK (collector_budget + python_budget = total_budget);
ALTER TABLE shared_api_credentials
    DROP COLUMN go_budget,
    DROP COLUMN go_interactive_budget,
    DROP COLUMN go_recovery_budget;

ALTER TABLE shared_api_permits
    DROP CONSTRAINT IF EXISTS shared_api_permits_caller_v3_check,
    DROP CONSTRAINT IF EXISTS shared_api_permits_caller_check,
    DROP CONSTRAINT IF EXISTS shared_api_permits_caller_v4_check;
ALTER TABLE shared_api_permits
    ADD CONSTRAINT shared_api_permits_caller_v4_check
        CHECK (caller IN ('collector', 'python'));

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
    IF requested_caller NOT IN ('collector', 'python') THEN
        RAISE EXCEPTION 'shared API caller must be collector or python';
    END IF;
    PERFORM clashlens_cleanup_shared_api_permits(100);
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
        RETURN QUERY SELECT false, now_at,
            CASE credential.state WHEN 'cooldown' THEN credential.cooldown_until
                                  ELSE NULL::timestamptz END,
            credential.state;
        RETURN;
    END IF;
    caller_budget := CASE requested_caller
        WHEN 'collector' THEN credential.collector_budget
        ELSE credential.python_budget
    END;
    SELECT count(*) FILTER (WHERE caller = requested_caller), count(*)
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
        SELECT min(permitted_at) + interval '1 second' INTO caller_next
        FROM shared_api_permits
        WHERE credential_fingerprint = requested_fingerprint
          AND caller = requested_caller
          AND permitted_at > now_at - interval '1 second';
    END IF;
    IF total_count >= credential.total_budget THEN
        SELECT min(permitted_at) + interval '1 second' INTO total_next
        FROM shared_api_permits
        WHERE credential_fingerprint = requested_fingerprint
          AND permitted_at > now_at - interval '1 second';
    END IF;
    RETURN QUERY SELECT false, now_at,
        GREATEST(COALESCE(caller_next, now_at), COALESCE(total_next, now_at)),
        credential.state;
END
$$;

ALTER FUNCTION clashlens_acquire_shared_api_permit(text, text) SECURITY DEFINER;

-- Responses may be published before their archive location is known.  The
-- compact replacement below removes the retired job and attempt references.
ALTER TABLE collector_observations
    ALTER COLUMN archive_reference DROP NOT NULL;

ALTER TABLE collector_observations
    DROP CONSTRAINT IF EXISTS collector_observations_archive_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_archive_v4_check,
    DROP CONSTRAINT IF EXISTS collector_observations_archive_pair_v4_check,
    DROP CONSTRAINT IF EXISTS collector_observations_catalogue_required_v3;
ALTER TABLE collector_observations
    ADD CONSTRAINT collector_observations_archive_v4_check CHECK (
        (archive_reference IS NULL OR archive_reference <> '')
        AND collector_version <> ''
        AND source_adapter_version <> ''
    ),
    ADD CONSTRAINT collector_observations_archive_pair_v4_check CHECK (
        (archive_reference IS NULL AND archive_catalogue_hash IS NULL)
        OR (
            archive_reference IS NOT NULL
            AND archive_catalogue_hash = response_hash
        )
    );

-- The leaguehistory endpoint joins the collected player set.
ALTER TABLE collector_observations
    DROP CONSTRAINT IF EXISTS collector_observations_endpoint_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_scope_v2_check,
    DROP CONSTRAINT IF EXISTS collector_observations_request_v2_check,
    ADD CONSTRAINT collector_observations_endpoint_v2_check CHECK (
        endpoint IN (
            'profile', 'battle_log', 'global_player_rankings', 'league_history'
        )
    ),
    ADD CONSTRAINT collector_observations_scope_v2_check CHECK (
        (scope = 'player' AND player_id IS NOT NULL
            AND normalized_tag IS NOT NULL
            AND endpoint IN ('profile', 'battle_log', 'league_history'))
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL
            AND endpoint = 'global_player_rankings')
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
            OR (endpoint IN ('profile', 'battle_log', 'league_history')
                AND request_query = ''
                AND paging_envelope_state = 'not_applicable')
        )
    );
ALTER TABLE collector_transport_failures
    DROP CONSTRAINT IF EXISTS collector_transport_failures_endpoint_v2_check,
    DROP CONSTRAINT IF EXISTS collector_transport_failures_scope_v2_check,
    ADD CONSTRAINT collector_transport_failures_endpoint_v2_check CHECK (
        endpoint IN (
            'profile', 'battle_log', 'global_player_rankings', 'league_history'
        )
    ),
    ADD CONSTRAINT collector_transport_failures_scope_v2_check CHECK (
        (scope = 'player' AND player_id IS NOT NULL
            AND normalized_tag IS NOT NULL
            AND endpoint IN ('profile', 'battle_log', 'league_history'))
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL
            AND endpoint = 'global_player_rankings')
    );
-- The generated contract columns are recreated with the new endpoint
-- (established drop-and-recreate pattern); the claim-probe index follows.
ALTER TABLE collector_observations
    DROP COLUMN IF EXISTS endpoint_version,
    DROP COLUMN IF EXISTS schema_version;
ALTER TABLE collector_observations
    ADD COLUMN endpoint_version text
        GENERATED ALWAYS AS (
            CASE endpoint
                WHEN 'profile' THEN 'profile-v1'
                WHEN 'battle_log' THEN 'battle-log-v1'
                WHEN 'global_player_rankings' THEN 'global-player-rankings-v1'
                WHEN 'league_history' THEN 'league-history-v1'
            END
        ) STORED,
    ADD COLUMN schema_version text
        GENERATED ALWAYS AS (
            CASE endpoint
                WHEN 'profile' THEN 'profile-schema-v1'
                WHEN 'battle_log' THEN 'battle-log-schema-v1'
                WHEN 'global_player_rankings' THEN 'global-player-rankings-schema-v1'
                WHEN 'league_history' THEN 'league-history-schema-v1'
            END
        ) STORED;
CREATE INDEX IF NOT EXISTS collector_observations_source_contract_v3
    ON collector_observations (endpoint, endpoint_version, schema_version);

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
                || CASE NEW.endpoint
                       WHEN 'battle_log' THEN '/battlelog'
                       WHEN 'league_history' THEN '/leaguehistory'
                       ELSE ''
                   END
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
                WHEN 'league_history' THEN 'league-history-v1'
            END
        );
    END IF;
    RETURN NEW;
END
$$;

-- A single row describes the latest bytes seen for one endpoint identity.  It
-- is intentionally not an observation: unchanged polls update this row only.
-- last_content_fingerprint digests only the fields Clash Lens reads (see
-- response_fields.py); for endpoints without a field list it is the raw hash.
CREATE TABLE IF NOT EXISTS collector_response_state (
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    identity_key text NOT NULL CHECK (length(identity_key) BETWEEN 1 AND 255),
    endpoint text NOT NULL CHECK (
        endpoint IN (
            'profile', 'battle_log', 'global_player_rankings', 'league_history'
        )
    ),
    player_id bigint REFERENCES players (id),
    normalized_tag text,
    last_response_hash text NOT NULL CHECK (last_response_hash ~ '^[0-9a-f]{64}$'),
    last_content_fingerprint text NOT NULL
        CHECK (last_content_fingerprint ~ '^[0-9a-f]{64}$'),
    last_occurrence_key text NOT NULL,
    last_seen_at timestamptz NOT NULL,
    request_count bigint NOT NULL DEFAULT 1 CHECK (request_count > 0),
    last_observation_id bigint REFERENCES collector_observations (id)
        ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, identity_key, endpoint),
    CHECK (
        (
            scope = 'player'
            AND endpoint IN ('profile', 'battle_log', 'league_history')
            AND player_id IS NOT NULL
            AND normalized_tag IS NOT NULL
            AND identity_key = normalized_tag
        )
        OR (
            scope = 'global'
            AND endpoint = 'global_player_rankings'
            AND player_id IS NULL
            AND normalized_tag IS NULL
            AND identity_key = 'global'
        )
    )
);
ALTER TABLE collector_response_state SET (
    fillfactor = 80,
    autovacuum_vacuum_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
DROP INDEX IF EXISTS collector_response_state_seen;

-- Upload state is content-addressed rather than attempt-addressed.  The
-- uploader leases these rows briefly; a late owner cannot finish or fail a
-- row after its token expires.
CREATE TABLE IF NOT EXISTS collector_response_uploads (
    response_hash text PRIMARY KEY CHECK (response_hash ~ '^[0-9a-f]{64}$'),
    spool_key text NOT NULL CHECK (length(spool_key) BETWEEN 1 AND 1024),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    state text NOT NULL DEFAULT 'pending' CHECK (
        state IN ('pending', 'leased', 'complete', 'failed')
    ),
    -- Empty for the first upload of a hash. When retired bytes are observed
    -- again the row is recycled with a generation suffix so the new upload
    -- writes a fresh immutable key instead of the tombstoned location.
    upload_generation text NOT NULL DEFAULT ''
        CHECK (upload_generation ~ '^([0-9a-f]{32})?$'),
    archive_reference text,
    archive_instance_id text REFERENCES archive_instances (instance_id),
    lease_owner text,
    lease_token text,
    lease_expires_at timestamptz,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error_category text,
    last_error_detail text,
    completed_at timestamptz,
    local_deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (state = 'leased'
            AND lease_owner IS NOT NULL
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL)
        OR (state <> 'leased'
            AND lease_owner IS NULL
            AND lease_token IS NULL
            AND lease_expires_at IS NULL)
    ),
    CHECK (
        state <> 'complete'
        OR (
            archive_reference IS NOT NULL
            AND archive_instance_id IS NOT NULL
            AND completed_at IS NOT NULL
        )
    ),
    CHECK (length(COALESCE(last_error_category, '')) <= 128),
    CHECK (length(COALESCE(last_error_detail, '')) <= 1024)
);
CREATE INDEX IF NOT EXISTS collector_response_uploads_claim
    ON collector_response_uploads (state, next_attempt_at, created_at, response_hash)
    WHERE state IN ('pending', 'failed');

-- Transport failures are retained as durable retry evidence without admission
-- ownership columns from the retired collector tree.
ALTER TABLE collector_transport_failures
    ADD COLUMN IF NOT EXISTS occurrence_key text;
CREATE UNIQUE INDEX IF NOT EXISTS collector_transport_failures_occurrence_v4
    ON collector_transport_failures (occurrence_key);

-- The collector writes compact freshness and upload state; the worker retains
-- its existing processing-job path.
GRANT SELECT, INSERT, UPDATE, DELETE ON collector_response_state TO clashlens_collector;
GRANT SELECT ON collector_response_state TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON collector_response_uploads TO clashlens_collector;
GRANT SELECT ON collector_response_uploads TO clashlens_python_worker;

-- Step 4 starts from a fresh database contract.  Remove the old collector
-- job/attempt/result tree and reset membership children; compact work rows
-- below are the only new admission state.
DROP FUNCTION IF EXISTS clashlens_enqueue_interactive(text, text, integer, boolean);
DROP TRIGGER IF EXISTS reset_baseline_evidence_validate_v2
    ON reset_baseline_evidence;
DROP FUNCTION IF EXISTS clashlens_validate_reset_baseline_evidence_v2();
DROP FUNCTION IF EXISTS clashlens_lock_reset_baseline_v2(bigint);
DROP FUNCTION IF EXISTS clashlens_reset_job_lineage_v2(bigint, bigint);
DROP TABLE IF EXISTS collector_boundary_admission CASCADE;
DROP TABLE IF EXISTS collector_interactive_intent_events CASCADE;
DROP TABLE IF EXISTS collector_reset_baseline_sweeps CASCADE;
DROP TABLE IF EXISTS collector_reset_sweep_members CASCADE;
DROP TABLE IF EXISTS collector_endpoint_results CASCADE;
DROP TABLE IF EXISTS collector_attempt_events CASCADE;
DROP TABLE IF EXISTS collector_attempts CASCADE;
DROP TABLE IF EXISTS collector_jobs CASCADE;
DROP TABLE IF EXISTS global_rankings_intents CASCADE;
DROP TABLE IF EXISTS discovery_profile_intents CASCADE;
DROP TABLE IF EXISTS collector_spool_handoffs CASCADE;

ALTER TABLE collector_observations
    DROP COLUMN IF EXISTS collection_job_id,
    DROP COLUMN IF EXISTS attempt_id;
ALTER TABLE collector_transport_failures
    DROP COLUMN IF EXISTS collection_job_id,
    DROP COLUMN IF EXISTS attempt_id;
ALTER TABLE api_refresh_requests
    RENAME COLUMN collector_job_id TO collector_work_id;

ALTER TABLE collector_reset_sweeps
    ADD COLUMN IF NOT EXISTS member_ids bigint[] NOT NULL DEFAULT '{}';
CREATE OR REPLACE FUNCTION clashlens_guard_reset_sweep_inputs()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.membership_captured_at IS NOT NULL
       AND (
           NEW.boundary_at IS DISTINCT FROM OLD.boundary_at
           OR NEW.membership_rule_version IS DISTINCT FROM OLD.membership_rule_version
           OR NEW.membership_captured_at IS DISTINCT FROM OLD.membership_captured_at
           OR NEW.member_ids IS DISTINCT FROM OLD.member_ids
       ) THEN
        RAISE EXCEPTION 'reset sweep membership is immutable after capture';
    END IF;
    RETURN NEW;
END $$;

CREATE TABLE IF NOT EXISTS collector_work (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN (
        'initial_collection', 'live_refresh', 'reset_baseline',
        'discovery_profile', 'global_player_rankings'
    )),
    lane text NOT NULL CHECK (lane IN ('interactive', 'reset', 'ordinary')),
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    player_id bigint REFERENCES players (id),
    normalized_tag text,
    sweep_id bigint REFERENCES collector_reset_sweeps (id) ON DELETE CASCADE,
    due_at timestamptz NOT NULL,
    coalescing_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'waiting_retry', 'complete', 'failed', 'cancelled')
    ),
    profile_status text NOT NULL DEFAULT 'pending' CHECK (
        profile_status IN ('not_applicable', 'pending', 'observed', 'failed')
    ),
    battle_log_status text NOT NULL DEFAULT 'pending' CHECK (
        battle_log_status IN ('not_applicable', 'pending', 'observed', 'failed')
    ),
    -- League history is fetched at initial collection and once after each
    -- season-ending Reset, not on the regular poll or live refresh.
    league_history_status text NOT NULL DEFAULT 'not_applicable' CHECK (
        league_history_status IN
            ('not_applicable', 'pending', 'observed', 'failed')
    ),
    profile_observation_id bigint REFERENCES collector_observations (id)
        ON DELETE SET NULL,
    battle_log_observation_id bigint REFERENCES collector_observations (id)
        ON DELETE SET NULL,
    league_history_observation_id bigint REFERENCES collector_observations (id)
        ON DELETE SET NULL,
    failure_category text,
    failure_detail text,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (status = 'complete' OR completed_at IS NULL),
    CHECK (
        (scope = 'player' AND player_id IS NOT NULL AND normalized_tag IS NOT NULL)
        OR (scope = 'global' AND player_id IS NULL AND normalized_tag IS NULL)
    ),
    CHECK (
        (kind = 'global_player_rankings' AND scope = 'global'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND profile_status IN ('pending', 'observed')
            AND battle_log_status = 'not_applicable'
            AND league_history_status = 'not_applicable')
        OR (kind = 'discovery_profile' AND scope = 'player'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND battle_log_status = 'not_applicable'
            AND league_history_status IN ('pending', 'observed'))
        OR (kind = 'initial_collection' AND scope = 'player'
            AND lane = 'interactive' AND sweep_id IS NULL
            AND league_history_status IN ('pending', 'observed'))
        OR (kind = 'live_refresh' AND scope = 'player'
            AND lane = 'interactive' AND sweep_id IS NULL
            AND league_history_status = 'not_applicable')
        OR (kind = 'reset_baseline' AND scope = 'player'
            AND lane = 'reset' AND sweep_id IS NOT NULL
            AND league_history_status IN
                ('not_applicable', 'pending', 'observed'))
    ),
    CHECK (length(COALESCE(failure_category, '')) <= 128),
    CHECK (length(COALESCE(failure_detail, '')) <= 1024)
);
CREATE UNIQUE INDEX IF NOT EXISTS collector_work_one_active_key
    ON collector_work (coalescing_key)
    WHERE status IN ('pending', 'waiting_retry');
CREATE INDEX IF NOT EXISTS collector_work_claim_order
    ON collector_work (lane, status, due_at, id)
    WHERE status IN ('pending', 'waiting_retry');
CREATE INDEX IF NOT EXISTS collector_work_sweep_order
    ON collector_work (sweep_id, status, id)
    WHERE sweep_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS collector_work_once_per_cycle
    ON collector_work (coalescing_key)
    WHERE kind IN ('discovery_profile', 'global_player_rankings');

-- Reset evidence points at the one compact paired intent.
ALTER TABLE reset_baseline_evidence
    DROP CONSTRAINT IF EXISTS reset_baseline_evidence_complete_v2_check,
    DROP COLUMN IF EXISTS reset_baseline_sweep_id,
    DROP COLUMN IF EXISTS collection_job_id,
    DROP COLUMN IF EXISTS attempt_id,
    DROP COLUMN IF EXISTS legacy_profile_only,
    ADD COLUMN collector_work_id bigint NOT NULL REFERENCES collector_work (id);
DROP INDEX IF EXISTS reset_baseline_evidence_sweep_version_v2;
DROP INDEX IF EXISTS reset_baseline_evidence_sweep_key_v2;
CREATE UNIQUE INDEX reset_baseline_evidence_work_version
    ON reset_baseline_evidence (collector_work_id, version);
CREATE UNIQUE INDEX reset_baseline_evidence_work_key
    ON reset_baseline_evidence (collector_work_id, evidence_key);
ALTER TABLE reset_baseline_evidence
    ADD CONSTRAINT reset_baseline_evidence_complete_v4_check CHECK (
        state <> 'complete'
        OR (
            collector_work_id IS NOT NULL
            AND profile_observation_id IS NOT NULL
            AND battle_log_observation_id IS NOT NULL
            AND profile_processing_outcome_id IS NOT NULL
            AND battle_log_processing_outcome_id IS NOT NULL
            AND profile_valid
            AND battle_log_valid
            AND jsonb_array_length(failure_reasons) = 0
        )
    );

CREATE FUNCTION clashlens_validate_compact_reset_evidence()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    reset_kind text;
    reset_sweep_id bigint;
    reset_player_id bigint;
    reset_profile_observation_id bigint;
    reset_battle_observation_id bigint;
    reset_profile_status text;
    reset_battle_status text;
    reset_boundary timestamptz;
    observed collector_observations%ROWTYPE;
    processed observation_processing_outcomes%ROWTYPE;
BEGIN
    IF NEW.collector_work_id IS NULL THEN
        RAISE EXCEPTION 'reset evidence requires compact Reset work';
    END IF;
    SELECT work.kind, work.sweep_id, work.player_id,
           work.profile_observation_id, work.battle_log_observation_id,
           work.profile_status, work.battle_log_status, sweep.boundary_at
    INTO reset_kind, reset_sweep_id, reset_player_id,
         reset_profile_observation_id, reset_battle_observation_id,
         reset_profile_status, reset_battle_status, reset_boundary
    FROM collector_work AS work
    JOIN collector_reset_sweeps AS sweep ON sweep.id = work.sweep_id
    WHERE work.id = NEW.collector_work_id;
    IF NOT FOUND
       OR reset_kind <> 'reset_baseline'
       OR reset_sweep_id IS DISTINCT FROM NEW.sweep_id
       OR reset_player_id IS DISTINCT FROM NEW.player_id
       OR reset_boundary IS DISTINCT FROM NEW.boundary_at THEN
        RAISE EXCEPTION 'reset evidence identity does not match compact Reset work';
    END IF;
    IF reset_profile_observation_id
           IS DISTINCT FROM NEW.profile_observation_id
       OR reset_battle_observation_id
           IS DISTINCT FROM NEW.battle_log_observation_id THEN
        RAISE EXCEPTION 'reset evidence does not match paired Reset observations';
    END IF;

    IF NEW.profile_observation_id IS NOT NULL THEN
        SELECT * INTO observed FROM collector_observations
        WHERE id = NEW.profile_observation_id;
        IF NOT FOUND OR observed.endpoint <> 'profile'
           OR observed.player_id IS DISTINCT FROM NEW.player_id
           OR observed.response_completed_at < NEW.boundary_at THEN
            RAISE EXCEPTION 'reset profile observation is invalid';
        END IF;
    END IF;
    IF NEW.battle_log_observation_id IS NOT NULL THEN
        SELECT * INTO observed FROM collector_observations
        WHERE id = NEW.battle_log_observation_id;
        IF NOT FOUND OR observed.endpoint <> 'battle_log'
           OR observed.player_id IS DISTINCT FROM NEW.player_id
           OR observed.response_completed_at < NEW.boundary_at THEN
            RAISE EXCEPTION 'reset battle-log observation is invalid';
        END IF;
    END IF;
    IF NEW.state = 'complete' THEN
        IF reset_profile_status <> 'observed'
           OR reset_battle_status <> 'observed' THEN
            RAISE EXCEPTION 'complete reset evidence requires both endpoint results';
        END IF;
        SELECT * INTO processed FROM observation_processing_outcomes
        WHERE id = NEW.profile_processing_outcome_id;
        IF NOT FOUND OR processed.observation_id
               IS DISTINCT FROM NEW.profile_observation_id
           OR processed.endpoint <> 'profile'
           OR processed.outcome <> 'processed' THEN
            RAISE EXCEPTION 'reset profile observation was not processed';
        END IF;
        SELECT * INTO processed FROM observation_processing_outcomes
        WHERE id = NEW.battle_log_processing_outcome_id;
        IF NOT FOUND OR processed.observation_id
               IS DISTINCT FROM NEW.battle_log_observation_id
           OR processed.endpoint <> 'battle_log'
           OR processed.outcome <> 'processed' THEN
            RAISE EXCEPTION 'reset battle-log observation was not processed';
        END IF;
    END IF;
    RETURN NEW;
END
$$;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_validate_compact_reset_evidence() SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_validate_compact_reset_evidence() FROM PUBLIC;
CREATE TRIGGER reset_baseline_evidence_validate_compact
BEFORE INSERT OR UPDATE ON reset_baseline_evidence
FOR EACH ROW
EXECUTE FUNCTION clashlens_validate_compact_reset_evidence();

-- Recreate only the compact Refresh admission function.
CREATE FUNCTION clashlens_enqueue_interactive(
    requested_type text,
    requested_tag text,
    cooldown_seconds integer DEFAULT 30,
    bypass_cooldown boolean DEFAULT false
)
RETURNS TABLE (work_id bigint, outcome text, reused boolean)
LANGUAGE plpgsql
AS $$
DECLARE
    selected_player_id bigint;
    selected_work_id bigint;
    selected_status text;
    selected_completed_at timestamptz;
    selected_outcome text;
    selected_reused boolean;
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
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_tag, 0));
    INSERT INTO players (normalized_tag, active)
    VALUES (requested_tag, false)
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id INTO selected_player_id FROM players WHERE normalized_tag = requested_tag;
    SELECT id, status, completed_at
    INTO selected_work_id, selected_status, selected_completed_at
    FROM collector_work
    WHERE player_id = selected_player_id AND lane = 'interactive'
      AND status IN ('pending', 'waiting_retry')
    ORDER BY id DESC LIMIT 1 FOR UPDATE;
    IF selected_work_id IS NOT NULL THEN
        selected_outcome := 'coalesced';
        selected_reused := true;
    ELSE
        IF NOT bypass_cooldown THEN
            SELECT id, status, completed_at
            INTO selected_work_id, selected_status, selected_completed_at
            FROM collector_work
            WHERE player_id = selected_player_id AND lane = 'interactive'
              AND status = 'complete'
              AND completed_at >= clock_timestamp() - make_interval(secs => cooldown_seconds)
            ORDER BY completed_at DESC, id DESC LIMIT 1;
        END IF;
        IF selected_work_id IS NOT NULL THEN
            selected_outcome := 'cooldown_hit';
            selected_reused := true;
        ELSE
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at,
                coalescing_key, profile_status, battle_log_status,
                league_history_status
            ) VALUES (
                requested_type, 'interactive', 'player', selected_player_id,
                requested_tag, clock_timestamp(), 'interactive:' || requested_tag,
                'pending', 'pending',
                CASE requested_type
                    WHEN 'initial_collection' THEN 'pending'
                    ELSE 'not_applicable'
                END
            ) RETURNING id INTO selected_work_id;
            selected_outcome := 'created';
            selected_reused := false;
        END IF;
    END IF;
    RETURN QUERY SELECT selected_work_id, selected_outcome, selected_reused;
END
$$;
ALTER FUNCTION clashlens_enqueue_interactive(text, text, integer, boolean)
    SECURITY DEFINER;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_enqueue_interactive(text, text, integer, boolean) SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;

CREATE OR REPLACE FUNCTION clashlens_enqueue_discovery_profiles(
    requested_player_ids bigint[]
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    cycle_start timestamptz := date_bin(
        interval '5 minutes', clock_timestamp(),
        timestamptz '2000-01-01 00:00:00+00'
    );
    created_count integer;
BEGIN
    IF requested_player_ids IS NULL
       OR cardinality(requested_player_ids) > 500
       OR EXISTS (
            SELECT 1 FROM unnest(requested_player_ids) AS player_id
            WHERE player_id IS NULL OR player_id <= 0
       ) THEN
        RAISE EXCEPTION 'player IDs must be a bounded array of positive values'
            USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(requested_player_ids) AS requested(player_id)
        LEFT JOIN players AS player ON player.id = requested.player_id
        WHERE player.id IS NULL
    ) THEN
        RAISE EXCEPTION 'player ID does not exist' USING ERRCODE = '22023';
    END IF;
    WITH requested AS (
        SELECT DISTINCT player_id
        FROM unnest(requested_player_ids) AS player_id
    ), inserted AS (
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at,
            coalescing_key, profile_status, battle_log_status,
            league_history_status
        )
        SELECT 'discovery_profile', 'ordinary', 'player', player.id,
               player.normalized_tag, cycle_start,
               'discovery-profile:' || player.id || ':' ||
                   to_char(cycle_start AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
               'pending', 'not_applicable', 'pending'
        FROM requested
        JOIN players AS player ON player.id = requested.player_id
        WHERE NOT (player.active AND player.eligibility_state = 'eligible')
        ON CONFLICT DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO created_count FROM inserted;
    RETURN created_count;
END
$$;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_enqueue_discovery_profiles(bigint[]) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END
$$;

GRANT SELECT, INSERT, UPDATE, DELETE ON collector_work TO clashlens_collector;
GRANT SELECT ON collector_work TO clashlens_python_worker, clashlens_python_api;
GRANT USAGE, SELECT ON SEQUENCE collector_work_id_seq TO clashlens_collector;
REVOKE ALL ON FUNCTION clashlens_enqueue_interactive(text, text, integer, boolean)
    FROM PUBLIC, clashlens_python_worker;
GRANT EXECUTE ON FUNCTION clashlens_enqueue_interactive(text, text, integer, boolean)
    TO clashlens_collector, clashlens_python_api;
ALTER TABLE api_refresh_requests
    DROP CONSTRAINT IF EXISTS api_refresh_requests_collector_work_id_fkey,
    ADD CONSTRAINT api_refresh_requests_collector_work_id_fkey
        FOREIGN KEY (collector_work_id) REFERENCES collector_work (id) ON DELETE CASCADE;
REVOKE ALL ON FUNCTION clashlens_enqueue_discovery_profiles(bigint[])
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_enqueue_discovery_profiles(bigint[])
    TO clashlens_python_worker;
DROP INDEX IF EXISTS collector_response_state_seen;

-- Raw responses retire 56 days after their season ends. The deadline hangs
-- off the fixed 28-day season grid anchored at 1783918800 (a Monday 05:00 UTC
-- boundary), so it depends on the response's season, not its age.
CREATE FUNCTION clashlens_season_retire_after(observed_at timestamptz)
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT to_timestamp(
        1783918800
        + floor((extract(epoch FROM observed_at) - 1783918800) / 2419200)
            * 2419200
        + 7257600
    )
$$;
ALTER FUNCTION clashlens_season_retire_after(timestamptz) SECURITY DEFINER;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_season_retire_after(timestamptz) SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_season_retire_after(timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clashlens_season_retire_after(timestamptz)
    TO clashlens_collector;

ALTER TABLE archive_catalogue
    ADD COLUMN IF NOT EXISTS retire_after timestamptz;
UPDATE archive_catalogue
SET retire_after = clashlens_season_retire_after(first_verified_at)
WHERE retire_after IS NULL;
ALTER TABLE archive_catalogue
    ALTER COLUMN retire_after SET NOT NULL,
    ALTER COLUMN retire_after
        SET DEFAULT clashlens_season_retire_after(clock_timestamp());
DROP INDEX IF EXISTS archive_catalogue_retention;
CREATE INDEX archive_catalogue_retention
    ON archive_catalogue (availability, retire_after);

-- The retention trigger keeps its verified-location fence. The maintained
-- column is now retire_after: a later season's sighting extends the deadline,
-- an earlier one never shortens it. last_seen_before served the retired
-- six-month model and is dropped.
CREATE OR REPLACE FUNCTION clashlens_observed_archive_retention()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.archive_catalogue_hash IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM archive_catalogue
        WHERE response_hash = NEW.response_hash
          AND archive_reference = NEW.archive_reference
          AND availability = 'verified'
    ) THEN
        RAISE EXCEPTION 'observation requires a currently verified archive location';
    END IF;
    UPDATE archive_catalogue
    SET retire_after = clashlens_season_retire_after(NEW.response_completed_at)
    WHERE response_hash = NEW.response_hash
      AND archive_reference = NEW.archive_reference
      AND retire_after < clashlens_season_retire_after(NEW.response_completed_at);
    RETURN NEW;
END $$;
ALTER TABLE archive_catalogue DROP COLUMN IF EXISTS last_seen_before;

-- One durable row per player per past season, refreshed by the leaguehistory
-- endpoint at initial collection and after each season-ending Reset. The
-- restrictive evidence references keep pruning honest: a referenced
-- observation or parsed payload cannot disappear underneath a season row.
CREATE TABLE IF NOT EXISTS player_league_history_entries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id bigint NOT NULL REFERENCES players (id),
    league_season_id text NOT NULL CHECK (league_season_id ~ '^[0-9]+$'),
    observed_at timestamptz NOT NULL,
    observation_id bigint NOT NULL REFERENCES collector_observations (id),
    parsed_payload_id bigint NOT NULL REFERENCES parsed_source_payloads (id),
    league_trophies integer,
    league_tier_id integer,
    placement integer,
    attack_wins integer,
    attack_losses integer,
    attack_stars integer,
    defense_wins integer,
    defense_losses integer,
    defense_stars integer,
    max_battles integer,
    source_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (player_id, league_season_id)
);
GRANT SELECT, INSERT, UPDATE ON player_league_history_entries
    TO clashlens_python_worker;
GRANT SELECT ON player_league_history_entries TO clashlens_python_api;
GRANT USAGE, SELECT ON SEQUENCE player_league_history_entries_id_seq
    TO clashlens_python_worker;

-- League-history jobs are fenced at claim compatibility 6 so a worker image
-- without the parser can never claim them.
CREATE OR REPLACE FUNCTION clashlens_set_python_claim_compatibility_v3()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.claim_compatibility_version := CASE
        WHEN NEW.processing_version = 'clashlens-domain-processing-v1'
         AND NEW.domain_rule_version = 'clashlens-domain-rules-v1'
         AND (
            (NEW.work_type IN ('process_observation', 'replay_observation')
                AND (
                    NEW.parser_version IN ('supercell-source-parser-v1','supercell-source-parser-v2')
                    OR NEW.parser_version = 'supercell-profile-parser-v3'
                    OR NEW.parser_version = 'supercell-league-history-parser-v1'
                )
                AND EXISTS (
                    SELECT 1 FROM collector_observations AS observation
                    WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
                      AND (
                        (observation.endpoint = 'profile'
                            AND observation.endpoint_version = 'profile-v1'
                            AND observation.schema_version = 'profile-schema-v1')
                        OR (observation.endpoint = 'league_history'
                            AND observation.endpoint_version = 'league-history-v1'
                            AND observation.schema_version = 'league-history-schema-v1'
                            AND NEW.parser_version = 'supercell-league-history-parser-v1')
                        OR (NEW.parser_version NOT IN ('supercell-profile-parser-v3', 'supercell-league-history-parser-v1') AND (
                            (observation.endpoint = 'battle_log'
                                AND observation.endpoint_version = 'battle-log-v1'
                                AND observation.schema_version = 'battle-log-schema-v1')
                            OR (observation.endpoint = 'global_player_rankings'
                                AND observation.endpoint_version = 'global-player-rankings-v1'
                                AND observation.schema_version = 'global-player-rankings-schema-v1')
                        ))
                      )
                ))
            OR (NEW.work_type IN ('reconcile_ranked_day','build_snapshot')
                AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_analytics'
                AND NEW.analytics_rule_version = 'legend-analytics-v1'
                AND NEW.input_json ? 'snapshot_id'
                AND NEW.input_json ? 'snapshot_version'
                AND NEW.input_json ? 'snapshot_input_hash'
                AND NEW.input_json ? 'source_ranked_day_version_id'
                AND (NEW.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$'
                AND length(NEW.input_json->>'snapshot_input_hash') > 0)
            OR (NEW.work_type IN ('build_army_analytics','redecode_army')
                AND NEW.analytics_rule_version = 'army-analytics-v2')
         )
        THEN CASE
            WHEN NEW.parser_version = 'supercell-league-history-parser-v1' THEN 6
            WHEN NEW.parser_version = 'supercell-profile-parser-v3' THEN 5
            WHEN NEW.work_type IN ('build_army_analytics','redecode_army') THEN 3
            WHEN NEW.parser_version = 'supercell-source-parser-v2' THEN 2
            ELSE 1
        END
        ELSE 0
    END;
    RETURN NEW;
END $$;

DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending','waiting_retry','waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4,5,6)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_waiting_dependency_claim_v3;
CREATE INDEX python_processing_jobs_waiting_dependency_claim_v3
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5,6);
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased'
      AND claim_compatibility_version IN (1,2,3,4,5,6)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending','waiting_retry')
      AND claim_compatibility_version IN (1,2,3,4,5,6)
      AND attempt_count < max_attempts
      AND priority NOT IN (100,50,25,10);

INSERT INTO clash_lens_schema_migrations(version) VALUES (26)
ON CONFLICT (version) DO NOTHING;
COMMIT;
