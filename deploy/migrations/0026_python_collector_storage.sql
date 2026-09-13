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

-- A single row describes the latest bytes seen for one endpoint identity.  It
-- is intentionally not an observation: unchanged polls update this row only.
CREATE TABLE IF NOT EXISTS collector_response_state (
    scope text NOT NULL CHECK (scope IN ('player', 'global')),
    identity_key text NOT NULL CHECK (length(identity_key) BETWEEN 1 AND 255),
    endpoint text NOT NULL CHECK (
        endpoint IN ('profile', 'battle_log', 'global_player_rankings')
    ),
    player_id bigint REFERENCES players (id),
    normalized_tag text,
    last_response_hash text NOT NULL CHECK (last_response_hash ~ '^[0-9a-f]{64}$'),
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
            AND endpoint IN ('profile', 'battle_log')
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
    profile_observation_id bigint REFERENCES collector_observations (id)
        ON DELETE SET NULL,
    battle_log_observation_id bigint REFERENCES collector_observations (id)
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
            AND battle_log_status = 'not_applicable')
        OR (kind = 'discovery_profile' AND scope = 'player'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND battle_log_status = 'not_applicable')
        OR (kind IN ('initial_collection', 'live_refresh')
            AND scope = 'player' AND lane = 'interactive' AND sweep_id IS NULL)
        OR (kind = 'reset_baseline' AND scope = 'player'
            AND lane = 'reset' AND sweep_id IS NOT NULL)
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
                coalescing_key, profile_status, battle_log_status
            ) VALUES (
                requested_type, 'interactive', 'player', selected_player_id,
                requested_tag, clock_timestamp(), 'interactive:' || requested_tag,
                'pending', 'pending'
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
            coalescing_key, profile_status, battle_log_status
        )
        SELECT 'discovery_profile', 'ordinary', 'player', player.id,
               player.normalized_tag, cycle_start,
               'discovery-profile:' || player.id || ':' ||
                   to_char(cycle_start AT TIME ZONE 'UTC',
                           'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
               'pending', 'not_applicable'
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

INSERT INTO clash_lens_schema_migrations(version) VALUES (26)
ON CONFLICT (version) DO NOTHING;
COMMIT;
