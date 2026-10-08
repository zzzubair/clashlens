-- Clash Lens deployment migration 0082.
-- Every 30 seconds the collector returns upload leases that ran out to the
-- waiting list. With no index for leased uploads, each pass read the whole
-- upload table to find the few dozen leased rows: 404 ms on average, and
-- about 1.16 GB of database cache read in one pass on 8 October 2026. This
-- index holds only leased uploads, so it stays as small as the uploads in
-- flight, and each pass reads it instead.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again.
DROP INDEX CONCURRENTLY IF EXISTS collector_response_uploads_lease_expiry;

CREATE INDEX CONCURRENTLY collector_response_uploads_lease_expiry
    ON collector_response_uploads (lease_expires_at)
    WHERE state = 'leased';

INSERT INTO clash_lens_schema_migrations(version) VALUES (82)
ON CONFLICT (version) DO NOTHING;
