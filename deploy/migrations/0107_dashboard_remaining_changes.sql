-- Clash Lens deployment migration 0107.
-- Examples of how players' trophies moved for the rest of a Legend day, for
-- the dashboard's projected place at the next Reset (projected_place.py).
--
-- For each of the last 7 finished Legend days and each 15-minute step from
-- its Reset, one row holds every Daily board player's situation at that
-- moment (attacks and defenses still open, attacks on the 3 days before, and
-- rank band or trophy level), the trophies they won or lost from then to
-- the end of the day, and whether a battle that moved their trophies was
-- still to come, which a day's battles can leave at 0 overall. The worker
-- writes a day once its Daily board publishes and deletes days older than 7.
-- At about 13,300 players that is 96 rows a day of two 13,300-number arrays
-- and one 13,300-flag array: 7 days of made-up data that size took 15.7 MB on
-- disk before the flags, which add at most 13.3 KB a row, 9 MB for 7 days.
-- Days are replaced one by one, so it does not grow.
BEGIN;

CREATE TABLE IF NOT EXISTS dashboard_remaining_changes (
    ranked_day_start timestamptz NOT NULL,
    slot smallint NOT NULL CHECK (slot BETWEEN 0 AND 95),
    -- The Daily board the day was counted from.
    snapshot_id bigint NOT NULL
        REFERENCES leaderboard_snapshots (id) ON DELETE CASCADE,
    situations smallint[] NOT NULL,
    changes smallint[] NOT NULL
        CHECK (cardinality(changes) = cardinality(situations)),
    moved boolean[] NOT NULL
        CHECK (cardinality(moved) = cardinality(situations)),
    built_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ranked_day_start, slot)
);

CREATE INDEX IF NOT EXISTS dashboard_remaining_changes_snapshot
    ON dashboard_remaining_changes (snapshot_id);

REVOKE ALL ON dashboard_remaining_changes FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON dashboard_remaining_changes
    TO clashlens_python_worker;
GRANT SELECT ON dashboard_remaining_changes TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (107)
ON CONFLICT (version) DO NOTHING;
COMMIT;
