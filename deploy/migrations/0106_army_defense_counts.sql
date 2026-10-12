-- Clash Lens deployment migration 0106.
-- Each completed Legend day's defense counts per defender and distinct army
-- (army_types.py), the defense counterpart of 0102's attack counts, for the
-- Legends averages on the dashboard's Armies on defense card. A defense's
-- army is the attacker's, read from the defender's own battle report.
--
-- army_package_defense_day_counts holds, per completed Legend day, one row
-- per defender and distinct army that attacked them: 88,080 defenses on
-- Season day 7 (11 Oct 2026), so up to about 88,000 rows, about 10.6 MB a
-- day at the attack table's 120 bytes a row, and about 300 MB for a 28-day
-- Season. Each Season's rows are kept like the attack counts: neither table
-- is tied to the completed-day markers, so season retirement keeps them.
--
-- Numbered 0106, leaving 0105 to the Discord bot's migration: ./ops applies
-- every migration the database lacks, in file order, so a gap is fine and
-- either can merge first.
BEGIN;

-- The completed-day marker hash each day's defense counts were made from. A
-- day rebuilt since no longer matches and is counted again.
CREATE TABLE IF NOT EXISTS army_type_defense_counted_days (
    ranked_day_start timestamptz PRIMARY KEY,
    fact_input_hash text NOT NULL CHECK (fact_input_hash ~ '^[0-9a-f]{64}$')
);

CREATE TABLE IF NOT EXISTS army_package_defense_day_counts (
    ranked_day_start timestamptz NOT NULL,
    population_player_id bigint NOT NULL,
    -- 0 when the army could not be read; minus the battle_army_decodes id of
    -- an older partly read army that has no exact_armies row.
    exact_army_id bigint NOT NULL,
    defenses smallint NOT NULL CHECK (defenses > 0),
    one_star smallint NOT NULL,
    two_star smallint NOT NULL,
    three_star smallint NOT NULL,
    destruction integer NOT NULL,
    trophies_lost integer NOT NULL
);
CREATE INDEX IF NOT EXISTS army_package_defense_day_counts_selection
    ON army_package_defense_day_counts (ranked_day_start, population_player_id);

REVOKE ALL ON army_type_defense_counted_days, army_package_defense_day_counts
    FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON army_type_defense_counted_days,
    army_package_defense_day_counts TO clashlens_python_worker;
GRANT SELECT ON army_type_defense_counted_days, army_package_defense_day_counts
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (106)
ON CONFLICT (version) DO NOTHING;
COMMIT;
