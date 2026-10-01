-- The upload claim takes the earliest due row in (next_attempt_at, created_at,
-- response_hash) order. The old claim index led with state, so with two
-- claimable states every claim read and sorted all due rows: about 200 MB of
-- table pages per claim during a backlog. Leading with the claim order lets a
-- claim stop at the first due row it can lock.
--
-- Every upload rewrites its row several times, and the old row versions stay
-- at the front of the claim order until vacuum removes them. The default 20%
-- dead-row trigger let about 95,000 build up; clean this table at 1%.
BEGIN;

ALTER TABLE collector_response_uploads SET (autovacuum_vacuum_scale_factor = 0.01);

DROP INDEX IF EXISTS collector_response_uploads_claim;
CREATE INDEX collector_response_uploads_claim_order
    ON collector_response_uploads (next_attempt_at, created_at, response_hash)
    WHERE state IN ('pending', 'failed');

INSERT INTO clash_lens_schema_migrations(version) VALUES (41)
ON CONFLICT (version) DO NOTHING;
COMMIT;
