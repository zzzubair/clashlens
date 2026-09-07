-- Delete only completed, redundant operational trees. Durable domain references
-- deliberately remain restrictive and are an additional safety barrier.
BEGIN;
DO $$
DECLARE
    fk record;
    operational text[] := ARRAY[
        'collector_attempts', 'collector_attempt_events', 'collector_endpoint_results',
        'collector_observations', 'collector_transport_failures',
        'collector_interactive_intent_events', 'api_refresh_requests',
        'python_processing_jobs', 'python_processing_attempts',
        'python_processing_job_events', 'python_replay_requests',
        'processed_observation_versions', 'known_player_discoveries',
        'player_discovery_events', 'observation_processing_outcomes',
        'player_profile_effects', 'battle_log_observations',
        'battle_log_observation_rows', 'official_top200_attempts',
        'official_top200_versions', 'official_top200_attempt_entries',
        'official_top200_version_entries', 'official_top200_entries'
    ];
BEGIN
    FOR fk IN
        SELECT c.conname, child.relname AS child, parent.relname AS parent,
               pg_get_constraintdef(c.oid) AS definition
        FROM pg_constraint AS c
        JOIN pg_class AS child ON child.oid = c.conrelid
        JOIN pg_class AS parent ON parent.oid = c.confrelid
        WHERE c.contype = 'f' AND c.connamespace = current_schema()::regnamespace
          AND child.relname = ANY(operational)
          AND parent.relname = ANY(ARRAY[
              'collector_jobs', 'collector_attempts', 'collector_observations',
              'python_processing_jobs', 'battle_log_observations',
              'official_top200_attempts', 'official_top200_versions'
          ])
          AND c.confdeltype <> 'c'
    LOOP
        EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', fk.child, fk.conname);
        EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s ON DELETE CASCADE',
            fk.child, fk.conname, fk.definition);
    END LOOP;
END $$;

-- A completed attempt can expire independently of a retained semantic result.
ALTER TABLE observation_processing_outcomes
    DROP CONSTRAINT IF EXISTS observation_processing_outcomes_attempt_id_fkey,
    ADD CONSTRAINT observation_processing_outcomes_attempt_id_fkey
        FOREIGN KEY (attempt_id) REFERENCES python_processing_attempts(id) ON DELETE SET NULL;
ALTER TABLE player_profile_effects
    DROP CONSTRAINT IF EXISTS player_profile_effects_attempt_id_fkey,
    ADD CONSTRAINT player_profile_effects_attempt_id_fkey
        FOREIGN KEY (attempt_id) REFERENCES python_processing_attempts(id) ON DELETE SET NULL;
ALTER TABLE collector_jobs
    DROP CONSTRAINT IF EXISTS collector_jobs_result_attempt_fk,
    ADD CONSTRAINT collector_jobs_result_attempt_fk
        FOREIGN KEY (result_attempt_id) REFERENCES collector_attempts(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS battle_log_observations_player_time
    ON battle_log_observations (player_id, parser_version, observed_at, id);
CREATE INDEX IF NOT EXISTS collector_jobs_retention
    ON collector_jobs (updated_at, id) WHERE status = 'complete';
CREATE INDEX IF NOT EXISTS collector_observations_collection_job
    ON collector_observations (collection_job_id, id);

ALTER TABLE battle_payload_rows
    DROP CONSTRAINT IF EXISTS battle_payload_rows_parsed_payload_id_fkey,
    ADD CONSTRAINT battle_payload_rows_parsed_payload_id_fkey
        FOREIGN KEY (parsed_payload_id) REFERENCES parsed_source_payloads(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS collector_observations_response_hash
    ON collector_observations (response_hash);
CREATE INDEX IF NOT EXISTS python_processing_jobs_retention
    ON python_processing_jobs (updated_at, id) WHERE status = 'complete';

INSERT INTO clash_lens_schema_migrations(version) VALUES (17)
ON CONFLICT (version) DO NOTHING;
COMMIT;
