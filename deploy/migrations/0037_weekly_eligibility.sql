BEGIN;

ALTER TABLE collector_work
    ADD COLUMN eligibility_recheck boolean NOT NULL DEFAULT false;

-- Weekly profiles do not refetch league history that was already collected.
DO $$
DECLARE constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'collector_work'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%discovery_profile%'
          AND pg_get_constraintdef(oid) LIKE '%battle_log_status%'
    LOOP
        EXECUTE format('ALTER TABLE collector_work DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;
ALTER TABLE collector_work ADD CONSTRAINT collector_work_endpoint_contract CHECK (
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
);
ALTER TABLE collector_work ADD CONSTRAINT collector_work_eligibility_kind CHECK (
    NOT eligibility_recheck OR kind = 'discovery_profile'
);
CREATE INDEX collector_work_player_eligibility
    ON collector_work (player_id, due_at DESC)
    WHERE kind IN ('discovery_profile', 'initial_collection', 'live_refresh');
CREATE INDEX collector_work_weekly_pending
    ON collector_work (due_at, id)
    WHERE eligibility_recheck AND status IN ('pending', 'waiting_retry');

CREATE FUNCTION clashlens_eligibility_week(instant timestamptz)
RETURNS timestamptz LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN date_bin(interval '7 days', instant, timestamptz '2000-01-03 05:00:00+00');

CREATE FUNCTION clashlens_eligibility_checked_since(
    requested_player_id bigint, boundary_at timestamptz, instant timestamptz
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT EXISTS (
        SELECT 1 FROM player_profile_versions AS version
        JOIN player_profile_effects AS effect ON effect.profile_version_id = version.id
        WHERE version.player_id = requested_player_id
          AND version.eligibility_state IN ('eligible', 'ineligible')
          AND effect.observed_at >= boundary_at AND effect.observed_at <= instant
    ) OR EXISTS (
        -- Unchanged ordinary profiles retain the old observation but advance
        -- last_success_at. Reuse only an observation actually parsed as a
        -- recognized tier, including a separately reported season conflict.
        SELECT 1 FROM players AS player
        JOIN collector_response_state AS state
          ON state.scope = 'player' AND state.identity_key = player.normalized_tag
         AND state.endpoint = 'profile'
        JOIN player_profile_effects AS effect ON effect.observation_id = state.last_observation_id
        JOIN player_profile_versions AS version ON version.id = effect.profile_version_id
        WHERE player.id = requested_player_id
          AND state.last_success_at >= boundary_at AND state.last_success_at <= instant
          AND state.last_seen_at = state.last_success_at
          AND version.eligibility_state IN ('eligible', 'ineligible')
    );
$$;

-- An old profile replay must not cancel Monday's still-unfetched check.
CREATE OR REPLACE FUNCTION clashlens_cancel_inactive_discovery_work(requested_player_id bigint)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE cancelled_count integer;
BEGIN
    IF requested_player_id IS NULL OR requested_player_id <= 0 THEN
        RAISE EXCEPTION 'player ID must be positive' USING ERRCODE = '22023';
    END IF;
    UPDATE collector_work AS work
    SET status = 'cancelled', updated_at = clock_timestamp()
    FROM players AS player
    WHERE player.id = requested_player_id AND player.id = work.player_id
      AND NOT player.active AND player.eligibility_state = 'ineligible'
      AND work.kind = 'discovery_profile' AND work.lane = 'ordinary'
      AND work.status IN ('pending', 'waiting_retry')
      AND clashlens_eligibility_checked_since(
          player.id, clashlens_eligibility_week(work.due_at), clock_timestamp());
    GET DIAGNOSTICS cancelled_count = ROW_COUNT;
    RETURN cancelled_count;
END $$;

CREATE FUNCTION clashlens_enqueue_eligibility_profiles(
    requested_player_ids bigint[], instant timestamptz, scheduled boolean
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    boundary_at timestamptz := clashlens_eligibility_week(instant);
    candidate record;
    created_count integer := 0;
    inserted_count integer;
BEGIN
    IF instant IS NULL OR scheduled IS NULL
       OR (NOT scheduled AND requested_player_ids IS NULL)
       OR (scheduled AND requested_player_ids IS NOT NULL)
       OR cardinality(requested_player_ids) > 500
       OR EXISTS (SELECT 1 FROM unnest(requested_player_ids) AS id WHERE id IS NULL OR id <= 0)
    THEN
        RAISE EXCEPTION 'invalid eligibility request' USING ERRCODE = '22023';
    END IF;
    IF EXISTS (
        SELECT 1 FROM unnest(requested_player_ids) AS requested(id)
        LEFT JOIN players AS player ON player.id = requested.id WHERE player.id IS NULL
    ) THEN
        RAISE EXCEPTION 'player ID does not exist' USING ERRCODE = '22023';
    END IF;
    -- Keep the queue bounded across restart and while live collection is late.
    IF scheduled AND EXISTS (
        SELECT 1 FROM collector_work WHERE eligibility_recheck
          AND status IN ('pending', 'waiting_retry')
    ) THEN RETURN 0; END IF;

    FOR candidate IN
        SELECT player.id, player.normalized_tag
        FROM players AS player
        WHERE (NOT player.active OR (NOT scheduled AND player.eligibility_state <> 'eligible'))
          AND (scheduled OR player.id = ANY(requested_player_ids))
          AND NOT clashlens_eligibility_checked_since(player.id, boundary_at, instant)
          AND NOT EXISTS (
              SELECT 1 FROM collector_response_state AS state
              WHERE state.scope = 'player' AND state.identity_key = player.normalized_tag
                AND state.endpoint = 'profile' AND state.last_observation_id IS NOT NULL
                AND state.last_success_at >= boundary_at AND state.last_success_at <= instant
                AND state.last_seen_at = state.last_success_at
          )
          AND NOT EXISTS (
              SELECT 1 FROM collector_work AS work
              WHERE work.player_id = player.id
                AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                AND (work.status IN ('pending', 'waiting_retry')
                     OR (work.due_at >= boundary_at
                         AND work.due_at < boundary_at + interval '7 days')
                     OR EXISTS (
                         SELECT 1 FROM collector_observations AS observation
                         WHERE observation.id = work.profile_observation_id
                           AND observation.response_completed_at >= boundary_at
                           AND observation.response_completed_at <= instant))
          )
        ORDER BY player.id
        LIMIT CASE WHEN scheduled THEN 30 ELSE 500 END
        FOR UPDATE OF player SKIP LOCKED
    LOOP
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
            profile_status, battle_log_status, league_history_status, eligibility_recheck
        ) VALUES (
            'discovery_profile', 'ordinary', 'player', candidate.id,
            candidate.normalized_tag,
            instant + CASE WHEN scheduled THEN created_count * interval '2 seconds' ELSE interval '0' END,
            'discovery-profile:' || candidate.id || ':' ||
                to_char(boundary_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'pending', 'not_applicable',
            CASE WHEN scheduled AND EXISTS (
                SELECT 1 FROM collector_response_state
                WHERE scope = 'player' AND identity_key = candidate.normalized_tag
                  AND endpoint = 'league_history'
                  AND last_success_at IS NOT NULL
            ) THEN 'not_applicable' ELSE 'pending' END,
            scheduled
        ) ON CONFLICT DO NOTHING;
        GET DIAGNOSTICS inserted_count = ROW_COUNT;
        created_count := created_count + inserted_count;
    END LOOP;
    RETURN created_count;
END $$;

CREATE OR REPLACE FUNCTION clashlens_enqueue_discovery_profiles(requested_player_ids bigint[])
RETURNS integer LANGUAGE sql SECURITY DEFINER
AS $$
    SELECT clashlens_enqueue_eligibility_profiles(requested_player_ids, clock_timestamp(), false);
$$;

CREATE FUNCTION clashlens_enqueue_weekly_eligibility(instant timestamptz)
RETURNS integer LANGUAGE sql SECURITY DEFINER
AS $$
    SELECT clashlens_enqueue_eligibility_profiles(NULL, instant, true);
$$;

DO $$
DECLARE signature text;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'clashlens_eligibility_week(timestamptz)',
        'clashlens_eligibility_checked_since(bigint,timestamptz,timestamptz)',
        'clashlens_cancel_inactive_discovery_work(bigint)',
        'clashlens_enqueue_eligibility_profiles(bigint[],timestamptz,boolean)',
        'clashlens_enqueue_discovery_profiles(bigint[])',
        'clashlens_enqueue_weekly_eligibility(timestamptz)'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%s SET search_path TO pg_catalog, %I, pg_temp',
                       current_schema(), signature, current_schema());
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION clashlens_eligibility_week(timestamptz),
    clashlens_eligibility_checked_since(bigint,timestamptz,timestamptz),
    clashlens_enqueue_eligibility_profiles(bigint[],timestamptz,boolean),
    clashlens_enqueue_discovery_profiles(bigint[]),
    clashlens_enqueue_weekly_eligibility(timestamptz)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_enqueue_discovery_profiles(bigint[])
    TO clashlens_python_worker;
GRANT EXECUTE ON FUNCTION clashlens_eligibility_week(timestamptz),
    clashlens_eligibility_checked_since(bigint,timestamptz,timestamptz),
    clashlens_enqueue_weekly_eligibility(timestamptz)
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (37)
ON CONFLICT (version) DO NOTHING;
COMMIT;
