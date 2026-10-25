-- One row per Reset whose late-battle correction pass finished with no failed
-- player (late_battle_sweep.py). The worker kept this only in memory, so every
-- restart ran the pass again: on 24 Oct 2026 the 21:05 deploy repeated the pass
-- that had finished at 05:55, correcting 2,192 players over 6 minutes of heavy
-- database work. A pass cut short leaves no row and runs again after a restart.
-- About 50 bytes a row, one row a day: about 20 KB a year. Nothing deletes them.
BEGIN;

CREATE TABLE IF NOT EXISTS late_battle_sweeps (
    boundary_at timestamptz PRIMARY KEY
        CHECK ((boundary_at AT TIME ZONE 'UTC')::time = TIME '05:00'),
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON late_battle_sweeps FROM PUBLIC;
GRANT SELECT, INSERT ON TABLE late_battle_sweeps TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (129)
ON CONFLICT (version) DO NOTHING;
COMMIT;
