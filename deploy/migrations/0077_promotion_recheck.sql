-- Clash Lens deployment migration 0077.
-- The collector's Monday re-check of the promotion list (0076) asks for each
-- listed player's profile without saving it. When the answer shows Legend I,
-- this queues the ordinary discovery check for that player: one saved
-- profile and one league-history request, processed like any other newly
-- seen player, so the player is tracked from that saved answer.
BEGIN;

CREATE FUNCTION clashlens_queue_promoted_player(tag text)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
AS $$
DECLARE promoted_id bigint;
BEGIN
    INSERT INTO players (normalized_tag, active, eligibility_state)
    VALUES (tag, false, 'unknown')
    ON CONFLICT (normalized_tag) DO NOTHING;
    SELECT id INTO STRICT promoted_id FROM players WHERE normalized_tag = tag;
    RETURN clashlens_enqueue_eligibility_profiles(ARRAY[promoted_id], clock_timestamp(), false);
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
