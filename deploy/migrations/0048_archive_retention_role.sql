-- Raw-response cleanup connects with its own NOLOGIN role; ./ops gives it a
-- login only while cleanup is enabled. It reads only what decides whether a
-- stored response may be deleted and changes only that response's deletion
-- state. Row locks on observations and jobs run through one owner-run
-- function, so the role holds no other write access.
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clashlens_archive_retention') THEN
        CREATE ROLE clashlens_archive_retention NOLOGIN;
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO clashlens_archive_retention', current_schema());
END
$$;
ALTER ROLE clashlens_archive_retention
    NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

GRANT SELECT ON TABLE clash_lens_contract, archive_instances TO clashlens_archive_retention;
GRANT SELECT (response_hash, archive_reference, byte_size, archive_instance_id,
              availability, retire_after, retiring_since),
      UPDATE (availability, retiring_since)
    ON TABLE archive_catalogue TO clashlens_archive_retention;
GRANT SELECT (response_hash, state) ON TABLE collector_response_uploads
    TO clashlens_archive_retention;
GRANT SELECT (id, archive_reference) ON TABLE collector_observations
    TO clashlens_archive_retention;
GRANT SELECT (id, observation_id, replay_observation_id, status)
    ON TABLE python_processing_jobs TO clashlens_archive_retention;

-- Lock every observation of one stored response and every job that reads it,
-- so no new replay or job change can start while cleanup decides.
CREATE OR REPLACE FUNCTION clashlens_lock_archive_reference_work(target_reference text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    PERFORM 1 FROM collector_observations
    WHERE archive_reference = target_reference ORDER BY id FOR UPDATE;
    PERFORM 1 FROM python_processing_jobs AS p
    JOIN collector_observations AS o
      ON o.id = COALESCE(p.observation_id, p.replay_observation_id)
    WHERE o.archive_reference = target_reference ORDER BY p.id FOR UPDATE OF p;
END
$$;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_lock_archive_reference_work(text) SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;
REVOKE ALL ON FUNCTION clashlens_lock_archive_reference_work(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clashlens_lock_archive_reference_work(text)
    TO clashlens_archive_retention;

INSERT INTO clash_lens_schema_migrations(version) VALUES (48)
ON CONFLICT (version) DO NOTHING;
COMMIT;
