-- Clash Lens deployment migration 0075.
-- Stop skipping a newly seen player just because another job saved a battle
-- or sighting naming them. Saving such a row takes a key-share lock on the
-- player, and 0068's FOR UPDATE ... SKIP LOCKED treated that as busy, so the
-- player got no check. The selection now takes FOR NO KEY UPDATE, which
-- key-share locks do not block; a player another job is updating is still
-- skipped. The worker locks its discovered players the same way before
-- calling this, so they are never skipped here. Everything else matches 0068.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_enqueue_eligibility_profiles(
    requested_player_ids bigint[], instant timestamptz, scheduled boolean
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    boundary_at timestamptz := clashlens_eligibility_week(instant);
    week_key text := to_char(boundary_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
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
          AND NOT EXISTS (
              SELECT 1 FROM collector_work AS work
              WHERE work.player_id = player.id
                AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                AND (work.status IN ('pending', 'waiting_retry')
                     OR (work.kind = 'discovery_profile'
                         AND work.coalescing_key = 'discovery-profile:' || player.id || ':' || week_key)
                     OR (work.kind <> 'discovery_profile'
                         AND work.due_at >= boundary_at
                         AND work.due_at < boundary_at + interval '7 days')
                     OR EXISTS (
                         SELECT 1 FROM collector_observations AS observation
                         WHERE observation.id = work.profile_observation_id
                           AND observation.response_completed_at >= boundary_at
                           AND observation.response_completed_at <= instant))
          )
          AND NOT EXISTS (
              SELECT 1 FROM collector_response_state AS state
              WHERE state.scope = 'player' AND state.identity_key = player.normalized_tag
                AND state.endpoint = 'profile'
                AND state.last_success_at >= boundary_at AND state.last_success_at <= instant
          )
          AND NOT EXISTS (
              SELECT 1 FROM player_profile_versions AS version
              JOIN player_profile_effects AS effect ON effect.profile_version_id = version.id
              WHERE version.player_id = player.id
                AND version.eligibility_state IN ('eligible', 'ineligible')
                AND effect.observed_at >= boundary_at AND effect.observed_at <= instant
          )
        ORDER BY player.id
        LIMIT CASE WHEN scheduled THEN 30 ELSE 500 END
        FOR NO KEY UPDATE OF player SKIP LOCKED
    LOOP
        INSERT INTO collector_work (
            kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
            profile_status, battle_log_status, league_history_status, eligibility_recheck
        ) VALUES (
            'discovery_profile', 'ordinary', 'player', candidate.id,
            candidate.normalized_tag,
            instant + CASE WHEN scheduled THEN created_count * interval '2 seconds' ELSE interval '0' END,
            'discovery-profile:' || candidate.id || ':' || week_key,
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

-- Replacing a function clears its search_path; ownership and grants stay.
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_enqueue_eligibility_profiles(bigint[],timestamptz,boolean) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (75)
ON CONFLICT (version) DO NOTHING;
COMMIT;
