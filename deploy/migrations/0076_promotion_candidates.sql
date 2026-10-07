-- Clash Lens deployment migration 0076.
-- Legend II and Legend III players who can be promoted into Legend I at a
-- Monday Reset, so a Monday re-check can ask for exactly them. One row per
-- tag: the tier and trophies last seen and when. Rows come from the lab's
-- list (the load-promotion-candidates command), from every processed profile
-- showing Legend II or III, and from players already saved with such a
-- profile. A profile showing any other recognized tier removes the row; a
-- Legend I player is tracked from then on.
-- About 130 bytes a row with its indexes: the lab's October 2026 list of
-- 250,680 players is about 33 MB. It grows only with newly seen Legend II and
-- III players and never holds a raw response.
BEGIN;

CREATE TABLE promotion_candidates (
    normalized_tag text PRIMARY KEY,
    league_tier_id integer NOT NULL CHECK (league_tier_id IN (105000034, 105000035)),
    trophies integer CHECK (trophies >= 0),
    checked_at timestamptz NOT NULL
);
-- The Monday re-check reads Legend II first, oldest check first.
CREATE INDEX promotion_candidates_due
    ON promotion_candidates (league_tier_id, checked_at);

-- Record one recognized profile; the caller holds the player's row lock. An
-- older observation never overwrites or removes a newer one, and never changes
-- the row of a player whose saved profile was checked later.
CREATE FUNCTION clashlens_note_promotion_candidate(
    tag text, tier_id integer, trophy_count integer, observed_at timestamptz
)
RETURNS void LANGUAGE sql SECURITY DEFINER
AS $$
    DELETE FROM promotion_candidates
    WHERE normalized_tag = tag AND checked_at <= observed_at
      AND tier_id NOT IN (105000034, 105000035) AND NOT EXISTS (
          SELECT 1 FROM players AS player WHERE player.normalized_tag = tag
            AND GREATEST(player.current_observed_at, player.current_profile_confirmed_at) > observed_at
      );
    INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
    SELECT tag, tier_id, trophy_count, observed_at
    WHERE tier_id IN (105000034, 105000035) AND NOT EXISTS (
        SELECT 1 FROM players AS player WHERE player.normalized_tag = tag
          AND GREATEST(player.current_observed_at, player.current_profile_confirmed_at) > observed_at
    )
    ON CONFLICT (normalized_tag) DO UPDATE SET
        league_tier_id = EXCLUDED.league_tier_id,
        trophies = EXCLUDED.trophies,
        checked_at = EXCLUDED.checked_at
    WHERE promotion_candidates.checked_at < EXCLUDED.checked_at;
$$;

-- Players already saved whose current profile shows Legend II or III, as of
-- that profile's latest check.
INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
SELECT player.normalized_tag, version.league_tier_id, version.trophies,
       COALESCE(GREATEST(player.current_observed_at, player.current_profile_confirmed_at),
                version.observed_at)
FROM players AS player
JOIN player_profile_versions AS version ON version.id = player.current_profile_version_id
WHERE NOT player.active AND version.league_tier_id IN (105000034, 105000035)
ON CONFLICT (normalized_tag) DO NOTHING;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_note_promotion_candidate(text,integer,integer,timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON TABLE promotion_candidates
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE promotion_candidates TO clashlens_collector;
REVOKE ALL ON FUNCTION clashlens_note_promotion_candidate(text,integer,integer,timestamptz)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_note_promotion_candidate(text,integer,integer,timestamptz)
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (76)
ON CONFLICT (version) DO NOTHING;
COMMIT;
