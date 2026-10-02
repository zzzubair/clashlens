-- Raw-response cleanup finds each archive location's processing jobs through
-- COALESCE(observation_id, replay_observation_id). This lookup turns that into
-- one index probe per location instead of a search of the whole job history.
BEGIN;
DROP INDEX IF EXISTS python_processing_jobs_source_observation;
CREATE INDEX python_processing_jobs_source_observation
    ON python_processing_jobs ((COALESCE(observation_id, replay_observation_id)));
INSERT INTO clash_lens_schema_migrations(version) VALUES (47)
ON CONFLICT (version) DO NOTHING;
COMMIT;
