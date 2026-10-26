-- Clash Lens deployment migration 0130.
-- Each distinct army's composition and group (army_types.composition_keys)
-- saved with its type labels as two 8-byte numbers, so opening a variant on
-- the Armies page counts its armies without reading them: reading and
-- grouping every army took 2-5 s for one variant at the top 10,000 on
-- 26 Oct 2026.
--
-- Adding columns with no default writes no row; it holds the type labels'
-- write lock briefly. Rows read NULL until the worker relabels them under
-- army-types-v6, which the Armies page waits for, as it does for any rule
-- change: about 322,000 distinct armies, 50,000 a run, a few minutes after
-- deploy, never between 04:30 and 07:00 UTC. The two columns add about
-- 16 bytes a row: about 5 MB now, and about 16 bytes for each new distinct
-- army after.
BEGIN;

ALTER TABLE army_package_types
    ADD COLUMN IF NOT EXISTS composition_key bigint,
    ADD COLUMN IF NOT EXISTS group_key bigint;

INSERT INTO clash_lens_schema_migrations(version) VALUES (130)
ON CONFLICT (version) DO NOTHING;
COMMIT;
