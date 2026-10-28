-- Clash Lens deployment migration 0135.
-- The Notebook's Tomorrow screen (notebook_tomorrow.py) ranks bases against
-- the armies that attack players near a Clasher's Reset trophies. The
-- worker (notebook_meta.py) saves each counted Legend day's defenses by the
-- attacker's army type and the defender's Reset trophies in 10-trophy
-- steps, from army_package_defense_day_counts. No existing row changes.
--
-- Size: about 1,500 rows a Legend day (about 16 army types times the steps
-- players start on), about 40 bytes a row. Only the last 28 Legend days are
-- kept, so it stays near 45,000 rows, about 2 MB.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_meta_counts (
    ranked_day_start timestamptz NOT NULL,
    type_id smallint NOT NULL,
    -- The defenders' Reset trophies, divided by 10.
    step smallint NOT NULL,
    defenses integer NOT NULL CHECK (defenses > 0),
    three_star integer NOT NULL,
    trophies_lost integer NOT NULL,
    PRIMARY KEY (ranked_day_start, type_id, step)
);

-- Which day's defense counts and army type rules each day was saved from;
-- a day recounted or relabelled since is saved again.
CREATE TABLE IF NOT EXISTS notebook_meta_days (
    ranked_day_start timestamptz PRIMARY KEY,
    fact_input_hash text NOT NULL,
    rules_version text NOT NULL
);

REVOKE ALL ON notebook_meta_counts, notebook_meta_days FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON notebook_meta_counts, notebook_meta_days
    TO clashlens_python_worker;
GRANT SELECT ON notebook_meta_counts, notebook_meta_days TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (135)
ON CONFLICT (version) DO NOTHING;
COMMIT;
