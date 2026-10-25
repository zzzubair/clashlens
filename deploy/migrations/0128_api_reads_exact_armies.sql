-- Clash Lens deployment migration 0128.
-- The Analytics Armies page shows a variant's most-used armies, each with a
-- Copy army button, so the API reads saved armies' troops, spells and heroes.
-- A grant only; it locks nothing for longer than the catalog update.
BEGIN;

GRANT SELECT ON exact_armies TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (128)
ON CONFLICT (version) DO NOTHING;
COMMIT;
