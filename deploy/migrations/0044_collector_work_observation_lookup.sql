BEGIN;

-- Production collector_work has about 37,000 rows (about 5 MB), so building
-- these indexes blocks writes to collector_work for well under a second.
CREATE INDEX IF NOT EXISTS collector_work_profile_observation_lookup
    ON collector_work USING btree (profile_observation_id)
    WHERE profile_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS collector_work_battle_log_observation_lookup
    ON collector_work USING btree (battle_log_observation_id)
    WHERE battle_log_observation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS collector_work_league_history_observation_lookup
    ON collector_work USING btree (league_history_observation_id)
    WHERE league_history_observation_id IS NOT NULL;

INSERT INTO clash_lens_schema_migrations(version) VALUES (44)
ON CONFLICT (version) DO NOTHING;
COMMIT;
