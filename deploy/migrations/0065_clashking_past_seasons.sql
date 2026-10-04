-- Clash Lens deployment migration 0065.
-- Past Legend Season finishes copied from ClashKing's public API, shown on a
-- player's page with credit to ClashKing. These rows are third-party reports:
-- nothing else reads them, and they never feed our daily logs or totals.
-- See python/src/clashlens/clashking.py.
--
-- Only players someone viewed are fetched, at most once a day each. A refresh
-- replaces that player's rows, so the table grows by one row per finished
-- Season per viewed player: about 100 bytes a row, at most 13 rows a year.
BEGIN;

CREATE TABLE IF NOT EXISTS clashking_history_fetches (
    player_id bigint PRIMARY KEY REFERENCES players (id) ON DELETE CASCADE,
    -- Last time a view claimed a fetch; another fetch waits an hour after it.
    attempted_at timestamptz NOT NULL,
    -- Last successful fetch; the rows below are as of this time.
    fetched_at timestamptz
);

CREATE TABLE IF NOT EXISTS clashking_season_finishes (
    player_id bigint NOT NULL
        REFERENCES clashking_history_fetches (player_id) ON DELETE CASCADE,
    -- Our Season ID (Unix seconds of its start) for a 28-day Season, or
    -- 'YYYY-MM' for a calendar-month Legend season from before them.
    season_id text NOT NULL CHECK (season_id ~ '^([0-9]+|[0-9]{4}-[0-9]{2})$'),
    season_start timestamptz,
    season_end timestamptz,
    -- ClashKing's own label for the row, kept as provenance.
    source_season text NOT NULL CHECK (source_season <> ''),
    trophies integer NOT NULL CHECK (trophies >= 0),
    global_rank integer CHECK (global_rank >= 1),
    PRIMARY KEY (player_id, season_id),
    CHECK (
        (season_start IS NULL AND season_end IS NULL AND season_id ~ '-')
        OR season_end = season_start + interval '28 days'
    )
);

GRANT SELECT, INSERT, UPDATE ON TABLE clashking_history_fetches
    TO clashlens_python_api;
GRANT SELECT, INSERT, DELETE ON TABLE clashking_season_finishes
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (65)
ON CONFLICT (version) DO NOTHING;
COMMIT;
