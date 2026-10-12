-- Clash Lens deployment migration 0103.
-- The Live board ranks a tracked Legend I player at 5,000 who has no Legend
-- battle saved this Season from their newest sighted profile
-- (python/src/clashlens/api_leaderboard.py). Finding that profile and the
-- missing battle reads about 16 scattered pages per player; on 11 October
-- 2026 that was 22,000 pages from disk and 8 s for 1,365 players on every
-- board read. The worker now finds them every five minutes, as it counts
-- tie-order attacks, and keeps here each such player's chosen profile for
-- the current Season. One row per player found (about 1,400 on 11 October
-- 2026, under 1 MB); a player who no longer qualifies loses their row, so
-- the table does not grow with Seasons.
BEGIN;

CREATE TABLE IF NOT EXISTS live_unplayed_profiles (
    player_id bigint PRIMARY KEY REFERENCES players (id),
    official_season_id text NOT NULL CHECK (official_season_id <> ''),
    -- The chosen profile and the parts of it the board shows, which never
    -- change once saved.
    profile_version_id bigint NOT NULL REFERENCES player_profile_versions (id),
    observation_id bigint NOT NULL,
    name text NOT NULL,
    trophies integer NOT NULL,
    clan text,
    -- Its newest sighting; no observation when it has no saved sighting.
    sighted_at timestamptz NOT NULL,
    sighting_observation_id bigint,
    refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON live_unplayed_profiles FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON live_unplayed_profiles
    TO clashlens_python_worker;
GRANT SELECT ON live_unplayed_profiles TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (103)
ON CONFLICT (version) DO NOTHING;
COMMIT;
