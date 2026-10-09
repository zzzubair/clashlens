-- Clash Lens deployment migration 0090.
-- Find the next players due a weekly eligibility check from an index instead
-- of testing every untracked player on each search.
--
-- 0084's weekly search ran its two per-player evidence functions on each
-- untracked player in ID order until it found 30 still due, so players already
-- checked that week were tested again on every search. On production on
-- 9 October 2026, with 9,510 of 13,215 untracked players checked, the
-- once-a-minute search took 4.5 s on average and up to 58.6 s, and three
-- searches over 60 s in a row restart the collector. Once every player is
-- checked, each search would test them all and find none.
--
-- players.weekly_eligibility_week now records the Monday Reset of the last
-- week the weekly search finished with a player: it queued the player's check,
-- or found that week's discovery check, an initial or refresh check due that
-- week, a check whose profile was answered that week, or a recognized profile
-- from that week. An index over untracked players orders them by that week,
-- then ID, so a search reads only players it has not finished with this week
-- and stops at the first finished one. A new week makes every untracked player
-- due again without changing a row.
--
-- The players chosen are 0084's. A player with a check still waiting or a
-- profile still being processed is skipped without being marked, so a later
-- search looks again, and the cheap work lookups run before the evidence
-- functions. Players are taken oldest finished week first, which is ID order
-- within a week. Batch size, pacing, locking and the work rows created are
-- 0084's, and requested (non-weekly) checks are unchanged.
--
-- The column adds 8 bytes to each of about 26,600 players and the index holds
-- only untracked ones, about 13,200. Building it takes the same brief table
-- lock as adding the column, so it is built in this transaction.
BEGIN;

ALTER TABLE players
    ADD COLUMN weekly_eligibility_week timestamptz NOT NULL
        DEFAULT timestamptz '2000-01-03 05:00:00+00';
CREATE INDEX players_weekly_eligibility_next
    ON players (weekly_eligibility_week, id) WHERE NOT active;

CREATE OR REPLACE FUNCTION clashlens_enqueue_eligibility_profiles(
    requested_player_ids bigint[], instant timestamptz, scheduled boolean
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    boundary_at timestamptz := clashlens_eligibility_week(instant);
    week_key text := to_char(boundary_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
    candidates refcursor;
    candidate record;
    finished boolean;
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

    IF scheduled THEN
        OPEN candidates FOR
            SELECT player.id, player.normalized_tag
            FROM players AS player
            WHERE NOT player.active AND player.weekly_eligibility_week < boundary_at
            ORDER BY player.weekly_eligibility_week, player.id
            FOR NO KEY UPDATE OF player SKIP LOCKED;
    ELSE
        OPEN candidates FOR
            SELECT player.id, player.normalized_tag
            FROM players AS player
            WHERE (NOT player.active OR player.eligibility_state <> 'eligible')
              AND player.id = ANY(requested_player_ids)
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
              AND NOT clashlens_eligibility_processing_since(player.id, boundary_at, instant)
              AND NOT clashlens_eligibility_checked_since(player.id, boundary_at, instant)
            ORDER BY player.id
            LIMIT 500
            FOR NO KEY UPDATE OF player SKIP LOCKED;
    END IF;
    LOOP
        FETCH candidates INTO candidate;
        EXIT WHEN NOT FOUND;
        IF scheduled THEN
            -- This week's discovery check, an initial or refresh check due this
            -- week, or a check whose profile was answered this week.
            finished := EXISTS (
                SELECT 1 FROM collector_work AS work
                WHERE work.player_id = candidate.id
                  AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                  AND ((work.kind = 'discovery_profile'
                        AND work.coalescing_key = 'discovery-profile:' || candidate.id || ':' || week_key)
                       OR (work.kind <> 'discovery_profile'
                           AND work.due_at >= boundary_at
                           AND work.due_at < boundary_at + interval '7 days')
                       OR EXISTS (
                           SELECT 1 FROM collector_observations AS observation
                           WHERE observation.id = work.profile_observation_id
                             AND observation.response_completed_at >= boundary_at
                             AND observation.response_completed_at <= instant))
            );
            IF NOT finished THEN
                -- A check still waiting or a profile still being processed:
                -- a later search looks again.
                CONTINUE WHEN EXISTS (
                    SELECT 1 FROM collector_work AS work
                    WHERE work.player_id = candidate.id
                      AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                      AND work.status IN ('pending', 'waiting_retry')
                ) OR clashlens_eligibility_processing_since(candidate.id, boundary_at, instant);
                finished := clashlens_eligibility_checked_since(candidate.id, boundary_at, instant);
            END IF;
            UPDATE players SET weekly_eligibility_week = boundary_at WHERE id = candidate.id;
            CONTINUE WHEN finished;
        END IF;
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
        IF inserted_count > 0 THEN
            UPDATE players
            SET eligibility_attempts = eligibility_attempts + 1,
                eligibility_due_at = instant
                    + CASE WHEN scheduled THEN created_count * interval '2 seconds' ELSE interval '0' END
                    + clashlens_eligibility_retry_delay(eligibility_attempts + 1)
            WHERE id = candidate.id;
        END IF;
        created_count := created_count + inserted_count;
        EXIT WHEN scheduled AND created_count = 30;
    END LOOP;
    CLOSE candidates;
    IF NOT scheduled THEN
        PERFORM clashlens_mark_eligibility_due(requested_player_ids, instant);
    END IF;
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

INSERT INTO clash_lens_schema_migrations(version) VALUES (90)
ON CONFLICT (version) DO NOTHING;
COMMIT;
