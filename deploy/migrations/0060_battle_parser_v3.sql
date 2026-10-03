-- Clash Lens deployment migration 0060.
-- Battle parser v3: the same live battle rows as source-parser-v2, read with
-- trophy allocation v2 (two stars at 55% gives 17, not 18). Its jobs get
-- claim compatibility 7, so a worker image without the corrected table can
-- never claim them. Only battle_log observations qualify. Saved v1/v2
-- results, queued jobs and trophy numbers are not touched.
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
                    OR NEW.parser_version = 'supercell-league-history-parser-v1'
                    OR NEW.parser_version = 'supercell-battle-parser-v3'
                )
                AND EXISTS (
                    SELECT 1 FROM collector_observations AS observation
                    WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
                      AND (
                        (observation.endpoint = 'profile'
                            AND observation.endpoint_version = 'profile-v1'
                            AND observation.schema_version = 'profile-schema-v1'
                            AND NEW.parser_version <> 'supercell-battle-parser-v3')
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
                                AND observation.schema_version = 'global-player-rankings-schema-v1'
                                AND NEW.parser_version <> 'supercell-battle-parser-v3')
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
            WHEN NEW.parser_version = 'supercell-battle-parser-v3' THEN 7
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

-- Reclassifies only v3 jobs, which exist only if a new collector wrote them
-- before this migration ran.
UPDATE python_processing_jobs
SET claim_compatibility_version = claim_compatibility_version
WHERE parser_version = 'supercell-battle-parser-v3';

DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending','waiting_retry','waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_waiting_dependency_claim_v3;
CREATE INDEX python_processing_jobs_waiting_dependency_claim_v3
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5,6,7);
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased'
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending','waiting_retry')
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts
      AND priority NOT IN (25,100);
DROP INDEX IF EXISTS python_processing_jobs_waiting_dependency_unknown_priority_v3;
CREATE INDEX python_processing_jobs_waiting_dependency_unknown_priority_v3
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND priority NOT IN (25,100);

INSERT INTO clash_lens_schema_migrations(version) VALUES (60)
ON CONFLICT (version) DO NOTHING;
COMMIT;
