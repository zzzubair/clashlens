-- Clash Lens deployment migration 0062.
-- Saved army totals per rank band for the newest frozen leaderboard.
--
-- The Armies page's Top N and rank-band views count every attack by the
-- players in that part of the newest selected day's frozen leaderboard, on
-- every selected Legend day. Reading those facts grows with each stored day.
-- The worker saves each Legend day's totals for the 14 rank bands covering
-- ranks 1-1000 of the newest leaderboard, one row per lens and page category,
-- and the page adds up at most 28 x 14 rows.
--
-- Each Season keeps only its newest leaderboard's rows: about 8,600 at day
-- 28, replaced at each Reset. Rows go with their Legend day when season
-- retirement deletes its completed-day marker.
BEGIN;

CREATE TABLE IF NOT EXISTS army_analytics_rank_band_totals (
    snapshot_id bigint NOT NULL
        REFERENCES leaderboard_snapshots (id) ON DELETE CASCADE,
    lens text NOT NULL CHECK (lens IN ('offense', 'defense')),
    category text NOT NULL,
    season_day_number integer NOT NULL CHECK (season_day_number BETWEEN 1 AND 28),
    first_position integer NOT NULL CHECK (first_position BETWEEN 1 AND 1000),
    official_season_id text NOT NULL,
    ranked_day_start timestamptz NOT NULL
        REFERENCES army_analytics_completed_days (ranked_day_start)
        ON DELETE CASCADE,
    -- The completed-day marker hash these totals were counted from. A day
    -- rebuilt since no longer matches, and the page reads its facts instead.
    fact_input_hash text NOT NULL CHECK (fact_input_hash ~ '^[0-9a-f]{64}$'),
    -- SHA-256 of the band's ordered fact IDs and input hashes; NULL without facts.
    source_digest text CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    totals jsonb NOT NULL CHECK (jsonb_typeof(totals) = 'object'),
    PRIMARY KEY (snapshot_id, lens, category, season_day_number, first_position)
);

CREATE INDEX IF NOT EXISTS army_analytics_rank_band_totals_day
    ON army_analytics_rank_band_totals (ranked_day_start);
CREATE INDEX IF NOT EXISTS army_analytics_rank_band_totals_season
    ON army_analytics_rank_band_totals (official_season_id);

REVOKE ALL ON army_analytics_rank_band_totals FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON army_analytics_rank_band_totals
    TO clashlens_python_worker;
GRANT SELECT ON army_analytics_rank_band_totals TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (62)
ON CONFLICT (version) DO NOTHING;
COMMIT;
