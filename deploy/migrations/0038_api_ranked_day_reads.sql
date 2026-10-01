-- Clash Lens deployment migration 0038.
-- The player page shows each day's start trophies, and streak army analytics
-- finds each member-day's current shield state, from ranked-day rows.
BEGIN;

GRANT SELECT (start_trophies, player_id, ranked_day_start, version, shield_state)
    ON TABLE ranked_day_versions TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (38)
ON CONFLICT (version) DO NOTHING;
COMMIT;
