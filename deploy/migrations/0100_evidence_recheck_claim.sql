-- Clash Lens deployment migration 0100.
-- A day recheck for new evidence goes ahead of the twice-daily recheck pass
-- in the day recheck lane (priority 26); the pass fills what evidence leaves.
--
-- From 02:00 UTC on 11 October 2026 the pass queued about 25,000 rechecks in
-- the lane, due over 4 hours, about 105 a minute beside 50 to 90 for new
-- evidence, while the lane finished 140 to 230 a minute. The claim looked
-- only at the 32 rechecks that came due first, so evidence waited behind
-- every pass recheck due before it: 55 minutes to 2 hours on average from
-- 11:30 to 16:50 UTC, against 1 to 15 minutes on 10 October, when the pass
-- ran in the general background lane.
--
-- The index below holds only rechecks for evidence that wait to run, which
-- leave it when they start, so it stays small: about 90 a minute of the
-- longest pause. The claim probes it beside the lane's usual probe, so a
-- recheck for evidence is found however many pass rechecks came due before
-- it. Its condition repeats the claim's own, as the lane index's does, so
-- the planner picks it over reading that index past the pass.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again. The build reads the jobs table once,
-- like one of 0093's three builds (0.3 to 0.9 seconds on a cached 1 GB copy).
DROP INDEX CONCURRENTLY IF EXISTS python_processing_jobs_evidence_recheck_claim;

CREATE INDEX CONCURRENTLY python_processing_jobs_evidence_recheck_claim
    ON python_processing_jobs (due_at, created_at, id)
    WHERE status IN ('pending','waiting_retry')
      AND priority = 26
      AND (input_json ->> 'trigger') IS DISTINCT FROM 'recheck'
      AND claim_compatibility_version IN (1,2,3,4,5,6,7)
      AND attempt_count < max_attempts;

INSERT INTO clash_lens_schema_migrations(version) VALUES (100)
ON CONFLICT (version) DO NOTHING;
