-- Clash Lens deployment migration 0134.
-- The forgotten-base check (api_notebook.py): a run counts for at most 7
-- Legend days after the Clasher last confirmed it, by opening Clash Lens,
-- picking a base or answering "Still on it?". Later days wait, counting
-- for nothing, until they answer. No existing row changes.
--
-- Size: one row a game account that ever had a base picked (about 40
-- bytes), updated at most once a Legend day; it does not grow with time.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_checks (
    player_id bigint PRIMARY KEY REFERENCES players (id) ON DELETE CASCADE,
    -- The Legend day (its start) the game account's run was last confirmed.
    seen_day timestamptz NOT NULL
);

REVOKE ALL ON notebook_checks FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON notebook_checks TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (134)
ON CONFLICT (version) DO NOTHING;
COMMIT;
