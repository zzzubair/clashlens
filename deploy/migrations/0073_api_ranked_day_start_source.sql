-- Clash Lens deployment migration 0073.
-- The player page labels a Day 1 start of 5,000 by the Season rule, read from
-- the start's source saved in each ranked-day row's evidence.
BEGIN;

GRANT SELECT (input_evidence) ON TABLE ranked_day_versions TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (73)
ON CONFLICT (version) DO NOTHING;
COMMIT;
