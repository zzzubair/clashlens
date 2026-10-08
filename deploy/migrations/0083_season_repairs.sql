-- Clash Lens deployment migration 0083.
-- The receipt of each Season repair (python/src/clashlens/domain_repair.py
-- season_repair): one row per Season and rule revision the repair ran
-- under, with the Season's saved days and published Daily boards as they
-- were before it, the last player whose days it queued, when it queued the
-- board rebuilds, and the last player whose Season summary it stored again.
-- A few kilobytes per repair, written by an operator command; never grown
-- by collection.
BEGIN;

CREATE TABLE IF NOT EXISTS season_repairs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    official_season_id text NOT NULL CHECK (official_season_id <> ''),
    rule_revision text NOT NULL CHECK (rule_revision <> ''),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    before_days jsonb NOT NULL CHECK (jsonb_typeof(before_days) = 'object'),
    before_boards jsonb NOT NULL CHECK (jsonb_typeof(before_boards) = 'object'),
    queued_through_player_id bigint NOT NULL DEFAULT 0,
    boards_queued_at timestamptz,
    summaries_through_player_id bigint NOT NULL DEFAULT 0,
    UNIQUE (official_season_id, rule_revision)
);

-- The repair command runs in the worker container as the worker role.
REVOKE ALL ON season_repairs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON season_repairs TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (83)
ON CONFLICT (version) DO NOTHING;
COMMIT;
