-- Complete the bounded official player-discovery loop.
BEGIN;

LOCK TABLE collector_jobs IN ACCESS EXCLUSIVE MODE;

ALTER TABLE collector_jobs
    DROP CONSTRAINT IF EXISTS collector_jobs_work_type_v2_check,
    DROP CONSTRAINT IF EXISTS collector_jobs_work_scope_v2_check;
ALTER TABLE collector_jobs
    ADD CONSTRAINT collector_jobs_work_type_v2_check CHECK (work_type IN (
        'regular_poll', 'initial_collection', 'live_refresh',
        'legacy_reset_profile', 'legacy_unresolved_reset', 'reset_baseline',
        'global_player_rankings', 'discovery_profile', 'endpoint_retry'
    )),
    ADD CONSTRAINT collector_jobs_work_scope_v2_check CHECK (
        (work_type = 'global_player_rankings'
            AND scope = 'global' AND capacity_pool = 'normal'
            AND required_endpoint = 'global_player_rankings'
            AND sweep_id IS NULL AND reset_baseline_sweep_id IS NULL)
        OR (work_type = 'discovery_profile'
            AND scope = 'player' AND capacity_pool = 'normal'
            AND required_endpoint = 'profile'
            AND sweep_id IS NULL AND reset_baseline_sweep_id IS NULL)
        OR (work_type NOT IN ('global_player_rankings', 'discovery_profile') AND (
            work_type = 'endpoint_retry' OR scope = 'player'
        ))
    );

DROP INDEX IF EXISTS known_player_discoveries_player_source;

DROP INDEX IF EXISTS collector_jobs_one_global_rankings_per_cycle;

-- The generated coalescing key is the immutable pre-0007 cycle identity;
-- due_at may change during lease recovery or operator repair.
CREATE TABLE IF NOT EXISTS global_rankings_intents (
    cycle_at timestamptz PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (cycle_at = date_bin(interval '5 minutes', cycle_at,
                               timestamptz '2000-01-01 00:00:00+00'))
);
REVOKE ALL ON TABLE global_rankings_intents FROM PUBLIC;
GRANT INSERT ON TABLE global_rankings_intents TO clashlens_collector;
GRANT SELECT (cycle_at) ON TABLE global_rankings_intents TO clashlens_collector;
INSERT INTO global_rankings_intents (cycle_at)
SELECT DISTINCT substring(coalescing_key FROM length('global-player-rankings:') + 1)::timestamptz
FROM collector_jobs
WHERE work_type = 'global_player_rankings'
  AND coalescing_key ~ '^global-player-rankings:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z$'
  AND substring(coalescing_key FROM length('global-player-rankings:') + 1)::timestamptz
      = date_bin(
          interval '5 minutes',
          substring(coalescing_key FROM length('global-player-rankings:') + 1)::timestamptz,
          timestamptz '2000-01-01 00:00:00+00'
      )
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS discovery_profile_intents (
    player_id bigint NOT NULL REFERENCES players (id),
    cycle_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (player_id, cycle_at),
    CHECK (cycle_at = date_trunc('minute', cycle_at)
           AND extract(minute FROM cycle_at)::integer % 5 = 0
           AND extract(second FROM cycle_at) = 0)
);
REVOKE ALL ON TABLE discovery_profile_intents FROM PUBLIC;
-- Daily selection joins only this Python-owned immutable selector metadata.
REVOKE SELECT ON TABLE ranked_day_versions FROM clashlens_python_api;
GRANT SELECT (id, ranked_day_end, official_season_id, season_day_number)
    ON TABLE ranked_day_versions TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_enqueue_discovery_profiles(requested_player_ids bigint[])
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    cycle_start timestamptz := date_bin(
        interval '5 minutes', clock_timestamp(), timestamptz '2000-01-01 00:00:00+00'
    );
    created_count integer;
BEGIN
    IF requested_player_ids IS NULL
       OR cardinality(requested_player_ids) > 500
       OR EXISTS (SELECT 1 FROM unnest(requested_player_ids) AS player_id WHERE player_id IS NULL OR player_id <= 0)
    THEN
        RAISE EXCEPTION 'player IDs must be a bounded array of positive values' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(requested_player_ids) AS requested(player_id)
        LEFT JOIN players AS player ON player.id = requested.player_id
        WHERE player.id IS NULL
    ) THEN
        RAISE EXCEPTION 'player ID does not exist' USING ERRCODE = '22023';
    END IF;

    WITH requested AS (
        SELECT DISTINCT player_id
        FROM unnest(requested_player_ids) AS player_id
    ), eligible AS (
        SELECT player.id, player.normalized_tag
        FROM requested
        JOIN players AS player ON player.id = requested.player_id
        WHERE NOT (player.active AND player.eligibility_state = 'eligible')
    ), intents AS (
        INSERT INTO discovery_profile_intents (player_id, cycle_at)
        SELECT id, cycle_start FROM eligible
        ON CONFLICT DO NOTHING
        RETURNING player_id
    ), jobs AS (
        INSERT INTO collector_jobs (
            work_type, scope, player_id, normalized_tag, capacity_pool,
            priority, due_at, coalescing_key, required_endpoint, status
        )
        SELECT 'discovery_profile', 'player', player.id, player.normalized_tag,
               'normal', 300, clock_timestamp(), 'discovery-profile:' || player.id,
               'profile', 'pending'
        FROM intents JOIN players AS player ON player.id = intents.player_id
        ON CONFLICT DO NOTHING
        RETURNING 1
    )
    SELECT count(*) INTO created_count FROM jobs;
    RETURN created_count;
END
$$;

DO $$
DECLARE runtime_schema_name text := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_enqueue_discovery_profiles(bigint[]) SET search_path TO pg_catalog, %I, pg_temp',
        runtime_schema_name, runtime_schema_name
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_enqueue_discovery_profiles(bigint[])
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_enqueue_discovery_profiles(bigint[])
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations (version) VALUES (7)
ON CONFLICT (version) DO NOTHING;
COMMIT;
