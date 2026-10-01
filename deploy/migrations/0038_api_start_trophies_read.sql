-- Clash Lens deployment migration 0038.
-- The player page shows each day's start trophies from the ranked-day row.
BEGIN;

GRANT SELECT (start_trophies) ON TABLE ranked_day_versions TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (38)
ON CONFLICT (version) DO NOTHING;
COMMIT;
