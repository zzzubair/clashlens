-- Clash Lens deployment migration 0099.
-- The game's own ranking lists, pulled once a day around each Reset, and the
-- daily score of the published Daily board against them (game_rankings.py).
-- Pulls keep the exact raw bytes, gzip-compressed, with the request time and
-- location; they are deleted 28 days after their Reset. A day is about 260
-- pulls and 4 MB compressed (43 MB raw), so about 115 MB at most. Scores are
-- kept forever, about 2 KB a day per board version scored.
BEGIN;

CREATE TABLE game_ranking_pulls (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The Reset this pull belongs to.
    boundary_at timestamptz NOT NULL,
    -- locations: the weekly country list; global_early: the global top 200
    -- just after the Reset; global_poll: a global pull taken while waiting
    -- for previousRank to switch; global_reset: the first global pull whose
    -- previousRank holds this Reset's ranks; country: one country's top 200.
    purpose text NOT NULL CHECK (
        purpose IN ('locations', 'global_early', 'global_poll', 'global_reset', 'country')
    ),
    -- The game's location ID; NULL for the global list and the locations list.
    location_id integer,
    country_code text,
    requested_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL,
    -- NULL when no answer came back.
    http_status integer,
    -- NULL for a readable 200 answer; otherwise why the pull failed.
    failure text,
    -- How a global_reset pull was judged switched: 'changed' from the previous
    -- day's, or 'clock' when there was no previous day's to compare.
    switch_basis text CHECK (switch_basis IN ('changed', 'clock')),
    body_sha256 text CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    body_bytes integer CHECK (body_bytes >= 0),
    body_gzip bytea,
    CHECK ((purpose = 'global_reset') = (switch_basis IS NOT NULL))
);
CREATE INDEX game_ranking_pulls_boundary
    ON game_ranking_pulls (boundary_at, purpose, location_id);

CREATE TABLE game_ranking_scores (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL,
    -- The published frozen Daily board compared, by its ID and version.
    snapshot_id bigint NOT NULL,
    snapshot_version integer NOT NULL,
    -- 'unknown' when any pull that day failed or is missing; never a pass.
    status text NOT NULL CHECK (status IN ('scored', 'unknown')),
    score jsonb NOT NULL,
    scored_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (boundary_at, snapshot_id)
);

-- The published frozen Daily board for one Reset, as the website shows it.
CREATE FUNCTION clashlens_game_ranking_board(reset_at timestamptz)
RETURNS TABLE (
    snapshot_id bigint, snapshot_version integer, normalized_tag text,
    board_position integer, trophies integer, confidence text
)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT snapshot.id, snapshot.version, player.normalized_tag, entry.position,
           entry.trophies, entry.confidence
    FROM leaderboard_snapshots AS snapshot
    JOIN leaderboard_snapshot_entries AS entry ON entry.snapshot_id = snapshot.id
    JOIN players AS player ON player.id = entry.player_id
    WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
      AND snapshot.boundary_at = reset_at
      AND NOT EXISTS (
          SELECT 1 FROM boundary_publication_generations AS generation
          WHERE generation.snapshot_id = snapshot.id
            AND generation.snapshot_state = 'superseded'
      )
    ORDER BY entry.position;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_board(timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON TABLE game_ranking_pulls, game_ranking_scores
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT SELECT, INSERT, DELETE ON TABLE game_ranking_pulls TO clashlens_collector;
GRANT SELECT, INSERT ON TABLE game_ranking_scores TO clashlens_collector;
REVOKE ALL ON FUNCTION clashlens_game_ranking_board(timestamptz)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_game_ranking_board(timestamptz) TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (99)
ON CONFLICT (version) DO NOTHING;
COMMIT;
