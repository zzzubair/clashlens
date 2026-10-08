-- Clash Lens deployment migration 0088.
-- 1. A replay request can name supercell-battle-parser-v3, for battle logs
--    only, so a failed battle-log job saved under that parser is replayed
--    under the same rules instead of an older parser's.
-- 2. An operator can accept a failed processing job that cannot be repaired
--    (`./ops failed-items --accept-job-id`). The job, its attempts and its
--    saved response stay; one row records who accepted it, when and why, and
--    the collector's failed-job count stops counting it. A few rows in all,
--    under 1 KB each. Failed jobs are never cleaned up, so the reference holds.
BEGIN;

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
           'supercell-profile-parser-v3',
           'supercell-battle-parser-v3'
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
       OR (requested_parser_version = 'supercell-battle-parser-v3'
           AND observation_endpoint <> 'battle_log')
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

CREATE TABLE IF NOT EXISTS python_failed_job_acceptances (
    job_id bigint PRIMARY KEY REFERENCES python_processing_jobs (id),
    operator_identity text NOT NULL
        CHECK (operator_identity ~ '^[A-Za-z0-9._:@-]{1,128}$'),
    reason text NOT NULL
        CHECK (length(reason) BETWEEN 8 AND 500 AND reason !~ '[[:cntrl:]]'),
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The collector's measurements leave accepted jobs out of the failed count.
GRANT SELECT ON TABLE python_failed_job_acceptances TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (88)
ON CONFLICT (version) DO NOTHING;
COMMIT;
