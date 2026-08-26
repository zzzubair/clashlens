-- Clash Lens deployment migration 0010.
-- One coordinator owns population-wide publication for a reset boundary.
BEGIN;

CREATE TABLE IF NOT EXISTS boundary_publication_generations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL,
    generation integer NOT NULL CHECK (generation > 0),
    sweep_id bigint REFERENCES collector_reset_sweeps(id),
    ordering_rule_version text NOT NULL,
    freshness_rule_version text NOT NULL,
    expected_population_count integer NOT NULL CHECK (expected_population_count >= 0),
    expected_population_hash text NOT NULL CHECK (expected_population_hash ~ '^[0-9a-f]{64}$'),
    snapshot_state text NOT NULL DEFAULT 'pending'
        CHECK (snapshot_state IN ('pending','ready','building','published','superseded','failed')),
    army_state text NOT NULL DEFAULT 'pending'
        CHECK (army_state IN ('pending','ready','building','published','superseded','failed')),
    snapshot_input_hash text CHECK (snapshot_input_hash IS NULL OR snapshot_input_hash ~ '^[0-9a-f]{64}$'),
    snapshot_id bigint REFERENCES leaderboard_snapshots(id),
    army_input_hash text CHECK (army_input_hash IS NULL OR army_input_hash ~ '^[0-9a-f]{64}$'),
    supersedes_id bigint REFERENCES boundary_publication_generations(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (boundary_at, generation)
);
CREATE INDEX IF NOT EXISTS boundary_publication_generations_ready
    ON boundary_publication_generations (boundary_at, snapshot_state, army_state);

-- This is an audit mapping, not a second population source. The expected
-- members are copied from collector_reset_sweep_members when a generation is
-- created and then remain frozen for that generation.
CREATE TABLE IF NOT EXISTS boundary_publication_generation_members (
    generation_id bigint NOT NULL REFERENCES boundary_publication_generations(id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players(id),
    ranked_day_version_id bigint REFERENCES ranked_day_versions(id),
    ranked_day_input_hash text CHECK (ranked_day_input_hash IS NULL OR ranked_day_input_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','terminal','unavailable')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (generation_id, player_id)
);
CREATE INDEX IF NOT EXISTS boundary_publication_generation_members_status
    ON boundary_publication_generation_members (generation_id, status);

-- #31 consumes this after the frozen publication is complete. It is not a
-- readiness signal for either artifact.
CREATE TABLE IF NOT EXISTS boundary_publication_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL,
    generation integer NOT NULL,
    snapshot_id bigint NOT NULL REFERENCES leaderboard_snapshots(id),
    snapshot_input_hash text NOT NULL CHECK (snapshot_input_hash ~ '^[0-9a-f]{64}$'),
    emitted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (boundary_at, generation)
);

-- Collector admission is explicit: the scheduler blocks regular work until
-- the previous regular cycle drains, then keeps it blocked until reset work
-- drains and this row records the safe handoff.
CREATE TABLE IF NOT EXISTS collector_boundary_admission (
    boundary_at timestamptz PRIMARY KEY,
    reset_sweep_id bigint REFERENCES collector_reset_sweeps(id),
    regular_drain_complete boolean NOT NULL DEFAULT false,
    reset_drain_complete boolean NOT NULL DEFAULT false,
    safe_handoff boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (date_part('hour', boundary_at AT TIME ZONE 'UTC') = 5
       AND date_part('minute', boundary_at AT TIME ZONE 'UTC') = 0
       AND date_part('second', boundary_at AT TIME ZONE 'UTC') = 0)
);

-- New coordinator jobs carry a boundary generation. Legacy jobs remain
-- claimable for one release so deploys do not strand already-pending work.
ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v2_check;
ALTER TABLE python_processing_jobs
    ADD CONSTRAINT python_processing_jobs_input_v4_check CHECK (
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
                input_json ->> 'boundary_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
                AND (
                    (jsonb_typeof(input_json -> 'generation') = 'number'
                     AND (input_json ->> 'generation')::integer > 0)
                    OR NOT (input_json ? 'generation')
                )
            WHEN 'build_analytics' THEN
                (
                    jsonb_typeof(input_json -> 'snapshot_id') = 'number'
                    AND (input_json ->> 'snapshot_id')::bigint > 0
                    AND jsonb_typeof(input_json -> 'snapshot_version') = 'number'
                    AND (input_json ->> 'snapshot_version')::integer > 0
                    AND input_json ->> 'snapshot_input_hash' ~ '^[0-9a-f]{64}$'
                    AND jsonb_typeof(input_json -> 'source_ranked_day_version_id') = 'number'
                    AND (input_json ->> 'source_ranked_day_version_id')::bigint > 0
                )
                OR (
                    jsonb_typeof(input_json -> 'selection') = 'object'
                    AND jsonb_typeof(input_json -> 'selection' -> 'ranked_day_version_id') = 'number'
                    AND (input_json -> 'selection' ->> 'ranked_day_version_id')::bigint > 0
                )
                OR (
                    analytics_rule_version IN ('analytics-v1', 'legend-analytics-v1')
                    AND deduplication_key LIKE 'analytics:%'
                    AND jsonb_typeof(input_json -> 'snapshot_id') = 'number'
                    AND (input_json ->> 'snapshot_id')::bigint > 0
                    AND NOT (input_json ? 'snapshot_version')
                    AND NOT (input_json ? 'snapshot_input_hash')
                    AND NOT (input_json ? 'source_ranked_day_version_id')
                )
            WHEN 'build_export' THEN
                jsonb_typeof(input_json -> 'export_request_id') = 'number'
                AND (input_json ->> 'export_request_id')::bigint > 0
            WHEN 'build_army_analytics' THEN
                (
                    jsonb_typeof(input_json -> 'boundary_at') = 'string'
                    AND input_json ->> 'boundary_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00'
                    AND jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                )
                OR (
                    jsonb_typeof(input_json -> 'ranked_day_start') = 'string'
                    AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00'
                    AND jsonb_typeof(input_json -> 'official_season_id') = 'string'
                    AND input_json ->> 'official_season_id' <> ''
                )
            WHEN 'redecode_army' THEN
                (jsonb_typeof(input_json -> 'battle_id') = 'number'
                 AND (input_json ->> 'battle_id')::bigint > 0)
                OR (jsonb_typeof(input_json -> 'battle_ids') = 'array'
                    AND jsonb_array_length(input_json -> 'battle_ids') BETWEEN 1 AND 100)
            ELSE false
        END, false)
    );

GRANT SELECT, INSERT, UPDATE ON boundary_publication_generations,
    boundary_publication_generation_members, boundary_publication_events
    TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE boundary_publication_generations_id_seq,
    boundary_publication_events_id_seq TO clashlens_python_worker;
GRANT SELECT ON collector_reset_sweep_members TO clashlens_python_worker;
GRANT SELECT ON boundary_publication_generations, boundary_publication_generation_members,
    boundary_publication_events TO clashlens_python_api;
GRANT SELECT, INSERT, UPDATE ON collector_boundary_admission TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (10)
ON CONFLICT (version) DO NOTHING;
COMMIT;
