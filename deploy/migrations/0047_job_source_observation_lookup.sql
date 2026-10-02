-- Raw-response cleanup finds each archive location's processing jobs through
-- COALESCE(observation_id, replay_observation_id). This lookup turns that into
-- one index probe per location instead of a search of the whole job history.
-- CONCURRENTLY keeps the worker writing jobs while it builds, so this file has
-- no BEGIN: ./ops runs it with psql one statement at a time. A failed
-- concurrent build leaves an unusable index behind, so a rerun drops it first.
DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_source_observation;
CREATE INDEX CONCURRENTLY python_processing_jobs_source_observation
    ON python_processing_jobs ((COALESCE(observation_id, replay_observation_id)));
INSERT INTO clash_lens_schema_migrations(version) VALUES (47)
ON CONFLICT (version) DO NOTHING;
