-- Clash Lens deployment migration 0082.
-- One durable eligibility due state per player identity.
--
-- Until now a player named by a battle log or ranking got one profile check,
-- and only while fewer than 500 checks waited: a player seen while that queue
-- was full had no saved retry, and a check that failed (three failed runs
-- while the API answers) left the player unknown until something named them
-- again in a later week. On production on 8 October 2026, 504 players had
-- never had a profile answer after failed checks from 1-4 October, and one
-- had never been queued.
--
-- players.eligibility_due_at now records that a player still needs an
-- eligibility answer, and when to try next. Battle opponents, rankings,
-- imports and the Monday promotion re-check set it on the same row, so a
-- player named by several sources is due once. Every check created for a
-- player (discovery, weekly, import) also moves it to the next try: 5 minutes
-- after the first, doubling to at most 6 hours. On each pass the collector
-- (clashlens_admit_due_eligibility, run from clashlens_admit_discovery_profiles)
-- clears a due player who is tracked or has a recognized profile or a
-- not-found answer this week, waits for one with a check still waiting or a
-- profile fetched this week still being processed, and otherwise adds a
-- discovery check, oldest due first, while fewer than 500 wait. So a full
-- queue only delays a player, a failed check is tried again without anyone
-- naming the player again, and a profile processed without a recognized
-- league is fetched again on the next try instead of standing in for the week.
-- Saving a player as due does not depend on waiting work, and every untracked
-- player with an unfinished check, or this week's check, when this migration
-- runs is saved as due now, so a check that later fails is still tried again.
--
-- eligibility_answer_after raises that week boundary for one player: the
-- Monday re-check's unsaved answer showing Legend I needs a saved answer newer
-- than it, so an earlier lower-league answer this week, such as one read in
-- the minutes before the game applied the promotion, neither settles the
-- player nor stands in for the new check. Admission and the post-profile
-- cancellation use the same raised boundary.
--
-- players.first_battle_log_at records when a player's first successful battle
-- log arrived, for the discovery-to-first-log delay; it is set where
-- first_battle_pending is cleared, and stays empty for players logged before
-- this migration. players.battle_opponent_seen_at and
-- battle_opponent_observation_id record the first battle log naming a player
-- while untracked (filled from saved sightings now), so the report's
-- battle-opponent count survives pruning of those sightings.
--
-- About 26,600 players today; the four timestamps, the observation ID and the
-- count add 44 bytes a row, and the index holds only due players. The check
-- index below holds only waiting discovery checks, at most about 500 rows.
--
-- clashlens_population_report and clashlens_repair_population back the
-- collector-role population-status command: separate available, waiting to
-- sign up and unavailable tracked totals, the delay from a player's first
-- check to their first battle log, this week's eligibility answers for
-- battle opponents and other known players separately, the due players,
-- saved profiles still without a recognized league, and the promotion list
-- by tier. The repair marks due every
-- untracked player with no recognized answer and no not-found answer, and adds
-- to the promotion list every untracked player whose latest recognized
-- profile shows Legend II or III (0076 copied only those with an accepted
-- current profile: 360 of about 9,900 on 8 October 2026). It refetches nothing
-- itself and deletes nothing.
BEGIN;

ALTER TABLE players
    ADD COLUMN eligibility_due_at timestamptz,
    ADD COLUMN eligibility_answer_after timestamptz,
    ADD COLUMN eligibility_attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN first_battle_log_at timestamptz,
    ADD COLUMN battle_opponent_seen_at timestamptz,
    ADD COLUMN battle_opponent_observation_id bigint,
    ADD CONSTRAINT players_battle_opponent_seen CHECK (
        (battle_opponent_seen_at IS NULL) = (battle_opponent_observation_id IS NULL)
    ),
    ADD CONSTRAINT players_eligibility_due_state CHECK (
        eligibility_attempts >= 0
        AND (eligibility_due_at IS NOT NULL
             OR (eligibility_answer_after IS NULL AND eligibility_attempts = 0))
    );
CREATE INDEX players_eligibility_due
    ON players (eligibility_due_at, id) WHERE eligibility_due_at IS NOT NULL;
-- Counted on every pass that has a due player to admit.
CREATE INDEX collector_work_discovery_waiting
    ON collector_work (id)
    WHERE kind = 'discovery_profile' AND NOT eligibility_recheck
      AND status IN ('pending', 'waiting_retry');
GRANT UPDATE (first_battle_log_at) ON TABLE players TO clashlens_collector;

UPDATE players AS player
SET battle_opponent_seen_at = first.discovered_at,
    battle_opponent_observation_id = first.observation_id
FROM (
    SELECT DISTINCT ON (player_id) player_id, discovered_at, observation_id
    FROM known_player_discoveries WHERE source_kind = 'battle_opponent'
    ORDER BY player_id, discovered_at, id
) AS first
WHERE player.id = first.player_id;

-- A battle log's first sighting of an untracked player, kept on the player so
-- pruning the sighting does not move them out of the battle-opponent count.
CREATE FUNCTION clashlens_save_battle_opponent()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
AS $$
BEGIN
    UPDATE players
    SET battle_opponent_seen_at = NEW.discovered_at,
        battle_opponent_observation_id = NEW.observation_id
    WHERE id = NEW.player_id AND NOT active AND battle_opponent_seen_at IS NULL;
    RETURN NULL;
END $$;
CREATE TRIGGER known_player_discoveries_battle_opponent
AFTER INSERT ON known_player_discoveries
FOR EACH ROW WHEN (NEW.source_kind = 'battle_opponent')
EXECUTE FUNCTION clashlens_save_battle_opponent();

UPDATE players AS player SET eligibility_due_at = now()
WHERE NOT player.active AND EXISTS (
    SELECT 1 FROM collector_work AS work
    WHERE work.player_id = player.id
      AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
      AND (work.status IN ('pending', 'waiting_retry')
           OR (work.kind = 'discovery_profile'
               AND work.coalescing_key = 'discovery-profile:' || player.id || ':'
                   || to_char(clashlens_eligibility_week(now()) AT TIME ZONE 'UTC',
                              'YYYY-MM-DD"T"HH24:MI:SS"Z"')))
);

-- A recognized profile or a not-found answer within the window. The
-- recognized part is 0037's clashlens_eligibility_checked_since, found
-- through the player's profile responses in the window instead of every
-- saved profile version: a long-tracked player has thousands of those. On
-- production on 8 October 2026 both agreed for a sample of 1,346 untracked
-- players, and this took 1.7 s for all 13,341 where the old one passed 30 s.
CREATE FUNCTION clashlens_eligibility_answered_since(
    requested_player_id bigint, boundary_at timestamptz, instant timestamptz
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT EXISTS (
        SELECT 1 FROM collector_observations AS observation
        JOIN player_profile_effects AS effect ON effect.observation_id = observation.id
        JOIN player_profile_versions AS version ON version.id = effect.profile_version_id
        WHERE observation.player_id = requested_player_id
          AND observation.endpoint = 'profile'
          AND observation.response_completed_at >= boundary_at
          AND observation.response_completed_at <= instant
          AND version.eligibility_state IN ('eligible', 'ineligible')
    ) OR EXISTS (
        SELECT 1 FROM players AS player
        JOIN collector_response_state AS state
          ON state.scope = 'player' AND state.identity_key = player.normalized_tag
         AND state.endpoint = 'profile'
        WHERE player.id = requested_player_id
          AND ((state.last_not_found_at >= boundary_at AND state.last_not_found_at <= instant)
               -- An unchanged answer confirming a recognized saved profile.
               OR (state.last_success_at >= boundary_at AND state.last_success_at <= instant
                   AND state.last_seen_at = state.last_success_at
                   AND EXISTS (
                       SELECT 1 FROM player_profile_effects AS effect
                       JOIN player_profile_versions AS version
                         ON version.id = effect.profile_version_id
                       WHERE effect.observation_id = state.last_observation_id
                         AND version.eligibility_state IN ('eligible', 'ineligible'))))
    );
$$;

-- The latest recognized profile, newest profile response first.
CREATE FUNCTION clashlens_latest_recognized_tier(
    requested_player_id bigint, OUT league_tier_id bigint, OUT trophies integer,
    OUT observed_at timestamptz
)
LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT version.league_tier_id, version.trophies, observation.response_completed_at
    FROM collector_observations AS observation
    JOIN player_profile_effects AS effect ON effect.observation_id = observation.id
    JOIN player_profile_versions AS version ON version.id = effect.profile_version_id
    WHERE observation.player_id = requested_player_id AND observation.endpoint = 'profile'
      AND version.eligibility_state IN ('eligible', 'ineligible')
    ORDER BY observation.response_completed_at DESC, observation.id DESC
    LIMIT 1;
$$;

-- A successful profile fetch within the window whose answer still awaits
-- processing: the latest successful profile response, which that fetch either
-- saved or, for an unchanged answer, kept.
CREATE FUNCTION clashlens_eligibility_processing_since(
    requested_player_id bigint, boundary_at timestamptz, instant timestamptz
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    SELECT clashlens_eligibility_fetched_since(requested_player_id, boundary_at, instant)
       AND EXISTS (
        SELECT 1 FROM (
            SELECT observation.id FROM collector_observations AS observation
            WHERE observation.player_id = requested_player_id
              AND observation.endpoint = 'profile'
              AND observation.http_status BETWEEN 200 AND 299
              AND observation.response_completed_at <= instant
            ORDER BY observation.response_completed_at DESC, observation.id DESC
            LIMIT 1
        ) AS latest
        JOIN python_processing_jobs AS job ON job.observation_id = latest.id
        WHERE job.status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
    );
$$;

CREATE FUNCTION clashlens_eligibility_retry_delay(attempts integer)
RETURNS interval LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
RETURN least(
    interval '5 minutes' * power(2, least(greatest(attempts, 1), 8) - 1),
    interval '6 hours'
);

-- Save untracked players with no answer this week as due now, whether or not
-- a check is waiting. A player already due keeps its next try and backoff.
-- Returns how many became due.
CREATE FUNCTION clashlens_mark_eligibility_due(
    requested_player_ids bigint[], instant timestamptz
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE marked integer;
BEGIN
    IF requested_player_ids IS NULL OR instant IS NULL
       OR EXISTS (SELECT 1 FROM unnest(requested_player_ids) AS id WHERE id IS NULL OR id <= 0)
    THEN
        RAISE EXCEPTION 'invalid eligibility request' USING ERRCODE = '22023';
    END IF;
    WITH locked AS (
        SELECT player.id FROM players AS player
        WHERE player.id = ANY(requested_player_ids)
          AND NOT player.active AND player.eligibility_due_at IS NULL
        ORDER BY player.id
        FOR NO KEY UPDATE
    )
    UPDATE players AS player
    SET eligibility_due_at = instant
    FROM locked
    WHERE player.id = locked.id
      AND NOT player.active AND player.eligibility_due_at IS NULL
      AND NOT clashlens_eligibility_answered_since(
          player.id, clashlens_eligibility_week(instant), instant);
    GET DIAGNOSTICS marked = ROW_COUNT;
    RETURN marked;
END $$;

-- Turn due players into discovery checks while fewer than 500 wait.
CREATE FUNCTION clashlens_admit_due_eligibility(instant timestamptz)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    week_at timestamptz := clashlens_eligibility_week(instant);
    week_key text := to_char(week_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
    room integer;
    candidate record;
    boundary_at timestamptz;
    work_key text;
    suffix integer;
    inserted integer;
    added integer := 0;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM players WHERE eligibility_due_at <= instant) THEN
        RETURN 0;
    END IF;
    -- One pass at a time adds checks, so passes never exceed the limit together.
    IF NOT pg_try_advisory_xact_lock(hashtextextended('discovery-queue', 0)) THEN
        RETURN 0;
    END IF;
    SELECT 500 - count(*) INTO room FROM collector_work
    WHERE kind = 'discovery_profile' AND NOT eligibility_recheck
      AND status IN ('pending', 'waiting_retry');
    IF room <= 0 THEN
        RETURN 0;
    END IF;
    FOR candidate IN
        SELECT player.id, player.normalized_tag, player.active,
               player.eligibility_attempts, player.eligibility_answer_after
        FROM players AS player
        WHERE player.eligibility_due_at <= instant
        ORDER BY player.eligibility_due_at, player.id
        LIMIT 200
        FOR NO KEY UPDATE OF player SKIP LOCKED
    LOOP
        boundary_at := GREATEST(week_at, candidate.eligibility_answer_after);
        IF candidate.active
           OR clashlens_eligibility_answered_since(candidate.id, boundary_at, instant) THEN
            UPDATE players
            SET eligibility_due_at = NULL, eligibility_answer_after = NULL,
                eligibility_attempts = 0
            WHERE id = candidate.id;
        ELSIF EXISTS (
            SELECT 1 FROM collector_work AS work
            WHERE work.player_id = candidate.id
              AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
              AND work.status IN ('pending', 'waiting_retry')
        ) OR clashlens_eligibility_processing_since(candidate.id, boundary_at, instant) THEN
            -- A check is still waiting or this week's profile still awaits
            -- processing: try again later.
            UPDATE players
            SET eligibility_due_at = instant
                + clashlens_eligibility_retry_delay(candidate.eligibility_attempts)
            WHERE id = candidate.id;
        ELSIF room > 0 THEN
            work_key := 'discovery-profile:' || candidate.id || ':' || week_key;
            suffix := candidate.eligibility_attempts;
            -- This week's check already ran; a retry keeps its own unused key.
            WHILE EXISTS (
                SELECT 1 FROM collector_work
                WHERE kind = 'discovery_profile' AND coalescing_key = work_key
            ) LOOP
                suffix := suffix + 1;
                work_key := 'discovery-profile:' || candidate.id || ':' || week_key
                    || ':' || suffix;
            END LOOP;
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at, coalescing_key,
                profile_status, battle_log_status, league_history_status
            ) VALUES (
                'discovery_profile', 'ordinary', 'player', candidate.id,
                candidate.normalized_tag, instant, work_key,
                'pending', 'not_applicable', 'pending'
            ) ON CONFLICT DO NOTHING;
            GET DIAGNOSTICS inserted = ROW_COUNT;
            IF inserted > 0 THEN
                UPDATE players
                SET eligibility_attempts = candidate.eligibility_attempts + 1,
                    eligibility_due_at = instant
                        + clashlens_eligibility_retry_delay(candidate.eligibility_attempts + 1)
                WHERE id = candidate.id;
                room := room - 1;
                added := added + 1;
            END IF;
        END IF;
    END LOOP;
    RETURN added;
END $$;

-- As 0075, and each check created moves its player to the next try, and
-- requested players left without a check are saved as due. A profile fetched
-- this week holds a player back only while it awaits processing.
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
          AND NOT clashlens_eligibility_processing_since(player.id, boundary_at, instant)
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
        IF inserted_count > 0 THEN
            UPDATE players
            SET eligibility_attempts = eligibility_attempts + 1,
                eligibility_due_at = instant
                    + CASE WHEN scheduled THEN created_count * interval '2 seconds' ELSE interval '0' END
                    + clashlens_eligibility_retry_delay(eligibility_attempts + 1)
            WHERE id = candidate.id;
        END IF;
        created_count := created_count + inserted_count;
    END LOOP;
    IF NOT scheduled THEN
        PERFORM clashlens_mark_eligibility_due(requested_player_ids, instant);
    END IF;
    RETURN created_count;
END $$;

-- As 0068, with each player's raised answer boundary, then admit due players.
-- Waiting work reuses this week's profile only while it awaits processing or
-- once it shows a recognized league, so one processed without a recognized
-- league is fetched again.
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
          player.id,
          GREATEST(clashlens_eligibility_week(instant), player.eligibility_answer_after),
          instant));

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
      AND (clashlens_eligibility_processing_since(
          player.id,
          GREATEST(clashlens_eligibility_week(instant), player.eligibility_answer_after),
          instant)
          OR clashlens_eligibility_checked_since(
              player.id,
              GREATEST(clashlens_eligibility_week(instant), player.eligibility_answer_after),
              instant))
      AND NOT EXISTS (
          SELECT 1 FROM collector_observations AS observation
          WHERE observation.id = work.profile_observation_id
            AND (observation.http_status BETWEEN 200 AND 299 OR observation.http_status = 404)
      );

    SELECT clashlens_admit_due_eligibility(instant);
$$;

-- As 0068, with the player's raised answer boundary.
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
          player.id,
          GREATEST(clashlens_eligibility_week(work.due_at), player.eligibility_answer_after),
          clock_timestamp());
    GET DIAGNOSTICS cancelled_count = ROW_COUNT;
    RETURN cancelled_count;
END $$;

-- The Monday re-check's answer showing Legend I. ``handed`` is true once the
-- player is tracked or due a saved check newer than this answer; false only
-- while another job holds the player, so the re-check asks again later.
-- ``added`` is 1 when the player was not already due.
CREATE OR REPLACE FUNCTION clashlens_queue_promoted_player(
    tag text, OUT handed boolean, OUT added integer
)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE promoted record;
BEGIN
    INSERT INTO players (normalized_tag, active, eligibility_state)
    VALUES (tag, false, 'unknown')
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id, active, eligibility_due_at IS NOT NULL AS due INTO promoted
    FROM players WHERE normalized_tag = tag
    FOR NO KEY UPDATE SKIP LOCKED;
    added := 0;
    handed := FOUND;
    IF NOT handed THEN
        RETURN;
    END IF;
    IF promoted.active THEN
        RETURN;
    END IF;
    UPDATE players
    SET eligibility_due_at = LEAST(eligibility_due_at, clock_timestamp()),
        eligibility_answer_after = GREATEST(eligibility_answer_after, clock_timestamp())
    WHERE id = promoted.id;
    added := CASE WHEN promoted.due THEN 0 ELSE 1 END;
END $$;

-- Every untracked player still without any answer, and every untracked player
-- whose latest recognized profile shows Legend II or III but is not listed.
CREATE FUNCTION clashlens_repair_population(instant timestamptz)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE
    unanswered bigint[];
    marked integer;
    listed integer;
BEGIN
    SELECT COALESCE(array_agg(player.id ORDER BY player.id), '{}') INTO unanswered
    FROM players AS player
    WHERE NOT player.active AND player.eligibility_state IN ('unknown', 'uncertain')
      AND player.eligibility_due_at IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM collector_response_state AS state
          WHERE state.scope = 'player' AND state.identity_key = player.normalized_tag
            AND state.endpoint = 'profile' AND state.last_not_found_at IS NOT NULL
      );
    marked := clashlens_mark_eligibility_due(unanswered, instant);
    INSERT INTO promotion_candidates (normalized_tag, league_tier_id, trophies, checked_at)
    SELECT player.normalized_tag, latest.league_tier_id, latest.trophies, latest.observed_at
    FROM players AS player
    CROSS JOIN LATERAL clashlens_latest_recognized_tier(player.id) AS latest
    WHERE NOT player.active AND latest.league_tier_id IN (105000034, 105000035)
    ON CONFLICT (normalized_tag) DO NOTHING;
    GET DIAGNOSTICS listed = ROW_COUNT;
    RETURN jsonb_build_object(
        'unanswered_found', cardinality(unanswered),
        'marked_due', marked,
        'promotion_rows_added', listed
    );
END $$;

-- Read-only counts for the population-status command. ``season_id`` is the
-- current Season's official ID, as a current profile names it.
CREATE FUNCTION clashlens_population_report(instant timestamptz, season_id text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
AS $$
    WITH week AS (
        SELECT clashlens_eligibility_week(instant) AS start_at
    ), known AS MATERIALIZED (
        SELECT player.id, player.active, player.eligibility_state,
               player.eligibility_due_at, player.eligibility_attempts,
               player.eligibility_answer_after, player.first_battle_pending,
               player.battle_opponent_seen_at,
               state.last_not_found_at IS NOT NULL
                   AND (state.last_success_at IS NULL
                        OR state.last_not_found_at > state.last_success_at) AS gone,
               profile.source_contract_state = 'accepted'
                   AND profile.current_league_season_id = season_id AS season_current
        FROM players AS player
        LEFT JOIN collector_response_state AS state
          ON state.scope = 'player' AND state.identity_key = player.normalized_tag
         AND state.endpoint = 'profile'
        LEFT JOIN player_profile_versions AS profile
          ON profile.id = player.current_profile_version_id
    ), untracked AS MATERIALIZED (
        SELECT known.*,
               clashlens_eligibility_answered_since(
                   known.id, GREATEST(week.start_at, known.eligibility_answer_after), instant)
                   AS answered,
               EXISTS (
                   SELECT 1 FROM collector_work AS work
                   WHERE work.player_id = known.id
                     AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
                     AND work.status IN ('pending', 'waiting_retry')
               ) AS waiting,
               known.battle_opponent_seen_at IS NOT NULL AS opponent
        FROM known CROSS JOIN week WHERE NOT known.active
    ), first_checks AS (
        SELECT work.player_id, min(work.created_at) AS first_at
        FROM collector_work AS work
        JOIN players AS player ON player.id = work.player_id AND player.active
        WHERE work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
        GROUP BY work.player_id
        HAVING min(work.created_at) >= instant - interval '7 days'
    ), first_logs AS (
        SELECT extract(epoch FROM player.first_battle_log_at - first_checks.first_at)::double precision
                   AS seconds
        FROM first_checks JOIN players AS player ON player.id = first_checks.player_id
        -- Leaves out players whose first log came before that check or was
        -- not recorded (before this migration).
        WHERE player.first_battle_pending OR player.first_battle_log_at >= first_checks.first_at
    ), checks AS (
        SELECT work.eligibility_recheck AS weekly,
               jsonb_build_object(
                   'waiting', count(*) FILTER (WHERE work.status IN ('pending', 'waiting_retry')),
                   'complete', count(*) FILTER (WHERE work.status = 'complete'),
                   'failed', count(*) FILTER (WHERE work.status = 'failed'),
                   'cancelled', count(*) FILTER (WHERE work.status = 'cancelled'),
                   'players', count(DISTINCT work.player_id),
                   'players_now_tracked', count(DISTINCT work.player_id) FILTER (WHERE player.active)
               ) AS outcome
        FROM collector_work AS work
        JOIN players AS player ON player.id = work.player_id
        CROSS JOIN week
        WHERE work.kind = 'discovery_profile' AND work.created_at >= week.start_at
        GROUP BY work.eligibility_recheck
    ), listed AS (
        SELECT count(*) FILTER (WHERE league_tier_id = 105000035) AS legend_ii,
               count(*) FILTER (WHERE league_tier_id = 105000034) AS legend_iii,
               count(*) FILTER (WHERE league_tier_id = 105000035
                                  AND checked_at >= week.start_at + interval '1 hour')
                   AS legend_ii_checked,
               count(*) FILTER (WHERE league_tier_id = 105000034
                                  AND checked_at >= week.start_at + interval '1 hour')
                   AS legend_iii_checked
        FROM promotion_candidates CROSS JOIN week
    ), unlisted AS (
        SELECT count(*) AS total
        FROM players AS player
        CROSS JOIN LATERAL clashlens_latest_recognized_tier(player.id) AS latest
        WHERE NOT player.active AND latest.league_tier_id IN (105000034, 105000035)
          AND NOT EXISTS (
              SELECT 1 FROM promotion_candidates AS candidate
              WHERE candidate.normalized_tag = player.normalized_tag
          )
    )
    SELECT jsonb_build_object(
        'week_start', (SELECT start_at FROM week),
        'tracked', (
            SELECT jsonb_build_object(
                'total', count(*),
                'available', count(*) FILTER (WHERE NOT gone AND season_current),
                'waiting_to_sign_up', count(*) FILTER (WHERE NOT gone AND NOT COALESCE(season_current, false)),
                'unavailable', count(*) FILTER (WHERE gone),
                'first_battle_log_pending', count(*) FILTER (WHERE first_battle_pending)
            ) FROM known WHERE active
        ),
        'first_battle_log_delay', (
            SELECT jsonb_build_object(
                'players', count(*),
                'with_first_log', count(seconds),
                'median_seconds', percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds),
                'p95_seconds', percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds),
                'max_seconds', max(seconds)
            ) FROM first_logs
        ),
        'untracked', (
            SELECT jsonb_build_object(
                'total', count(*),
                'ineligible', count(*) FILTER (WHERE eligibility_state = 'ineligible'),
                'unknown', count(*) FILTER (WHERE eligibility_state <> 'ineligible'),
                'not_found', count(*) FILTER (WHERE gone)
            ) FROM untracked
        ),
        'untracked_this_week', (
            SELECT jsonb_object_agg(population.name, (
                SELECT jsonb_build_object(
                    'total', count(*),
                    'answered', count(*) FILTER (WHERE answered),
                    'check_waiting', count(*) FILTER (WHERE NOT answered AND waiting),
                    'due_for_retry', count(*) FILTER (
                        WHERE NOT answered AND NOT waiting AND eligibility_due_at IS NOT NULL),
                    'not_checked', count(*) FILTER (
                        WHERE NOT answered AND NOT waiting AND eligibility_due_at IS NULL)
                ) FROM untracked WHERE untracked.opponent = population.opponent
            ))
            FROM (VALUES ('battle_opponents', true), ('other_known', false))
                AS population(name, opponent)
        ),
        'old_classifications_remaining', (
            SELECT count(*) FROM untracked
            WHERE eligibility_state IN ('unknown', 'uncertain') AND NOT gone
              AND EXISTS (
                  SELECT 1 FROM player_profile_versions AS version
                  WHERE version.player_id = untracked.id
              )
        ),
        'checks_this_week', jsonb_build_object(
            'weekly', COALESCE((SELECT outcome FROM checks WHERE weekly), '{}'),
            'discovery', COALESCE((SELECT outcome FROM checks WHERE NOT weekly), '{}')
        ),
        'eligibility_due', (
            SELECT jsonb_build_object(
                'total', count(*),
                'due_now', count(*) FILTER (WHERE eligibility_due_at <= instant),
                'retried', count(*) FILTER (WHERE eligibility_attempts > 1),
                'oldest_due_at', min(eligibility_due_at)
            ) FROM known WHERE eligibility_due_at IS NOT NULL
        ),
        'promotion_list', (
            SELECT jsonb_build_object(
                'legend_ii', legend_ii, 'legend_iii', legend_iii,
                'legend_ii_checked_this_week', legend_ii_checked,
                'legend_iii_checked_this_week', legend_iii_checked,
                'known_but_unlisted', (SELECT total FROM unlisted)
            ) FROM listed
        ),
        'repair_candidates', (
            SELECT count(*) FROM known
            WHERE NOT active AND eligibility_state IN ('unknown', 'uncertain')
              AND eligibility_due_at IS NULL AND NOT EXISTS (
                  SELECT 1 FROM players AS player
                  JOIN collector_response_state AS state
                    ON state.scope = 'player' AND state.identity_key = player.normalized_tag
                   AND state.endpoint = 'profile' AND state.last_not_found_at IS NOT NULL
                  WHERE player.id = known.id
              )
        )
    );
$$;

-- Replacing a function clears its search_path; ownership and grants stay.
DO $$
DECLARE signature text;
BEGIN
    FOREACH signature IN ARRAY ARRAY[
        'clashlens_eligibility_answered_since(bigint,timestamptz,timestamptz)',
        'clashlens_latest_recognized_tier(bigint)',
        'clashlens_eligibility_processing_since(bigint,timestamptz,timestamptz)',
        'clashlens_save_battle_opponent()',
        'clashlens_eligibility_retry_delay(integer)',
        'clashlens_mark_eligibility_due(bigint[],timestamptz)',
        'clashlens_admit_due_eligibility(timestamptz)',
        'clashlens_enqueue_eligibility_profiles(bigint[],timestamptz,boolean)',
        'clashlens_admit_discovery_profiles(timestamptz)',
        'clashlens_cancel_inactive_discovery_work(bigint)',
        'clashlens_queue_promoted_player(text)',
        'clashlens_repair_population(timestamptz)',
        'clashlens_population_report(timestamptz,text)'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%s SET search_path TO pg_catalog, %I, pg_temp',
                       current_schema(), signature, current_schema());
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION
    clashlens_eligibility_answered_since(bigint,timestamptz,timestamptz),
    clashlens_latest_recognized_tier(bigint),
    clashlens_eligibility_processing_since(bigint,timestamptz,timestamptz),
    clashlens_save_battle_opponent(),
    clashlens_eligibility_retry_delay(integer),
    clashlens_mark_eligibility_due(bigint[],timestamptz),
    clashlens_admit_due_eligibility(timestamptz),
    clashlens_repair_population(timestamptz),
    clashlens_population_report(timestamptz,text)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_mark_eligibility_due(bigint[],timestamptz)
    TO clashlens_python_worker, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_admit_due_eligibility(timestamptz),
    clashlens_repair_population(timestamptz),
    clashlens_population_report(timestamptz,text)
    TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (82)
ON CONFLICT (version) DO NOTHING;
COMMIT;
