-- Keep migration-created army redecode work below live observation processing.
-- Historical jobs remain claim-compatible; only their scheduling class changes.
BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE current_version integer;
BEGIN
    SELECT version INTO current_version
    FROM clash_lens_contract
    WHERE singleton;
    IF current_version <> 5 THEN
        RAISE EXCEPTION 'army backfill migration requires contract version 5 (got %)', current_version;
    END IF;
END $$;

-- Migration 0008 created these jobs with the normal priority. Only its exact
-- key shape is reclassified; operator-created redecode jobs stay independent.
UPDATE python_processing_jobs
SET priority = 25
WHERE work_type = 'redecode_army'
  AND deduplication_key ~ '^redecode_army:army-decoder-v2:unit-catalog-v1:[1-9][0-9]*:[1-9][0-9]*$'
  AND status IN ('pending', 'waiting_retry', 'waiting_dependency', 'leased');

-- Keep the bounded claim probes indexed for both the backfill and normal
-- classes. The catch-all remains available for future/operator priorities.
DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending', 'waiting_retry', 'waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2
    ON python_processing_jobs (due_at, created_at, id, priority)
    WHERE status IN ('pending', 'waiting_retry', 'waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4)
      AND attempt_count < max_attempts
      AND priority NOT IN (25, 100);

INSERT INTO clash_lens_schema_migrations(version) VALUES (13)
ON CONFLICT (version) DO NOTHING;
COMMIT;
