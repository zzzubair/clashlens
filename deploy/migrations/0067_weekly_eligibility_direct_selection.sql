-- Clash Lens deployment migration 0067.
-- Pick the players due a weekly eligibility check directly, and keep a
-- week's check retrying after a temporary failure.
-- 0037's search called two per-player helper functions on every inactive
-- player before its cheap "already has this week's work" lookup. Postgres
-- cannot see inside those functions, so after a week's checks finished it
-- still ran them on everyone, once a minute: on production on 2026-10-04,
-- with 11,557 inactive players, one search ran past 5 seconds.
--
-- The same exclusions are now written as plain lookups the planner can
-- order itself, so finished players drop out at the cheap work lookup.
-- On a local database seeded like production (13,263 active players,
-- 408,292 profile versions, a finished week), one search took 2.3-2.5 s
-- before and 23 ms after with 11,557 inactive players, and 11-12 s before
-- and under 90 ms after with 50,000. No new table or index is needed.
-- The selection matches 0037 except where noted:
-- * a successful profile fetched since Monday's Reset (0037's
--   clashlens_eligibility_fetched_since) still skips the player, even while
--   processing is pending or after a later failure;
-- * a recognized profile observed since the Reset still skips the player.
--   0037's clashlens_eligibility_checked_since also accepted an unchanged
--   fetch since the Reset; that case is already a successful fetch since the
--   Reset, so the first lookup covers it;
-- * pending work from older weeks and work whose profile arrived this week
--   still skip the player, so late arrivals and players who turn inactive
--   mid-week are still picked up;
-- * this week's work still skips the player. For initial and refresh work
--   that is still work due this week. Discovery work now counts for the week
--   in its fixed key ('discovery-profile:<player id>:<week>'), not by its due
--   time: a retry moves the due time, so a check started just before
--   Monday's Reset and retried after it no longer stands in for the new
--   week's check. With unmoved due times the selection is 0037's.
-- Order, batch sizes, locking, pacing and the work rows created are as in
-- 0037. The helper functions stay; admission and cancellation still use them.
--
-- Discovery work now retries league history after a failed answer. 0037's
-- admission cancelled such work once its profile was recognized whenever
-- history was marked observed, even when that answer was an error, and its
-- processing cancellation (clashlens_cancel_inactive_discovery_work) did so
-- once the profile was ineligible, even before history was fetched. Both now
-- count history as finished only after a successful or not-found answer, so
-- the retry still fetches it.
--
-- An unchanged profile answer for discovery work normally reuses the saved
-- observation. clashlens_profile_observation_unrecognized lets the collector
-- refuse that reuse when the saved observation was processed without a
-- recognized league, so the answer is saved and processed again with the
-- current list of league tiers.
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
        FOR UPDATE OF player SKIP LOCKED
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

-- History counts as finished only after a successful or not-found answer.
CREATE OR REPLACE FUNCTION clashlens_admit_discovery_profiles(instant timestamptz)
RETURNS void LANGUAGE sql SECURITY DEFINER
AS $$
    UPDATE collector_work AS work
    SET status = 'cancelled', updated_at = clock_timestamp()
    FROM players AS player
    WHERE work.player_id = player.id AND work.kind = 'discovery_profile'
      AND work.status IN ('pending', 'waiting_retry')
      AND (work.league_history_status = 'not_applicable' OR EXISTS (
          SELECT 1 FROM collector_observations AS history
          WHERE history.id = work.league_history_observation_id
            AND (history.http_status BETWEEN 200 AND 299 OR history.http_status = 404)))
      AND ((work.eligibility_recheck AND player.active) OR clashlens_eligibility_checked_since(
          player.id, clashlens_eligibility_week(instant), instant));

    UPDATE collector_work AS work
    SET profile_status = 'observed', profile_observation_id = fresh.id,
        status = CASE WHEN work.league_history_status = 'not_applicable' OR EXISTS (
                          SELECT 1 FROM collector_observations AS history
                          WHERE history.id = work.league_history_observation_id
                            AND (history.http_status BETWEEN 200 AND 299
                                 OR history.http_status = 404))
                      THEN 'cancelled' ELSE work.status END,
        updated_at = clock_timestamp()
    FROM players AS player
    CROSS JOIN LATERAL (
        SELECT observation.id FROM collector_observations AS observation
        WHERE observation.player_id = player.id AND observation.endpoint = 'profile'
          AND observation.http_status BETWEEN 200 AND 299
          AND observation.response_completed_at <= instant
        ORDER BY observation.response_completed_at DESC, observation.id DESC LIMIT 1
    ) AS fresh
    WHERE work.player_id = player.id AND work.kind = 'discovery_profile'
      AND work.status IN ('pending', 'waiting_retry')
      AND (clashlens_eligibility_fetched_since(
          player.id, clashlens_eligibility_week(instant), instant)
          OR clashlens_eligibility_checked_since(
              player.id, clashlens_eligibility_week(instant), instant))
      AND NOT EXISTS (
          SELECT 1 FROM collector_observations AS observation
          WHERE observation.id = work.profile_observation_id
            AND (observation.http_status BETWEEN 200 AND 299 OR observation.http_status = 404)
      );
$$;

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
      AND (work.league_history_status = 'not_applicable' OR EXISTS (
          SELECT 1 FROM collector_observations AS history
          WHERE history.id = work.league_history_observation_id
            AND (history.http_status BETWEEN 200 AND 299 OR history.http_status = 404)))
      AND clashlens_eligibility_checked_since(
          player.id, clashlens_eligibility_week(work.due_at), clock_timestamp());
    GET DIAGNOSTICS cancelled_count = ROW_COUNT;
    RETURN cancelled_count;
END $$;

-- True when a saved profile was processed but never as a recognized league.
CREATE FUNCTION clashlens_profile_observation_unrecognized(requested_observation_id bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT EXISTS (
        SELECT 1 FROM player_profile_effects AS effect
        WHERE effect.observation_id = requested_observation_id
    ) AND NOT EXISTS (
        SELECT 1 FROM player_profile_effects AS effect
        JOIN player_profile_versions AS version ON version.id = effect.profile_version_id
        WHERE effect.observation_id = requested_observation_id
          AND version.eligibility_state IN ('eligible', 'ineligible')
    );
$$;

-- Replacing a function clears its search_path; ownership and grants stay.
DO $$
DECLARE signature text;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'clashlens_enqueue_eligibility_profiles(bigint[],timestamptz,boolean)',
        'clashlens_admit_discovery_profiles(timestamptz)',
        'clashlens_cancel_inactive_discovery_work(bigint)',
        'clashlens_profile_observation_unrecognized(bigint)'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%s SET search_path TO pg_catalog, %I, pg_temp',
                       current_schema(), signature, current_schema());
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION clashlens_profile_observation_unrecognized(bigint)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_profile_observation_unrecognized(bigint)
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (67)
ON CONFLICT (version) DO NOTHING;
COMMIT;
