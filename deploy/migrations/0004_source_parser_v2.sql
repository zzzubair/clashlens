-- Clash Lens deployment migration 0004.
-- Make the live official source shape parser v2 while retaining parser v1
-- for deterministic replay of the earlier synthetic adapter contract.

BEGIN;

-- Fence parser-v2 jobs from the previous Python image. That image already
-- knew the v2 label but still applied the v1 row interpretation; it claims
-- only compatibility generation 1. The new image claims both generations so
-- existing v1 work remains drainable and replayable.
CREATE OR REPLACE FUNCTION clashlens_fence_python_parser_v2_claim_v4()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.parser_version = 'supercell-source-parser-v2'
       AND NEW.claim_compatibility_version = 1 THEN
        NEW.claim_compatibility_version := 2;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS zz_python_processing_jobs_parser_v2_claim_fence_v4
    ON python_processing_jobs;
-- PostgreSQL runs same-event triggers by name. The zz prefix makes this fence
-- run after migration 0003's authoritative compatibility classifier.
CREATE TRIGGER zz_python_processing_jobs_parser_v2_claim_fence_v4
BEFORE INSERT OR UPDATE OF
    observation_id, replay_observation_id, work_type, parser_version,
    processing_version, domain_rule_version, analytics_rule_version, input_json,
    claim_compatibility_version
ON python_processing_jobs
FOR EACH ROW
EXECUTE FUNCTION clashlens_fence_python_parser_v2_claim_v4();

REVOKE ALL ON FUNCTION clashlens_fence_python_parser_v2_claim_v4()
    FROM PUBLIC, clashlens_collector, clashlens_python_worker,
         clashlens_python_api;

-- Reclassify only work carrying the v2 label. V1 and unsupported backlog is
-- untouched; the trigger applies the same rule to future inserts.
UPDATE python_processing_jobs
SET claim_compatibility_version = claim_compatibility_version
WHERE parser_version = 'supercell-source-parser-v2';

-- Both worker generations can use these broader probes. Generation-1 claims
-- imply this predicate, while the new worker also sees generation 2.
DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending', 'waiting_retry')
      AND claim_compatibility_version IN (1, 2)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased'
      AND claim_compatibility_version IN (1, 2)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending', 'waiting_retry')
      AND claim_compatibility_version IN (1, 2)
      AND attempt_count < max_attempts
      AND priority NOT IN (100);

-- New collection and derived work use the installed v2 parser by default.
-- Existing jobs and derived rows keep their recorded parser version.
ALTER TABLE python_processing_jobs
    ALTER COLUMN parser_version SET DEFAULT 'supercell-source-parser-v2';
ALTER TABLE reset_baseline_evidence
    ALTER COLUMN parser_version SET DEFAULT 'supercell-source-parser-v2';
ALTER TABLE ranked_day_versions
    ALTER COLUMN parser_version SET DEFAULT 'supercell-source-parser-v2';

-- Replay remains version-selectable so an archived observation can be
-- interpreted with either installed parser. The host wrapper defaults to v2
-- and accepts only these two installed versions; old v1 jobs remain intact.
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
           'supercell-source-parser-v2'
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

-- The collector-owned contract remains version two. This forward migration
-- changes only the installed source parser and replay seam.
INSERT INTO clash_lens_schema_migrations (version)
VALUES (4)
ON CONFLICT (version) DO NOTHING;

COMMIT;
