-- Clash Lens deployment migration 0039.
-- The player page counts a profile as fresh from the collector's last
-- successful check, including checks whose response was unchanged.
BEGIN;

GRANT SELECT (scope, identity_key, endpoint, last_observation_id, last_success_at)
    ON TABLE collector_response_state TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (39)
ON CONFLICT (version) DO NOTHING;
COMMIT;
