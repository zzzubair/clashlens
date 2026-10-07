-- Clash Lens deployment migration 0080.
-- The daily board finds each published board's Legend day from the newest
-- ranked day in that board's input list. With no index on (list, ranked day),
-- PostgreSQL walked the ranked-day index across every list's rows (about 5.4
-- million in production on 7 October 2026) and hit the statement time limit,
-- so the daily board showed "Leaderboard unavailable". With this index each
-- lookup reads one row.
--
-- Production got this index by hand on 7 October 2026 under the same name, so
-- an index that already exists and is usable is kept. The index is built
-- without blocking writes, so this file runs outside a transaction: ./ops
-- sends it to psql one statement at a time. A build that fails leaves an
-- unusable index and records no version, so the next run drops that unusable
-- index and builds it again.
DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_index WHERE indexrelid = to_regclass('boundary_publication_manifest_rows_manifest_ranked_day') AND NOT indisvalid) THEN DROP INDEX boundary_publication_manifest_rows_manifest_ranked_day; END IF; END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS boundary_publication_manifest_rows_manifest_ranked_day
    ON boundary_publication_manifest_rows (manifest_id, ranked_day_version_id);

INSERT INTO clash_lens_schema_migrations(version) VALUES (80)
ON CONFLICT (version) DO NOTHING;
