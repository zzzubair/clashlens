-- Clash Lens deployment migration 0024.
-- Observer read-only evidence for issue #92 Step 9 validation. Grants the
-- existing Python-worker role (the least-privilege operating role the
-- read-only observer uses) SELECT on exactly the evidence tables its probes
-- read. No contract-version bump: optional backward-compatible grants only.
-- No INSERT/UPDATE/DELETE, no sequence rights, no API-role grants, and no
-- automatic deletion. The retained-WAL directory listing stays superuser-only;
-- the observer measures retained WAL through its podman-exec probe instead.
BEGIN;

GRANT SELECT ON TABLE collector_transport_failures TO clashlens_python_worker;
GRANT SELECT ON TABLE processed_observation_versions TO clashlens_python_worker;
GRANT SELECT ON TABLE source_response_parses TO clashlens_python_worker;
GRANT SELECT ON TABLE archive_catalogue TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (24)
ON CONFLICT (version) DO NOTHING;
COMMIT;
