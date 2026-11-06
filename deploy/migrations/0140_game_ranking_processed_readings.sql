-- Clash Lens deployment migration 0140.
-- S5 of the game-rankings score (game_rankings.py) compares each country
-- list's trophies with our newest saved profile reading when the list was
-- requested. clashlens_game_ranking_readings (migration 0109) takes a
-- reading whose response completed by then even when the worker saved it
-- later: on 31 October 2026, 195 of the readings it chose were saved up to
-- 13.2 seconds after their list's request, changing 4 of 7,298 matches.
-- This read-only function takes only readings the worker had saved by
-- then. The score keeps the old lookup's result beside it. It stores
-- nothing.
BEGIN;

-- For each tag, the trophies of our newest saved profile reading whose
-- response completed, and which the worker saved, at or before that tag's
-- time. A tag with no player or no such reading returns no row.
CREATE FUNCTION clashlens_game_ranking_processed_readings(
    list_tags text[], list_times timestamptz[]
)
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
          AND effect.created_at <= wanted.at
        ORDER BY observation.response_completed_at DESC, observation.id DESC, effect.id DESC
        LIMIT 1
    ) AS reading;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_processed_readings(text[], timestamptz[]) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_game_ranking_processed_readings(text[], timestamptz[])
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_game_ranking_processed_readings(text[], timestamptz[])
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (140)
ON CONFLICT (version) DO NOTHING;
COMMIT;
