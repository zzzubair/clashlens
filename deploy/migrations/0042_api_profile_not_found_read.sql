-- Clash Lens deployment migration 0042.
-- The Live Leaderboard leaves out a player whose latest saved profile
-- response is a 404 (player not found), so the API reads response status.
BEGIN;

GRANT SELECT (id, http_status) ON TABLE collector_observations
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (42)
ON CONFLICT (version) DO NOTHING;
COMMIT;
