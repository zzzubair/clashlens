-- Clash Lens deployment migration 0077.
-- The collector's Monday re-check of the promotion list (0076) asks for each
-- listed player's profile without saving it. When the answer shows Legend I,
-- this queues the ordinary discovery check for that player: one saved
-- profile and one league-history request, processed like any other newly
-- seen player, so the player is tracked from that saved answer. It returns
-- true only once the player is tracked or has waiting work that still has to
-- fetch the profile; a player another job holds, whose check this week
-- already failed, or whose waiting work already has its profile returns false
-- so the re-check asks again later.
BEGIN;

CREATE FUNCTION clashlens_queue_promoted_player(tag text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE promoted_id bigint;
BEGIN
    INSERT INTO players (normalized_tag, active, eligibility_state)
    VALUES (tag, false, 'unknown')
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id INTO STRICT promoted_id FROM players WHERE normalized_tag = tag;
    PERFORM clashlens_enqueue_eligibility_profiles(ARRAY[promoted_id], clock_timestamp(), false);
    RETURN EXISTS (SELECT 1 FROM players WHERE id = promoted_id AND active)
        OR EXISTS (
            SELECT 1 FROM collector_work AS work
            WHERE work.player_id = promoted_id
              AND work.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
              AND work.status IN ('pending', 'waiting_retry')
              AND NOT EXISTS (
                  SELECT 1 FROM collector_observations AS observation
                  WHERE observation.id = work.profile_observation_id
                    AND (observation.http_status BETWEEN 200 AND 299
                         OR observation.http_status = 404)
              )
        );
END $$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_queue_promoted_player(text) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_queue_promoted_player(text)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_queue_promoted_player(text) TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (77)
ON CONFLICT (version) DO NOTHING;
COMMIT;
