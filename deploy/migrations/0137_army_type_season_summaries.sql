-- Clash Lens deployment migration 0137.
-- A past Season's Armies view: for each Top N the Armies page offers, the
-- attack totals per army type and variant over the Season's Legend days,
-- each day counting the players on that day's own board, as the page showed
-- them during the Season (army_season_summaries.py). The page reads these
-- rows and never the Season's battles.
--
-- One row per Season and Top N: 12 rows of about 2 KB, so about 25 KB a
-- Season and 0.3 MB a year. Season retirement neither reads nor deletes
-- them. This migration deletes nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS army_type_season_summaries (
    official_season_id text NOT NULL,
    population text NOT NULL CHECK (population ~ '^top-[0-9]{1,5}$'),
    -- The Legend days counted, 1 to 28.
    covered_days integer[] NOT NULL CHECK (
        cardinality(covered_days) BETWEEN 1 AND 28
    ),
    -- [type id, variant id, attacks, one-star, two-star, three-star,
    -- destruction, trophies won], as army_types.read_type_rows gives them.
    type_rows jsonb NOT NULL CHECK (
        jsonb_typeof(type_rows) = 'array'
        AND octet_length(type_rows::text) <= 524288
    ),
    rules_version text NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (official_season_id, population)
);

REVOKE ALL ON army_type_season_summaries FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON army_type_season_summaries
    TO clashlens_python_worker;
GRANT SELECT ON army_type_season_summaries TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (137)
ON CONFLICT (version) DO NOTHING;
COMMIT;
