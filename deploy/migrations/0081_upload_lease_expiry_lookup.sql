-- Clash Lens deployment migration 0081.
-- The uploader returns uploads whose lease ran out to the queue every 30
-- seconds. With no index for leased rows, each pass read the whole upload
-- table: on production on 8 October 2026 that was 2.93 million rows and about
-- 1.1 GB, 405 ms on average and up to 6.2 s, while the uploader was falling
-- behind. This index holds only leased rows, at most one per upload in
-- flight, so a pass reads those and stops.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again.
DROP INDEX CONCURRENTLY IF EXISTS collector_response_uploads_lease_expiry;

CREATE INDEX CONCURRENTLY collector_response_uploads_lease_expiry
    ON collector_response_uploads (lease_expires_at)
    WHERE state = 'leased';

INSERT INTO clash_lens_schema_migrations(version) VALUES (81)
ON CONFLICT (version) DO NOTHING;
