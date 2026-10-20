-- Clash Lens deployment migration 0121.
-- The weekly league record, stage 2: its decisions now decide Legend days,
-- so one player whose decision fails must not stop the rest. The decider
-- decides each player on their own; one whose decision fails is set aside
-- (python/src/clashlens/league_week.py _decide_batch): their marks stay,
-- with when it failed, until new evidence marks them again. The collector's
-- failed-work count, which the failed-work alert reads, includes each such
-- player (collector_metrics.py). A player looked up on the website reads as
-- still being checked while their marks wait (api_player_lookup.py).
--
-- Adding a column with no default writes no row; it holds the marks table's
-- write lock briefly.
BEGIN;

ALTER TABLE league_week_dirty ADD COLUMN IF NOT EXISTS failed_at timestamptz;

GRANT UPDATE (failed_at) ON TABLE league_week_dirty TO clashlens_python_worker;
GRANT SELECT ON TABLE league_week_dirty TO clashlens_collector;
-- A looked-up player's page keeps checking while their league is decided.
GRANT SELECT (player_id, failed_at) ON TABLE league_week_dirty TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (121)
ON CONFLICT (version) DO NOTHING;
COMMIT;
