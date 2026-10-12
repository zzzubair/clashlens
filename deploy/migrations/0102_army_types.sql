-- Clash Lens deployment migration 0102.
-- Army types: one level-1 type per distinct army, and each completed Legend
-- day's attack counts per player and distinct army (army_types.py).
--
-- army_package_types holds one row per exact_armies row: 180,085 distinct
-- armies on 11 Oct 2026, about 10 MB. A rule change rewrites only these rows.
--
-- army_package_day_counts holds, per completed Legend day, one row per
-- attacker and distinct army they attacked with: 34,998 rows for 10 Oct 2026,
-- about 4 MB a day and 110 MB for a 28-day Season. Each Season's rows are
-- kept for a later Season picker: neither counts table is tied to the
-- completed-day markers, so season retirement deleting a marker keeps them.
BEGIN;

CREATE TABLE IF NOT EXISTS army_package_types (
    exact_army_id bigint PRIMARY KEY REFERENCES exact_armies (id) ON DELETE CASCADE,
    -- A permanent type id from army_types.TYPE_NAMES; names live in code.
    type_id smallint NOT NULL CHECK (type_id > 0),
    rules_version text NOT NULL
);

-- Rule versions whose labelling of every saved distinct army has finished.
CREATE TABLE IF NOT EXISTS army_type_rule_versions (
    rules_version text PRIMARY KEY,
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The completed-day marker hash each day's counts were made from. A day
-- rebuilt since no longer matches and is counted again.
CREATE TABLE IF NOT EXISTS army_type_counted_days (
    ranked_day_start timestamptz PRIMARY KEY,
    fact_input_hash text NOT NULL CHECK (fact_input_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS army_package_day_counts (
    ranked_day_start timestamptz NOT NULL,
    population_player_id bigint NOT NULL,
    -- 0 when the army could not be read; minus the battle_army_decodes id of
    -- an older partly read army that has no exact_armies row.
    exact_army_id bigint NOT NULL,
    attacks smallint NOT NULL CHECK (attacks > 0),
    one_star smallint NOT NULL,
    two_star smallint NOT NULL,
    three_star smallint NOT NULL,
    destruction integer NOT NULL
);
CREATE INDEX IF NOT EXISTS army_package_day_counts_selection
    ON army_package_day_counts (ranked_day_start, population_player_id);

REVOKE ALL ON army_package_types, army_type_rule_versions,
    army_type_counted_days, army_package_day_counts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON army_package_types,
    army_type_rule_versions, army_type_counted_days, army_package_day_counts
    TO clashlens_python_worker;
GRANT SELECT ON army_package_types, army_type_rule_versions,
    army_type_counted_days, army_package_day_counts
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (102)
ON CONFLICT (version) DO NOTHING;
COMMIT;
