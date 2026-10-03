-- Clash Lens deployment migration 0058.
-- Smaller army battle facts and per-day army totals.
--
-- Each fact copied six army documents that its decoded-army record (decode_id)
-- already holds: about 86% of a 324 MB Legend day. New facts leave those
-- columns empty and army_analytics_battle_facts_with_armies reads them through
-- decode_id. Facts saved before this keep their copies, which the view
-- prefers, so no stored row changes and nothing is deleted. A fact's
-- decode_id foreign key keeps its decoded-army record from being deleted.
--
-- army_analytics_day_totals keeps each Legend day's per-lens counts so a
-- season summary adds up 28 small rows instead of re-reading every fact.
-- About 2 rows and tens of kilobytes per Legend day; rows go when season
-- retirement deletes their completed-day marker.
BEGIN;

ALTER TABLE army_analytics_battle_facts
    ALTER COLUMN home_troops DROP NOT NULL,
    ALTER COLUMN home_troops DROP DEFAULT,
    ALTER COLUMN spells DROP NOT NULL,
    ALTER COLUMN spells DROP DEFAULT,
    ALTER COLUMN siege DROP NOT NULL,
    ALTER COLUMN siege DROP DEFAULT,
    ALTER COLUMN cc_troops DROP NOT NULL,
    ALTER COLUMN cc_troops DROP DEFAULT,
    ALTER COLUMN heroes DROP NOT NULL,
    ALTER COLUMN heroes DROP DEFAULT,
    ALTER COLUMN unresolved_components DROP NOT NULL,
    ALTER COLUMN unresolved_components DROP DEFAULT;

CREATE OR REPLACE VIEW army_analytics_battle_facts_with_armies AS
SELECT fact.id, fact.battle_id, fact.evidence_id, fact.decode_id,
       fact.source_ranked_day_version_id, fact.ranked_day_start,
       fact.official_season_id, fact.season_day_number, fact.lens,
       fact.population_player_id, fact.battle_time_trophies, fact.stars,
       fact.destruction_percentage, fact.army_state, fact.failure_reason,
       COALESCE(fact.home_troops, decode.home_troops, '[]'::jsonb) AS home_troops,
       COALESCE(fact.spells, decode.spells, '[]'::jsonb) AS spells,
       COALESCE(fact.siege, decode.siege, '[]'::jsonb) AS siege,
       COALESCE(fact.cc_troops, decode.cc_troops, '[]'::jsonb) AS cc_troops,
       COALESCE(fact.heroes, decode.heroes, '[]'::jsonb) AS heroes,
       COALESCE(
           fact.unresolved_components, decode.unresolved_components, '[]'::jsonb
       ) AS unresolved_components,
       fact.perspective_disagreement, fact.input_hash, fact.version,
       fact.is_current, fact.created_at, fact.supersedes_id
FROM army_analytics_battle_facts AS fact
LEFT JOIN battle_army_decodes AS decode ON decode.id = fact.decode_id;

-- A day build sweeps and hashes one Legend day's current facts. Without this
-- both read the whole table: 562 MB to find one day on 2026-10-03, growing
-- every day of the season.
CREATE INDEX IF NOT EXISTS army_analytics_battle_facts_current_day
    ON army_analytics_battle_facts (ranked_day_start) WHERE is_current;

REVOKE ALL ON army_analytics_battle_facts_with_armies FROM PUBLIC;
GRANT SELECT ON army_analytics_battle_facts_with_armies
    TO clashlens_python_worker, clashlens_python_api;

CREATE TABLE IF NOT EXISTS army_analytics_day_totals (
    ranked_day_start timestamptz NOT NULL
        REFERENCES army_analytics_completed_days (ranked_day_start)
        ON DELETE CASCADE,
    lens text NOT NULL CHECK (lens IN ('offense', 'defense')),
    official_season_id text NOT NULL,
    season_day_number integer NOT NULL CHECK (season_day_number BETWEEN 1 AND 28),
    -- The completed-day marker hash these totals were counted from. A day
    -- rebuilt without new totals no longer matches and is counted again.
    fact_input_hash text NOT NULL CHECK (fact_input_hash ~ '^[0-9a-f]{64}$'),
    total_attacks integer NOT NULL CHECK (total_attacks >= 0),
    army_states jsonb NOT NULL CHECK (jsonb_typeof(army_states) = 'object'),
    unknown_affected_attacks integer NOT NULL CHECK (unknown_affected_attacks >= 0),
    unknown_component_occurrences integer NOT NULL
        CHECK (unknown_component_occurrences >= 0),
    perspective_disagreement_count integer NOT NULL
        CHECK (perspective_disagreement_count >= 0),
    -- [unit ID, quantity, uses, one-star, two-star, three-star] rows.
    unit_usage jsonb NOT NULL CHECK (jsonb_typeof(unit_usage) = 'array'),
    PRIMARY KEY (ranked_day_start, lens)
);

REVOKE ALL ON army_analytics_day_totals FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON army_analytics_day_totals
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (58)
ON CONFLICT (version) DO NOTHING;
COMMIT;
