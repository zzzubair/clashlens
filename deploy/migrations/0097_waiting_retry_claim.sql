-- Clash Lens deployment migration 0097.
-- Jobs that already failed get their own claim probe, oldest first. A failed
-- job is due again from its retry time, so among jobs of its priority a claim
-- looked at the 32 that became due first and missed it while newer work
-- waited ahead of it. At a Reset that newer live work itself waited behind the
-- Reset backlog, so a retried live job waited for both, past the 20 minutes
-- that let live work go ahead of Reset work (PR #369). A retried job returned
-- to pending after its lease ran out keeps its recent due time, so the probe
-- covers pending jobs that failed as well as jobs waiting to retry.
--
-- The index below holds only jobs that failed at least once and have not
-- finished, which leave it when they finish or run out of attempts, so it
-- stays small. Without it the probe would read every due job of its priority
-- to find them.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again. The build reads the jobs table once,
-- like one of 0093's three builds (0.3 to 0.9 seconds on a cached 1 GB copy).
DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_waiting_retry_claim;

CREATE INDEX CONCURRENTLY python_processing_jobs_waiting_retry_claim
    ON python_processing_jobs (priority, created_at, id)
    WHERE status IN ('pending','waiting_retry')
      AND attempt_count > 0
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts;

INSERT INTO clash_lens_schema_migrations(version) VALUES (97)
ON CONFLICT (version) DO NOTHING;
