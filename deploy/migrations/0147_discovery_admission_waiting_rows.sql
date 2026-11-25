-- Clash Lens deployment migration 0147.
-- The collector's admission pass asks for a player's profile proof only for
-- players with a waiting discovery check.
--
-- 0084's two housekeeping updates joined waiting checks to players and asked
-- for profile proof in the same query, so PostgreSQL could apply the proof to
-- every player row before finding the few with a waiting check. From the
-- 25 November 2026 Reset production's pass ran into its 60-second limit about
-- once a minute, inside that proof, with about ten checks waiting; the whole
-- pass then rolled back and newly found players waited an hour or more.
--
-- Each update now first lists the waiting checks through the two existing
-- waiting-check indexes (ordinary and weekly), then asks for proof once for
-- each check on that list, and writes a check only if it is still waiting.
-- A check's league history and attached profile reading are looked up by
-- their own IDs, never by reading every saved reading. The proof rules and
-- the outcomes are 0084's. Nothing is stored or rewritten.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_admit_discovery_profiles(instant timestamptz)
RETURNS void LANGUAGE sql SECURITY DEFINER
AS $$
    WITH waiting AS MATERIALIZED (
        SELECT work.id, work.eligibility_recheck, player.id AS player_id, player.active,
               work.league_history_status = 'not_applicable' OR coalesce((
                   SELECT history.http_status BETWEEN 200 AND 299 OR history.http_status = 404
                   FROM collector_observations AS history
                   WHERE history.id = work.league_history_observation_id), false)
                   AS history_settled,
               GREATEST(clashlens_eligibility_week(instant), player.eligibility_answer_after)
                   AS boundary_at
        FROM (
            SELECT id, player_id, eligibility_recheck,
                   league_history_status, league_history_observation_id
            FROM collector_work
            WHERE kind = 'discovery_profile' AND NOT eligibility_recheck
              AND status IN ('pending', 'waiting_retry')
            UNION ALL
            SELECT id, player_id, eligibility_recheck,
                   league_history_status, league_history_observation_id
            FROM collector_work
            WHERE kind = 'discovery_profile' AND eligibility_recheck
              AND status IN ('pending', 'waiting_retry')
        ) AS work
        JOIN players AS player ON player.id = work.player_id
    ), proven AS MATERIALIZED (
        SELECT id FROM waiting
        WHERE history_settled
          AND ((eligibility_recheck AND active)
               OR clashlens_eligibility_checked_since(player_id, boundary_at, instant))
    )
    UPDATE collector_work AS work
    SET status = 'cancelled', updated_at = clock_timestamp()
    FROM proven
    WHERE work.id = proven.id AND work.kind = 'discovery_profile'
      AND work.status IN ('pending', 'waiting_retry')
      AND (work.league_history_status = 'not_applicable' OR coalesce((
          SELECT history.http_status BETWEEN 200 AND 299 OR history.http_status = 404
          FROM collector_observations AS history
          WHERE history.id = work.league_history_observation_id), false));

    WITH waiting AS MATERIALIZED (
        SELECT work.id, player.id AS player_id,
               coalesce((
                   SELECT observation.http_status BETWEEN 200 AND 299
                          OR observation.http_status = 404
                   FROM collector_observations AS observation
                   WHERE observation.id = work.profile_observation_id), false)
                   AS profile_settled,
               GREATEST(clashlens_eligibility_week(instant), player.eligibility_answer_after)
                   AS boundary_at
        FROM (
            SELECT id, player_id, profile_observation_id FROM collector_work
            WHERE kind = 'discovery_profile' AND NOT eligibility_recheck
              AND status IN ('pending', 'waiting_retry')
            UNION ALL
            SELECT id, player_id, profile_observation_id FROM collector_work
            WHERE kind = 'discovery_profile' AND eligibility_recheck
              AND status IN ('pending', 'waiting_retry')
        ) AS work
        JOIN players AS player ON player.id = work.player_id
    ), proven AS MATERIALIZED (
        SELECT waiting.id, fresh.id AS observation_id
        FROM waiting
        CROSS JOIN LATERAL (
            SELECT observation.id FROM collector_observations AS observation
            WHERE observation.player_id = waiting.player_id AND observation.endpoint = 'profile'
              AND observation.http_status BETWEEN 200 AND 299
              AND observation.response_completed_at <= instant
            ORDER BY observation.response_completed_at DESC, observation.id DESC LIMIT 1
        ) AS fresh
        WHERE NOT waiting.profile_settled
          AND (clashlens_eligibility_processing_since(waiting.player_id, waiting.boundary_at,
                                                      instant)
               OR clashlens_eligibility_checked_since(waiting.player_id, waiting.boundary_at,
                                                      instant))
    )
    UPDATE collector_work AS work
    SET profile_status = 'observed', profile_observation_id = proven.observation_id,
        status = CASE WHEN work.league_history_status = 'not_applicable' OR coalesce((
                          SELECT history.http_status BETWEEN 200 AND 299
                                 OR history.http_status = 404
                          FROM collector_observations AS history
                          WHERE history.id = work.league_history_observation_id), false)
                      THEN 'cancelled' ELSE work.status END,
        updated_at = clock_timestamp()
    FROM proven
    WHERE work.id = proven.id AND work.kind = 'discovery_profile'
      AND work.status IN ('pending', 'waiting_retry')
      AND NOT coalesce((
          SELECT observation.http_status BETWEEN 200 AND 299 OR observation.http_status = 404
          FROM collector_observations AS observation
          WHERE observation.id = work.profile_observation_id), false);

    SELECT clashlens_admit_due_eligibility(instant);
$$;

-- Replacing a function clears its search_path; ownership and grants stay.
DO $$
BEGIN
    EXECUTE format('ALTER FUNCTION %I.clashlens_admit_discovery_profiles(timestamptz)'
                   ' SET search_path TO pg_catalog, %I, pg_temp',
                   current_schema(), current_schema());
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (147)
ON CONFLICT (version) DO NOTHING;
COMMIT;
