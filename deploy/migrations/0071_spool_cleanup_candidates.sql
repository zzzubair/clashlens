-- Clash Lens deployment migration 0071.
-- Spool cleanup looks up the oldest archived responses whose spool copy is
-- still kept. Without this index each lookup read and sorted the whole upload
-- table (1.67 million rows, 652 MB, in production on October 4), a third of
-- all database reads. The index holds only kept, archived rows, in the
-- lookup's order, so a lookup reads its first rows and stops.
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again.
DROP INDEX CONCURRENTLY IF EXISTS collector_response_uploads_cleanup_order;

CREATE INDEX CONCURRENTLY collector_response_uploads_cleanup_order
    ON collector_response_uploads (latest_sighting_at, completed_at, response_hash)
    WHERE state = 'complete' AND local_deleted_at IS NULL;

INSERT INTO clash_lens_schema_migrations(version) VALUES (71)
ON CONFLICT (version) DO NOTHING;
