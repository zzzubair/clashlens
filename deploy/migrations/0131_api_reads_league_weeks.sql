-- Clash Lens deployment migration 0131.
-- The Live Leaderboard leaves out a player the weekly league record puts
-- below Legend I this week, and ranks a promoted player from the Monday
-- they entered Legend I, so the API, and the Discord bot with its role,
-- reads the record. A grant only; it locks nothing for longer than the
-- catalog update.
BEGIN;

GRANT SELECT ON player_league_weeks TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (131)
ON CONFLICT (version) DO NOTHING;
COMMIT;
