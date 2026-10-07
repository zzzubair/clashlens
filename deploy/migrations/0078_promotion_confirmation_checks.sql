-- Clash Lens deployment migration 0078.
-- An unchanged profile answer confirms the saved profile without processing
-- (migration 0040); it also moves that player's promotion list (0076) check
-- forward, so the Monday re-check skips a player already checked since the
-- Reset. The list row is found by its tag and changes only while it still
-- shows the confirmed profile's tier.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_confirm_checked_profile()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    confirmed_version bigint;
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
      AND player.current_profile_fingerprint = NEW.last_content_fingerprint
    RETURNING player.current_profile_version_id INTO confirmed_version;
    IF FOUND THEN
        UPDATE promotion_candidates
        SET checked_at = NEW.last_success_at
        WHERE normalized_tag = NEW.identity_key
          AND checked_at < NEW.last_success_at
          AND league_tier_id = (
              SELECT league_tier_id FROM player_profile_versions
              WHERE id = confirmed_version
          );
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_confirm_checked_profile() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (78)
ON CONFLICT (version) DO NOTHING;
COMMIT;
