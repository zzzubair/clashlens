-- Clash Lens deployment migration 0077.
-- The collector's Monday re-check of the promotion list (0076) asks for each
-- listed player's profile without saving it. When the answer shows Legend I,
-- this queues the ordinary discovery check for that player: one saved
-- profile and one league-history request, processed like any other newly
-- seen player, so the player is tracked from that saved answer. ``added`` is
-- how many checks it queued (0 or 1). ``handed`` is true only once the player
-- is tracked or has waiting work that still has to fetch the profile; a player
-- another job holds, whose check this week already failed, or whose waiting
-- work already has its profile gets false so the re-check asks again later.
BEGIN;

CREATE FUNCTION clashlens_queue_promoted_player(
    tag text, OUT handed boolean, OUT added integer
)
LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE promoted_id bigint;
BEGIN
    INSERT INTO players (normalized_tag, active, eligibility_state)
    VALUES (tag, false, 'unknown')
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id INTO STRICT promoted_id FROM players WHERE normalized_tag = tag;
    added := clashlens_enqueue_eligibility_profiles(ARRAY[promoted_id], clock_timestamp(), false);
    handed := EXISTS (SELECT 1 FROM players WHERE id = promoted_id AND active)
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

-- An unchanged profile answer confirms the saved profile without processing
-- (migration 0040); it also moves that player's promotion list check forward,
-- so the re-check skips a player already checked since the Reset.
CREATE OR REPLACE FUNCTION clashlens_confirm_checked_profile()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF NEW.scope <> 'player' OR NEW.endpoint <> 'profile'
       OR NEW.last_success_at IS DISTINCT FROM NEW.last_seen_at THEN
        RETURN NEW;
    END IF;
    PERFORM id FROM players WHERE id = NEW.player_id FOR NO KEY UPDATE;
    UPDATE players AS player
    SET current_profile_confirmed_at = GREATEST(
            player.current_profile_confirmed_at, NEW.last_success_at),
        current_profile_fingerprint = NEW.last_content_fingerprint
    WHERE player.id = NEW.player_id
      AND player.current_profile_version_id IS NOT NULL
      AND NEW.last_success_at >= COALESCE(player.current_profile_confirmed_at,
                                          player.current_observed_at)
      AND player.current_profile_fingerprint = NEW.last_content_fingerprint;
    IF FOUND THEN
        UPDATE promotion_candidates AS candidate
        SET checked_at = NEW.last_success_at
        FROM players AS player
        JOIN player_profile_versions AS version
          ON version.id = player.current_profile_version_id
        WHERE player.id = NEW.player_id
          AND candidate.normalized_tag = player.normalized_tag
          AND candidate.league_tier_id = version.league_tier_id
          AND candidate.checked_at < NEW.last_success_at;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_queue_promoted_player(text) SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_confirm_checked_profile() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_queue_promoted_player(text)
    FROM PUBLIC, clashlens_python_worker, clashlens_python_api, clashlens_collector;
GRANT EXECUTE ON FUNCTION clashlens_queue_promoted_player(text) TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (77)
ON CONFLICT (version) DO NOTHING;
COMMIT;
