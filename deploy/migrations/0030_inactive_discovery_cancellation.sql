-- Stop ordinary discovery once a trusted profile proves a player ineligible,
-- while retaining unknown newcomers and permitting later fresh discovery.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_cancel_inactive_discovery_work(
    requested_player_id bigint
)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    cancelled_count integer;
BEGIN
    IF requested_player_id IS NULL OR requested_player_id <= 0 THEN
        RAISE EXCEPTION 'player ID must be positive' USING ERRCODE = '22023';
    END IF;

    UPDATE collector_work AS work
    SET status = 'cancelled', updated_at = clock_timestamp()
    FROM players AS player
    WHERE player.id = requested_player_id
      AND player.id = work.player_id
      AND player.active = false
      AND player.eligibility_state = 'ineligible'
      AND work.kind = 'discovery_profile'
      AND work.lane = 'ordinary'
      AND work.status IN ('pending', 'waiting_retry');
    GET DIAGNOSTICS cancelled_count = ROW_COUNT;
    RETURN cancelled_count;
END
$$;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_cancel_inactive_discovery_work(bigint) SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_cancel_inactive_discovery_work(bigint)
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_cancel_inactive_discovery_work(bigint)
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (30)
ON CONFLICT (version) DO NOTHING;
COMMIT;
