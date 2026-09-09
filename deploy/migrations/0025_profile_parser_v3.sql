-- Install the profile-only parser v3 claim and replay contract without
-- relabeling prior parser outcomes or allowing older workers to claim v3 work.
BEGIN;

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
                )
                AND EXISTS (
                    SELECT 1 FROM collector_observations AS observation
                    WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
                      AND (
                        (observation.endpoint = 'profile'
                            AND observation.endpoint_version = 'profile-v1'
                            AND observation.schema_version = 'profile-schema-v1')
                        OR (NEW.parser_version <> 'supercell-profile-parser-v3' AND (
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
            WHEN NEW.parser_version = 'supercell-profile-parser-v3' THEN 5
            WHEN NEW.work_type IN ('build_army_analytics','redecode_army') THEN 3
            WHEN NEW.parser_version = 'supercell-source-parser-v2' THEN 2
            ELSE 1
        END
        ELSE 0
    END;
    RETURN NEW;
END $$;

UPDATE python_processing_jobs
SET claim_compatibility_version = claim_compatibility_version
WHERE parser_version = 'supercell-profile-parser-v3';

DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending','waiting_retry','waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4,5)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_waiting_dependency_claim_v3;
CREATE INDEX python_processing_jobs_waiting_dependency_claim_v3
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5);
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased'
      AND claim_compatibility_version IN (1,2,3,4,5)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending','waiting_retry')
      AND claim_compatibility_version IN (1,2,3,4,5)
      AND attempt_count < max_attempts
      AND priority NOT IN (100,50,25,10);

CREATE OR REPLACE FUNCTION clashlens_request_python_replay_v2(
    requested_observation_id bigint,
    requested_operator_identity text,
    requested_reason text,
    requested_parser_version text,
    requested_processing_version text,
    requested_domain_rule_version text,
    requested_analytics_rule_version text
)
RETURNS TABLE (request_id bigint, job_id bigint, request_status text)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    observation_scope text;
    observation_endpoint text;
    observation_adapter text;
    existing_request record;
    created_request_id bigint;
    created_job_id bigint;
    created_dedup_key text;
BEGIN
    IF session_user <> 'clashlens_replay_request' THEN
        RAISE EXCEPTION 'replay request role required' USING ERRCODE = '42501';
    END IF;
    IF requested_operator_identity !~ '^[A-Za-z0-9._:@-]{1,128}$'
       OR length(requested_reason) NOT BETWEEN 1 AND 1024
       OR requested_reason ~ '[\r\n]'
       OR requested_parser_version NOT IN (
           'supercell-source-parser-v1',
           'supercell-source-parser-v2',
           'supercell-profile-parser-v3'
       )
       OR requested_processing_version <> 'clashlens-domain-processing-v1'
       OR requested_domain_rule_version <> 'clashlens-domain-rules-v1'
       OR requested_analytics_rule_version <> 'legend-analytics-v1'
    THEN
        RAISE EXCEPTION 'invalid replay request fields' USING ERRCODE = '22023';
    END IF;

    SELECT scope, endpoint, source_adapter_version
    INTO observation_scope, observation_endpoint, observation_adapter
    FROM collector_observations
    WHERE id = requested_observation_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'collector observation not found' USING ERRCODE = 'P0002';
    END IF;
    IF observation_scope <> 'player'
       OR observation_endpoint NOT IN ('profile', 'battle_log')
       OR observation_adapter NOT IN ('player-profile-v1', 'battle-log-v1')
       OR (requested_parser_version = 'supercell-profile-parser-v3'
           AND observation_endpoint <> 'profile')
    THEN
        RAISE EXCEPTION 'collector observation is not replayable' USING ERRCODE = '22023';
    END IF;

    SELECT request_row.id, request_row.job_id, request_row.status,
           request_row.operator_identity, request_row.reason
    INTO existing_request
    FROM python_replay_requests AS request_row
    WHERE request_row.observation_id = requested_observation_id
      AND request_row.target_parser_version = requested_parser_version
      AND request_row.target_domain_rule_version = requested_domain_rule_version
    FOR UPDATE;

    IF FOUND THEN
        IF existing_request.operator_identity <> requested_operator_identity
           OR existing_request.reason <> requested_reason THEN
            RAISE EXCEPTION 'replay request exists with different audit fields'
                USING ERRCODE = '23505';
        END IF;
        IF existing_request.job_id IS NULL THEN
            created_dedup_key := 'replay-observation:'
                || requested_observation_id::text
                || ':' || requested_parser_version
                || ':' || requested_domain_rule_version;
            INSERT INTO python_processing_jobs (
                replay_observation_id, work_type, deduplication_key, input_json,
                parser_version, processing_version, domain_rule_version,
                analytics_rule_version
            ) VALUES (
                requested_observation_id, 'replay_observation', created_dedup_key,
                jsonb_build_object('replay_request_id', existing_request.id),
                requested_parser_version, requested_processing_version,
                requested_domain_rule_version, requested_analytics_rule_version
            )
            RETURNING id INTO created_job_id;
            UPDATE python_replay_requests
            SET job_id = created_job_id, status = 'enqueued'
            WHERE id = existing_request.id;
            RETURN QUERY SELECT
                existing_request.id, created_job_id, 'enqueued'::text;
            RETURN;
        END IF;
        RETURN QUERY SELECT
            existing_request.id, existing_request.job_id, existing_request.status;
        RETURN;
    END IF;

    INSERT INTO python_replay_requests (
        observation_id, operator_identity, reason,
        target_parser_version, target_domain_rule_version
    ) VALUES (
        requested_observation_id, requested_operator_identity, requested_reason,
        requested_parser_version, requested_domain_rule_version
    )
    RETURNING id INTO created_request_id;

    created_dedup_key := 'replay-observation:'
        || requested_observation_id::text
        || ':' || requested_parser_version
        || ':' || requested_domain_rule_version;
    INSERT INTO python_processing_jobs (
        replay_observation_id, work_type, deduplication_key, input_json,
        parser_version, processing_version, domain_rule_version,
        analytics_rule_version
    ) VALUES (
        requested_observation_id, 'replay_observation', created_dedup_key,
        jsonb_build_object('replay_request_id', created_request_id),
        requested_parser_version, requested_processing_version,
        requested_domain_rule_version, requested_analytics_rule_version
    )
    RETURNING id INTO created_job_id;

    UPDATE python_replay_requests
    SET job_id = created_job_id, status = 'enqueued'
    WHERE id = created_request_id;

    RETURN QUERY SELECT
        created_request_id, created_job_id, 'enqueued'::text;
END
$$;

DO $$
DECLARE
    replay_schema_name text := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text) SET search_path TO pg_catalog, %I',
        replay_schema_name, replay_schema_name
    );
END
$$;

REVOKE ALL ON FUNCTION clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text)
    FROM PUBLIC, clashlens_collector, clashlens_python_worker,
         clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text)
    TO clashlens_replay_request;

INSERT INTO clash_lens_schema_migrations(version) VALUES (25)
ON CONFLICT (version) DO NOTHING;
COMMIT;
