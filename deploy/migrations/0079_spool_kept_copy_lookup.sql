-- Clash Lens deployment migration 0079.
-- Spool cleanup lists every upload whose spool copy is still kept. With no
-- index for that, each lookup read the whole upload table (2.64 million rows,
-- 1,037 MB in production on 7 October 2026) to find a few thousand. This
-- index holds only kept copies, so it grows with the waiting backlog, not the
-- table, and the lookup reads it instead.
--
-- The table's statistics were refreshed only after about 264,000 changed
-- rows, 10% of the table. The refresh taken just before the 7 October Reset
-- said almost no copy was kept, and the planner chose a 60-second plan. Now
-- they refresh after every 25,000 changed rows, whatever the table's size.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again.
ALTER TABLE collector_response_uploads SET (
    autovacuum_analyze_scale_factor = 0,
    autovacuum_analyze_threshold = 25000
);

DROP INDEX CONCURRENTLY IF EXISTS collector_response_uploads_kept_copies;

CREATE INDEX CONCURRENTLY collector_response_uploads_kept_copies
    ON collector_response_uploads (response_hash)
    WHERE local_deleted_at IS NULL;

INSERT INTO clash_lens_schema_migrations(version) VALUES (79)
ON CONFLICT (version) DO NOTHING;
