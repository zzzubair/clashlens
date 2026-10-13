-- Clash Lens deployment migration 0109.
-- S5 of the game-rankings score (game_rankings.py) compares each country
-- list's live trophies with our newest saved profile reading at the moment
-- the list was pulled. The scorer's collector login cannot read saved
-- profiles, so this read-only function gives it just those trophies. One
-- score asks for about 6,000 players, each one index lookup
-- (collector_observations_profile_player_time, migration 0049). It stores
-- nothing.
BEGIN;

-- For each tag, the trophies of our newest saved profile reading whose
-- response completed at or before that tag's time. A tag with no player or
-- no saved reading by then returns no row. A reading the game repeated is
-- saved as a new sighting of the old profile, so the sighting gives the time.
CREATE FUNCTION clashlens_game_ranking_readings(list_tags text[], list_times timestamptz[])
RETURNS TABLE (normalized_tag text, trophies integer)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT wanted.tag, reading.trophies
    FROM unnest(list_tags, list_times) AS wanted(tag, at)
    JOIN players AS player ON player.normalized_tag = wanted.tag
    CROSS JOIN LATERAL (
        SELECT profile.trophies
        FROM collector_observations AS observation
        JOIN player_profile_effects AS effect ON effect.observation_id = observation.id
        JOIN player_profile_versions AS profile ON profile.id = effect.profile_version_id
        WHERE observation.player_id = player.id AND observation.endpoint = 'profile'
          AND observation.response_completed_at <= wanted.at
        ORDER BY observation.response_completed_at DESC, observation.id DESC, effect.id DESC
        LIMIT 1
    ) AS reading;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_readings(text[], timestamptz[]) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_game_ranking_readings(text[], timestamptz[])
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_game_ranking_readings(text[], timestamptz[])
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (109)
ON CONFLICT (version) DO NOTHING;
COMMIT;
