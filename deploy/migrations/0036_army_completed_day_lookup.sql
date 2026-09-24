-- Let army analytics verify complete Legend days without scanning every
-- player's retained daily publication for the selected season.
BEGIN;

CREATE INDEX IF NOT EXISTS api_player_daily_logs_completed_season_day_v3
    ON api_player_daily_logs (
        official_season_id, season_day_number, ranked_day_start
    )
    WHERE state = 'Complete' AND coverage = 'complete';

INSERT INTO clash_lens_schema_migrations(version) VALUES (36)
ON CONFLICT (version) DO NOTHING;
COMMIT;
