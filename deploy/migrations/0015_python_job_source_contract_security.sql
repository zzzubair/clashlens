-- Clash Lens deployment migration 0015.
-- Keep the Python job source-contract trigger migration-owned and least-privilege.
BEGIN;

DO $$
DECLARE
    runtime_schema_name text := current_schema();
BEGIN
    IF to_regprocedure(
        format('%I.clashlens_set_python_job_source_contract()', runtime_schema_name)
    ) IS NOT NULL THEN
        EXECUTE format(
            'ALTER FUNCTION %I.clashlens_set_python_job_source_contract() SECURITY DEFINER',
            runtime_schema_name
        );
        EXECUTE format(
            'ALTER FUNCTION %I.clashlens_set_python_job_source_contract() SET search_path TO pg_catalog, %I',
            runtime_schema_name,
            runtime_schema_name
        );
        EXECUTE format(
            'REVOKE ALL ON FUNCTION %I.clashlens_set_python_job_source_contract() FROM PUBLIC',
            runtime_schema_name
        );
    END IF;
END
$$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (15)
ON CONFLICT (version) DO NOTHING;
COMMIT;
