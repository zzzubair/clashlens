-- Clash Lens deployment migration 0019.
-- Compact historical player-season summaries. One independently readable
-- record per (player, season) with typed season totals and up to 28 compact
-- daily trophy entries. No battle IDs, battle evidence, or heavyweight JSON.
-- Existing detail is retained; this migration deletes nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS player_season_summaries (
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    official_season_id text NOT NULL,
    season_start timestamptz,
    season_end timestamptz,
    start_trophies integer CHECK (start_trophies IS NULL OR start_trophies >= 0),
    end_trophies integer CHECK (end_trophies IS NULL OR end_trophies >= 0),
    final_rank integer CHECK (final_rank IS NULL OR final_rank >= 1),
    attack_count integer CHECK (attack_count IS NULL OR attack_count >= 0),
    attack_gain integer CHECK (attack_gain IS NULL OR attack_gain >= 0),
    attack_three_star_count integer CHECK (
        attack_three_star_count IS NULL OR attack_three_star_count >= 0
    ),
    defense_count integer CHECK (defense_count IS NULL OR defense_count >= 0),
    defense_loss integer CHECK (defense_loss IS NULL OR defense_loss >= 0),
    defense_three_star_count integer CHECK (
        defense_three_star_count IS NULL OR defense_three_star_count >= 0
    ),
    net_trophy_change integer,
    attack_star_0 integer NOT NULL DEFAULT 0 CHECK (attack_star_0 >= 0),
    attack_star_1 integer NOT NULL DEFAULT 0 CHECK (attack_star_1 >= 0),
    attack_star_2 integer NOT NULL DEFAULT 0 CHECK (attack_star_2 >= 0),
    attack_star_3 integer NOT NULL DEFAULT 0 CHECK (attack_star_3 >= 0),
    attack_star_unknown integer NOT NULL DEFAULT 0 CHECK (attack_star_unknown >= 0),
    defense_star_0 integer NOT NULL DEFAULT 0 CHECK (defense_star_0 >= 0),
    defense_star_1 integer NOT NULL DEFAULT 0 CHECK (defense_star_1 >= 0),
    defense_star_2 integer NOT NULL DEFAULT 0 CHECK (defense_star_2 >= 0),
    defense_star_3 integer NOT NULL DEFAULT 0 CHECK (defense_star_3 >= 0),
    defense_star_unknown integer NOT NULL DEFAULT 0 CHECK (defense_star_unknown >= 0),
    days_observed integer NOT NULL DEFAULT 0 CHECK (
        days_observed BETWEEN 0 AND 28
    ),
    days_missing integer NOT NULL DEFAULT 0 CHECK (
        days_missing BETWEEN 0 AND 28
    ),
    missing_days integer[] NOT NULL DEFAULT '{}',
    coverage_state text NOT NULL DEFAULT 'partial' CHECK (
        coverage_state IN ('complete', 'partial')
    ),
    unresolved_flags text[] NOT NULL DEFAULT '{}',
    daily_entries jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(daily_entries) = 'array'
        AND jsonb_array_length(daily_entries) <= 28
        AND octet_length(daily_entries::text) <= 32768
    ),
    projection_version text NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (player_id, official_season_id),
    CHECK (season_end IS NULL OR season_start IS NULL OR season_end > season_start)
);
CREATE INDEX IF NOT EXISTS player_season_summaries_season
    ON player_season_summaries (official_season_id, player_id);

-- The worker materializes summaries from published daily logs; the public
-- API only reads them. No collector, account, or replay access.
GRANT SELECT, INSERT, UPDATE ON TABLE player_season_summaries
    TO clashlens_python_worker;
GRANT SELECT ON TABLE player_season_summaries TO clashlens_python_api;
GRANT SELECT ON TABLE api_frozen_leaderboards TO clashlens_python_worker;
GRANT SELECT ON TABLE api_frozen_leaderboard_entries TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (19)
ON CONFLICT (version) DO NOTHING;
COMMIT;
