-- Clash Lens deployment migration 0072.
-- One league-history request per tracked player after a Season ends. The
-- Season's official results (final placement and trophies) appear there
-- minutes after the Reset (about 05:13 UTC on 5 October 2026), after the
-- Reset pair has already asked. The Season-opening Reset schedules one per
-- frozen member, due 20 minutes after it; an operator command can schedule
-- the same for every tracked player. The coalescing key names the ended
-- Season, so each player gets at most one per Season. About 13,000 rows
-- every 28 days at October 2026 membership; finished rows are kept as the
-- work reference that protects their responses.
BEGIN;

ALTER TABLE collector_work
    DROP CONSTRAINT collector_work_kind_check,
    ADD CONSTRAINT collector_work_kind_check CHECK (kind IN (
        'initial_collection', 'live_refresh', 'reset_baseline',
        'discovery_profile', 'global_player_rankings', 'reset_settlement',
        'league_history_refresh'
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
        OR (kind = 'league_history_refresh' AND scope = 'player'
            AND lane = 'ordinary' AND sweep_id IS NULL
            AND profile_status = 'not_applicable'
            AND battle_log_status = 'not_applicable'
            AND league_history_status IN ('pending', 'observed'))
    );

-- Finished refreshes keep their row, so scheduling again adds nothing.
CREATE UNIQUE INDEX IF NOT EXISTS collector_work_one_league_history_refresh
    ON collector_work (coalescing_key)
    WHERE kind = 'league_history_refresh';

INSERT INTO clash_lens_schema_migrations(version) VALUES (72)
ON CONFLICT (version) DO NOTHING;
COMMIT;
