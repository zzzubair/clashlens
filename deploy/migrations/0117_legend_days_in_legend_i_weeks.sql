-- Clash Lens deployment migration 0117.
-- The weekly league record, stage 2: a public Legend day exists only in a
-- week the record says was Legend I (python/src/clashlens/league_days.py,
-- docs/domain.md, Weekly league record).
--
-- Each public day (api_player_daily_logs) from the current Season, which
-- started on 5 October 2026 when the record begins, names its week's record
-- row: (player, week, 'legend_i'). The database then refuses a public day in
-- a week decided below Legend I or not known, and refuses a decision taking
-- a week out of Legend I while one of its public days remains; the worker
-- deletes those first. Days of earlier Seasons have no record and name none.
--
-- Both rules are added NOT VALID: they hold for every day saved from now on,
-- and the public days saved before are not checked yet. The Season repair
-- names the week of each of those in a Legend I week and deletes the others
-- (league_days.settle); then
--     ALTER TABLE api_player_daily_logs VALIDATE CONSTRAINT api_player_daily_logs_legend_i_week;
--     ALTER TABLE api_player_daily_logs VALIDATE CONSTRAINT api_player_daily_logs_names_week;
-- check them all. The two columns stay nullable while earlier Seasons' days
-- are kept.
--
-- The record's foreign key check finds a week's public days by the
-- (player_id, ranked_day_start) index already there. Adding the columns
-- writes no row; adding the rules holds the table's write lock briefly.
BEGIN;

ALTER TABLE api_player_daily_logs
    ADD COLUMN IF NOT EXISTS week_start timestamptz,
    ADD COLUMN IF NOT EXISTS league text;

ALTER TABLE api_player_daily_logs
    DROP CONSTRAINT IF EXISTS api_player_daily_logs_legend_i_week,
    ADD CONSTRAINT api_player_daily_logs_legend_i_week
        FOREIGN KEY (player_id, week_start, league)
        REFERENCES player_league_weeks (player_id, week_start, league) NOT VALID,
    DROP CONSTRAINT IF EXISTS api_player_daily_logs_names_week,
    ADD CONSTRAINT api_player_daily_logs_names_week CHECK (
        ranked_day_start < timestamptz '2026-10-05 05:00:00+00'
        OR coalesce(league = 'legend_i'
                    AND week_start = clashlens_week_start(ranked_day_start), false)
    ) NOT VALID;

-- The worker deletes a week's public days when it leaves Legend I.
GRANT DELETE ON TABLE api_player_daily_logs TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (117)
ON CONFLICT (version) DO NOTHING;
COMMIT;
