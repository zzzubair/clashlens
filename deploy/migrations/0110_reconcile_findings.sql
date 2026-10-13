-- Clash Lens deployment migration 0110.
-- The nightly re-check of finished Legend days (reconcile_pass.py).
--
-- reconcile_findings: one row per problem with a player's saved day,
-- battle or Daily board row, which check found it, what it expected and
-- what was saved, the version of the day it judged, and when it was found,
-- last seen, given a recalculation, seen again after that recalculation,
-- and gone. A problem the re-check no longer finds is resolved and kept;
-- nothing deletes rows. About 200 bytes a row; on 9 October 2026 the checks
-- would have found about 1,000 to 3,000 problems at once. Each later
-- problem adds a row, so at 1,000 new problems a day the table grows about
-- 73 MB a year.
--
-- The re-check runs as its own job type, reconcile_pass: one step of a pass
-- a job, at backfill priority, claimed like a daily result calculation.
-- Older worker images do not claim it, so its jobs wait for this one.
BEGIN;

CREATE TABLE IF NOT EXISTS reconcile_findings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_name text NOT NULL CHECK (check_name IN (
        'recount', 'chain', 'ledger', 'copies', 'missing_day', 'board'
    )),
    player_id bigint NOT NULL,
    ranked_day_start timestamptz NOT NULL,
    -- The battle a copies finding is about, the Daily board a board finding
    -- is about, else 0.
    subject_id bigint NOT NULL DEFAULT 0,
    ranked_day_version_id bigint,
    expected jsonb NOT NULL DEFAULT '{}'::jsonb,
    saved jsonb NOT NULL DEFAULT '{}'::jsonb,
    found_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    fix_queued_at timestamptz,
    escalated_at timestamptz,
    resolved_at timestamptz
);

-- One open finding per problem; a problem that comes back after it was
-- resolved is a new finding.
CREATE UNIQUE INDEX IF NOT EXISTS reconcile_findings_open
    ON reconcile_findings (ranked_day_start, player_id, check_name, subject_id)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS reconcile_findings_found
    ON reconcile_findings (found_at);

REVOKE ALL ON reconcile_findings FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON reconcile_findings TO clashlens_python_worker;

ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_work_type_v2_check;
ALTER TABLE python_processing_jobs
    ADD CONSTRAINT python_processing_jobs_work_type_v2_check CHECK (work_type IN (
        'process_observation', 'replay_observation', 'reconcile_ranked_day',
        'build_snapshot', 'build_analytics', 'build_export', 'build_army_analytics',
        'redecode_army', 'reconcile_pass'
    )) NOT VALID;
ALTER TABLE python_processing_jobs
    VALIDATE CONSTRAINT python_processing_jobs_work_type_v2_check;

-- A pass step: its slot, its day, the last player before its batch and the
-- recalculations its pass has queued so far.
ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v5_check;
ALTER TABLE python_processing_jobs
    ADD CONSTRAINT python_processing_jobs_input_v5_check CHECK (
        jsonb_typeof(input_json) = 'object'
        AND COALESCE(CASE work_type
            WHEN 'process_observation' THEN input_json = '{}'::jsonb
            WHEN 'replay_observation' THEN
                jsonb_typeof(input_json -> 'replay_request_id') = 'number'
                AND (input_json ->> 'replay_request_id')::bigint > 0
            WHEN 'reconcile_ranked_day' THEN
                jsonb_typeof(input_json -> 'player_id') = 'number'
                AND (input_json ->> 'player_id')::bigint > 0
                AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
            WHEN 'build_snapshot' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'build_analytics' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'build_export' THEN
                jsonb_typeof(input_json -> 'export_request_id') = 'number'
                AND (input_json ->> 'export_request_id')::bigint > 0
            WHEN 'build_army_analytics' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'redecode_army' THEN
                (jsonb_typeof(input_json -> 'battle_id') = 'number'
                 AND (input_json ->> 'battle_id')::bigint > 0)
                OR (jsonb_typeof(input_json -> 'battle_ids') = 'array'
                    AND jsonb_array_length(input_json -> 'battle_ids') BETWEEN 1 AND 100)
            WHEN 'reconcile_pass' THEN
                input_json ->> 'slot' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}:(morning|night)$'
                AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
                AND jsonb_typeof(input_json -> 'after_player_id') = 'number'
                AND (input_json ->> 'after_player_id')::bigint >= 0
                AND jsonb_typeof(input_json -> 'queued') = 'number'
                AND (input_json ->> 'queued')::integer >= 0
            ELSE false
        END, false)
    ) NOT VALID;
ALTER TABLE python_processing_jobs
    VALIDATE CONSTRAINT python_processing_jobs_input_v5_check;

-- Migration 0060's function, with reconcile_pass claimable as
-- reconcile_ranked_day is.
CREATE OR REPLACE FUNCTION clashlens_set_python_claim_compatibility_v3()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.claim_compatibility_version := CASE
        WHEN NEW.processing_version = 'clashlens-domain-processing-v1'
         AND NEW.domain_rule_version = 'clashlens-domain-rules-v1'
         AND (
            (NEW.work_type IN ('process_observation', 'replay_observation')
                AND (
                    NEW.parser_version IN ('supercell-source-parser-v1','supercell-source-parser-v2')
                    OR NEW.parser_version = 'supercell-profile-parser-v3'
                    OR NEW.parser_version = 'supercell-league-history-parser-v1'
                    OR NEW.parser_version = 'supercell-battle-parser-v3'
                )
                AND EXISTS (
                    SELECT 1 FROM collector_observations AS observation
                    WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
                      AND (
                        (observation.endpoint = 'profile'
                            AND observation.endpoint_version = 'profile-v1'
                            AND observation.schema_version = 'profile-schema-v1'
                            AND NEW.parser_version <> 'supercell-battle-parser-v3')
                        OR (observation.endpoint = 'league_history'
                            AND observation.endpoint_version = 'league-history-v1'
                            AND observation.schema_version = 'league-history-schema-v1'
                            AND NEW.parser_version = 'supercell-league-history-parser-v1')
                        OR (NEW.parser_version NOT IN ('supercell-profile-parser-v3', 'supercell-league-history-parser-v1') AND (
                            (observation.endpoint = 'battle_log'
                                AND observation.endpoint_version = 'battle-log-v1'
                                AND observation.schema_version = 'battle-log-schema-v1')
                            OR (observation.endpoint = 'global_player_rankings'
                                AND observation.endpoint_version = 'global-player-rankings-v1'
                                AND observation.schema_version = 'global-player-rankings-schema-v1'
                                AND NEW.parser_version <> 'supercell-battle-parser-v3')
                        ))
                      )
                ))
            OR (NEW.work_type IN ('reconcile_ranked_day','build_snapshot','reconcile_pass')
                AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_analytics'
                AND NEW.analytics_rule_version = 'legend-analytics-v1'
                AND NEW.input_json ? 'snapshot_id'
                AND NEW.input_json ? 'snapshot_version'
                AND NEW.input_json ? 'snapshot_input_hash'
                AND NEW.input_json ? 'source_ranked_day_version_id'
                AND (NEW.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$'
                AND (NEW.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$'
                AND length(NEW.input_json->>'snapshot_input_hash') > 0)
            OR (NEW.work_type IN ('build_army_analytics','redecode_army')
                AND NEW.analytics_rule_version = 'army-analytics-v2')
         )
        THEN CASE
            WHEN NEW.parser_version = 'supercell-battle-parser-v3' THEN 7
            WHEN NEW.parser_version = 'supercell-league-history-parser-v1' THEN 6
            WHEN NEW.parser_version = 'supercell-profile-parser-v3' THEN 5
            WHEN NEW.work_type IN ('build_army_analytics','redecode_army') THEN 3
            WHEN NEW.parser_version = 'supercell-source-parser-v2' THEN 2
            ELSE 1
        END
        ELSE 0
    END;
    RETURN NEW;
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (110)
ON CONFLICT (version) DO NOTHING;
COMMIT;
