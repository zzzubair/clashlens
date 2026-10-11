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
    -- previousRank differs from global_early's; country: one country's top 200.
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
    body_sha256 text CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
    body_bytes integer CHECK (body_bytes >= 0),
    body_gzip bytea
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

-- The published frozen Daily board for one Reset, as the website shows it:
-- the newest version with no publication or one not superseded, so a board
-- an army-only correction carried into a new publication still counts. A
-- board with no entries is one row with no player.
CREATE FUNCTION clashlens_game_ranking_board(reset_at timestamptz)
RETURNS TABLE (
    snapshot_id bigint, snapshot_version integer, normalized_tag text,
    board_position integer, trophies integer, confidence text,
    measured_coverage numeric
)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    WITH board AS (
        SELECT snapshot.id, snapshot.version, snapshot.measured_coverage
        FROM leaderboard_snapshots AS snapshot
        WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
          AND snapshot.boundary_at = reset_at
          AND (NOT EXISTS (SELECT 1 FROM boundary_publication_generations AS generation
                           WHERE generation.snapshot_id = snapshot.id)
               OR EXISTS (SELECT 1 FROM boundary_publication_generations AS generation
                          WHERE generation.snapshot_id = snapshot.id
                            AND generation.snapshot_state <> 'superseded'))
        ORDER BY snapshot.version DESC, snapshot.id DESC
        LIMIT 1
    )
    SELECT board.id, board.version, player.normalized_tag, entry.position,
           entry.trophies, entry.confidence, board.measured_coverage
    FROM board
    LEFT JOIN (leaderboard_snapshot_entries AS entry
               JOIN players AS player ON player.id = entry.player_id)
      ON entry.snapshot_id = board.id
    ORDER BY entry.position;
$$;

-- Of these tags, the players with a stored Legend battle, as attacker or
-- defender, on a Legend day of this Season before the Reset. Saved profiles
-- do not keep their attack and defense counts, so the battles are the proof.
-- Each side reads the Season's battles once against the few tags asked.
CREATE FUNCTION clashlens_game_ranking_season_battlers(
    list_tags text[], list_season_start timestamptz, list_reset_at timestamptz
)
RETURNS TABLE (normalized_tag text)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    WITH wanted AS (
        SELECT player.id, player.normalized_tag
        FROM players AS player WHERE player.normalized_tag = ANY(list_tags)
    )
    SELECT wanted.normalized_tag
    FROM wanted JOIN legend_battles AS battle ON battle.attacker_player_id = wanted.id
    WHERE battle.ranked_day_start >= list_season_start
      AND battle.ranked_day_start < list_reset_at
    UNION
    SELECT wanted.normalized_tag
    FROM wanted JOIN legend_battles AS battle ON battle.defender_player_id = wanted.id
    WHERE battle.ranked_day_start >= list_season_start
      AND battle.ranked_day_start < list_reset_at;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_board(timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_game_ranking_season_battlers(text[], timestamptz, timestamptz) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON TABLE game_ranking_pulls, game_ranking_scores
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT SELECT, INSERT, DELETE ON TABLE game_ranking_pulls TO clashlens_collector;
GRANT SELECT, INSERT ON TABLE game_ranking_scores TO clashlens_collector;
REVOKE ALL ON FUNCTION clashlens_game_ranking_board(timestamptz),
    clashlens_game_ranking_season_battlers(text[], timestamptz, timestamptz)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_game_ranking_board(timestamptz),
    clashlens_game_ranking_season_battlers(text[], timestamptz, timestamptz)
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (99)
ON CONFLICT (version) DO NOTHING;
COMMIT;
