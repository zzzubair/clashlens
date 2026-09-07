-- Clash Lens deployment migration 0020.
-- Shared whole-season army summaries. One independently readable record
-- per (season, lens, category) holds whole-season usage counts and rates
-- for deployed troops/spells/siege (and the remaining army categories),
-- 0/1/2/3-star attack counts with the three-star rate derived from the
-- stored attack sample, and the underlying denominators plus
-- excluded/undecodable counts and honest coverage. Historical reads use
-- these rows only; they never touch battle facts or drill into individual
-- battles. Existing detail is retained; this migration deletes nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS army_season_summaries (
    official_season_id text NOT NULL,
    lens text NOT NULL CHECK (lens IN ('offense', 'defense')),
    category text NOT NULL CHECK (category IN (
        'troops', 'spells', 'siege', 'heroes', 'pets', 'equipment',
        'equipment-for-hero', 'cc-troops', 'hero-pet', 'hero-equipment',
        'cc-composition'
    )),
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
    total_attacks integer NOT NULL DEFAULT 0 CHECK (total_attacks >= 0),
    usable_army_sample integer NOT NULL DEFAULT 0 CHECK (
        usable_army_sample >= 0
    ),
    army_states jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(army_states) = 'object'
    ),
    unknown_affected_attacks integer NOT NULL DEFAULT 0 CHECK (
        unknown_affected_attacks >= 0
    ),
    unknown_component_occurrences integer NOT NULL DEFAULT 0 CHECK (
        unknown_component_occurrences >= 0
    ),
    perspective_disagreement_count integer NOT NULL DEFAULT 0 CHECK (
        perspective_disagreement_count >= 0
    ),
    missing_trophy_membership_evidence integer NOT NULL DEFAULT 0 CHECK (
        missing_trophy_membership_evidence >= 0
    ),
    result_rows jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(result_rows) = 'array'
        AND octet_length(result_rows::text) <= 524288
    ),
    projection_version text NOT NULL,
    content_digest text NOT NULL CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (official_season_id, lens, category)
);
CREATE INDEX IF NOT EXISTS army_season_summaries_season
    ON army_season_summaries (official_season_id);

-- The worker materializes summaries from versioned battle facts; the public
-- API only reads them. No collector, account, or replay access.
GRANT SELECT, INSERT, UPDATE ON TABLE army_season_summaries
    TO clashlens_python_worker;
GRANT SELECT ON TABLE army_season_summaries TO clashlens_python_api;
GRANT SELECT ON TABLE army_analytics_battle_facts TO clashlens_python_worker;
GRANT SELECT ON TABLE army_analytics_completed_days TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (20)
ON CONFLICT (version) DO NOTHING;
COMMIT;
