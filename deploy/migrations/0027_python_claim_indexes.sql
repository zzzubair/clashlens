-- Keep every Python claim probe bounded after dependency deferrals and the
-- retired priority-10/50 queue classes were removed from enqueue paths.
BEGIN;

DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending','waiting_retry')
      AND claim_compatibility_version IN (1,2,3,4,5,6)
      AND attempt_count < max_attempts
      AND priority NOT IN (25,100);

DROP INDEX IF EXISTS python_processing_jobs_waiting_dependency_unknown_priority_v3;
CREATE INDEX python_processing_jobs_waiting_dependency_unknown_priority_v3
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status = 'waiting_dependency'
      AND claim_compatibility_version IN (1,2,3,4,5,6)
      AND priority NOT IN (25,100);

INSERT INTO clash_lens_schema_migrations(version) VALUES (27)
ON CONFLICT (version) DO NOTHING;
COMMIT;
