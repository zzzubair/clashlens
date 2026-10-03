-- One delayed settlement check per frozen Reset member: a fresh profile,
-- then the battle log that must cover it, due 20 minutes after the 05:00 UTC
-- Reset. The Reset sweep schedules it with its own Reset work and links it
-- to the member's boundary settlement row. It never blocks ordinary
-- collection or the next Reset, and nothing reads its result yet. About
-- 13,300 rows a day at October 2026 membership; finished rows are kept as
-- the work reference that protects their two responses.
BEGIN;

ALTER TABLE collector_work
    DROP CONSTRAINT collector_work_kind_check,
    ADD CONSTRAINT collector_work_kind_check CHECK (kind IN (
        'initial_collection', 'live_refresh', 'reset_baseline',
        'discovery_profile', 'global_player_rankings', 'reset_settlement'
    )),
    DROP CONSTRAINT collector_work_endpoint_contract,
    ADD CONSTRAINT collector_work_endpoint_contract CHECK (
        (kind = 'global_player_rankings' AND scope = 'global'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND profile_status IN ('pending', 'observed')
            AND battle_log_status = 'not_applicable'
            AND league_history_status = 'not_applicable')
        OR (kind = 'discovery_profile' AND scope = 'player'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND battle_log_status = 'not_applicable'
            AND league_history_status IN ('not_applicable', 'pending', 'observed'))
        OR (kind = 'initial_collection' AND scope = 'player'
            AND lane = 'interactive' AND sweep_id IS NULL
            AND league_history_status IN ('pending', 'observed'))
        OR (kind = 'live_refresh' AND scope = 'player'
            AND lane = 'interactive' AND sweep_id IS NULL
            AND league_history_status = 'not_applicable')
        OR (kind = 'reset_baseline' AND scope = 'player'
            AND lane = 'reset' AND sweep_id IS NOT NULL
            AND league_history_status IN ('not_applicable', 'pending', 'observed'))
        OR (kind = 'reset_settlement' AND scope = 'player'
            AND lane = 'ordinary' AND sweep_id IS NOT NULL
            AND league_history_status = 'not_applicable')
    );

-- Finished checks keep their row, so a repeated sweep cannot add another.
CREATE UNIQUE INDEX IF NOT EXISTS collector_work_one_reset_settlement
    ON collector_work (sweep_id, player_id)
    WHERE kind = 'reset_settlement';

-- The collector links each check to its boundary row, and nothing more.
GRANT SELECT (player_id, boundary_at, delayed_work_id),
      INSERT (player_id, boundary_at, sweep_id, delayed_work_id),
      UPDATE (delayed_work_id)
    ON TABLE reset_boundary_settlements TO clashlens_collector;
GRANT USAGE ON SEQUENCE reset_boundary_settlements_id_seq
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (61)
ON CONFLICT (version) DO NOTHING;
COMMIT;
