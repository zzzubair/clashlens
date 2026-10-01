BEGIN;

ALTER TABLE players
    ADD COLUMN current_profile_confirmed_at timestamptz,
    ADD COLUMN current_profile_fingerprint text
        CHECK (current_profile_fingerprint ~ '^[0-9a-f]{64}$');

WITH latest_success AS (
    SELECT DISTINCT ON (player_id) player_id, id, response_completed_at
    FROM collector_observations
    WHERE endpoint = 'profile' AND http_status BETWEEN 200 AND 299
    ORDER BY player_id, response_completed_at DESC, id DESC
), retained AS (
    SELECT player.id, player.current_observed_at, checked.last_success_at,
           checked.last_content_fingerprint, checked.last_observation_id,
           successful.id AS successful_id,
           successful.response_completed_at <= player.current_observed_at
           AND (successful.id = profile.observation_id OR EXISTS (
               SELECT 1 FROM player_profile_effects AS effect
               WHERE effect.observation_id = successful.id
                 AND effect.profile_version_id = profile.id
           )) AS confirms_profile
    FROM players AS player
    JOIN player_profile_versions AS profile
      ON profile.id = player.current_profile_version_id
    LEFT JOIN latest_success AS successful ON successful.player_id = player.id
    LEFT JOIN collector_response_state AS checked
      ON checked.scope = 'player' AND checked.identity_key = player.normalized_tag
     AND checked.endpoint = 'profile'
)
UPDATE players AS player
SET current_profile_confirmed_at = GREATEST(retained.current_observed_at,
        CASE WHEN retained.confirms_profile THEN retained.last_success_at END),
    current_profile_fingerprint = CASE
        WHEN retained.confirms_profile
         AND retained.last_observation_id = retained.successful_id
        THEN retained.last_content_fingerprint END
FROM retained WHERE player.id = retained.id;

CREATE FUNCTION clashlens_retain_profile_confirmation()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    checked_at timestamptz;
BEGIN
    SELECT last_success_at INTO checked_at
    FROM collector_response_state
    WHERE scope = 'player' AND identity_key = NEW.normalized_tag
      AND endpoint = 'profile' AND last_success_at = last_seen_at
      AND NEW.current_profile_fingerprint = last_content_fingerprint;
    NEW.current_profile_confirmed_at := GREATEST(
        OLD.current_profile_confirmed_at, NEW.current_profile_confirmed_at,
        NEW.current_observed_at, checked_at
    );
    RETURN NEW;
END;
$$;

CREATE FUNCTION clashlens_confirm_checked_profile()
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
    RETURN NEW;
END;
$$;

DO $$
DECLARE
    schema_name text := current_schema();
    function_name text;
BEGIN
    FOREACH function_name IN ARRAY ARRAY[
        'clashlens_retain_profile_confirmation', 'clashlens_confirm_checked_profile'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%I() SET search_path TO pg_catalog, %I, pg_temp',
                       schema_name, function_name, schema_name);
        EXECUTE format('REVOKE ALL ON FUNCTION %I.%I() FROM PUBLIC',
                       schema_name, function_name);
    END LOOP;
END;
$$;

CREATE TRIGGER players_retain_profile_confirmation
BEFORE UPDATE OF current_profile_version_id, current_observed_at,
                 current_profile_fingerprint ON players
FOR EACH ROW EXECUTE FUNCTION clashlens_retain_profile_confirmation();

CREATE TRIGGER collector_confirm_checked_profile
AFTER INSERT OR UPDATE ON collector_response_state
FOR EACH ROW EXECUTE FUNCTION clashlens_confirm_checked_profile();

GRANT UPDATE (current_profile_fingerprint) ON players TO clashlens_python_worker;
GRANT SELECT (current_profile_confirmed_at) ON players TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (40)
ON CONFLICT (version) DO NOTHING;
COMMIT;
