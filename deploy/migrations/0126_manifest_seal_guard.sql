-- Clash Lens deployment migration 0126.
-- Sealing a manifest read its whole row about a dozen times as JSON to check that
-- its identity had not changed. A full army manifest's rule_versions holds
-- its Season's inputs, about 30 MB of JSON by the Season's ninth day, so one
-- seal held about 2.8 GB of memory then, and production's connection was
-- killed for memory at 5.5 and 6.4 GB on 19 and 21 October 2026.
--
-- Its identity columns are now refused by a trigger that fires only when an
-- UPDATE names them, so no value of theirs is read, and the seal reads only
-- rows_sealed. Updates allowed and refused are the same, except that naming
-- an identity column with its own value is now refused too; nothing does.
--
-- Replacing a function and adding a trigger reads no rows; adding the trigger
-- holds the manifests' write lock briefly.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_freeze_boundary_manifest_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows'
       AND TG_OP IN ('UPDATE', 'DELETE')
       AND EXISTS (
           SELECT 1 FROM boundary_publication_manifests AS manifest
           WHERE manifest.id = (to_jsonb(OLD)->>'manifest_id')::bigint
             AND manifest.rows_sealed
       ) THEN
        RAISE EXCEPTION 'boundary publication manifest rows are immutable after sealing';
    END IF;
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows'
       AND TG_OP IN ('INSERT', 'UPDATE')
       AND EXISTS (
           SELECT 1 FROM boundary_publication_manifests AS manifest
           WHERE manifest.id = (to_jsonb(NEW)->>'manifest_id')::bigint
             AND manifest.rows_sealed
       ) THEN
        RAISE EXCEPTION 'boundary publication manifest rows are immutable after sealing';
    END IF;
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    -- boundary_publication_manifests_identity refuses an UPDATE naming an
    -- identity column, so the seal is the only change left to allow.
    IF TG_OP = 'UPDATE' THEN
        IF NOT OLD.rows_sealed AND NEW.rows_sealed THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION 'boundary publication manifests are immutable after rows are sealed';
END $$;

CREATE OR REPLACE FUNCTION clashlens_boundary_manifest_identity_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'boundary publication manifests are immutable after rows are sealed';
END $$;
REVOKE ALL ON FUNCTION clashlens_boundary_manifest_identity_guard() FROM PUBLIC;
DROP TRIGGER IF EXISTS boundary_publication_manifests_identity
    ON boundary_publication_manifests;
CREATE TRIGGER boundary_publication_manifests_identity
BEFORE UPDATE OF generation_id, artifact_kind, rule_versions, digest, created_at
ON boundary_publication_manifests
FOR EACH ROW EXECUTE FUNCTION clashlens_boundary_manifest_identity_guard();

INSERT INTO clash_lens_schema_migrations(version) VALUES (126)
ON CONFLICT (version) DO NOTHING;

COMMIT;
