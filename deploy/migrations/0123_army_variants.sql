-- Clash Lens deployment migration 0123.
-- Army variants: each distinct army's variant inside its type, from its
-- heroes' equipment (army_types.VARIANTS), saved beside its type.
--
-- Adding a column with a constant default writes no row; it holds the type
-- labels' write lock briefly. Every saved row reads variant 0 (its type's
-- Other) until the worker relabels it under army-types-v5, which the Armies
-- page waits for, as it does for any rule change: about 230,000 distinct
-- armies, 50,000 a run, about a minute after deploy.
--
-- The Armies page reads each counted army's spells for its "usually with"
-- tags, so the API reads that one column of exact_armies.
BEGIN;

ALTER TABLE army_package_types
    ADD COLUMN IF NOT EXISTS variant_id smallint NOT NULL DEFAULT 0;

GRANT SELECT (id, spells) ON TABLE exact_armies TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (123)
ON CONFLICT (version) DO NOTHING;
COMMIT;
