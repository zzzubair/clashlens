-- Clash Lens deployment migration 0081.
-- A Reset's manifests were frozen again in full for every correction: on
-- 7 October 2026, 35 generations wrote 911,900 rows, about 2.15 GB, while
-- about 97% of each player's rows repeated an earlier one.
--
-- A new manifest can now name a full manifest of the same Reset and kind as
-- its base and store only the players whose row differs from it. A differing
-- row an earlier manifest already stored keeps only its plain columns and
-- names that manifest for its identity. boundary_publication_manifest_entries
-- returns every manifest's complete rows, each reused identity carrying the
-- reading manifest's generation, exactly as a full manifest would store them,
-- so the digest still covers the same rows. Old manifests stay full and
-- unchanged.
--
-- Adding nullable columns and unchecked constraints reads no existing rows:
-- every existing row has an identity and no source, which the constraints
-- allow.
BEGIN;

ALTER TABLE boundary_publication_manifests
    ADD COLUMN IF NOT EXISTS base_manifest_id bigint
        REFERENCES boundary_publication_manifests(id) ON DELETE RESTRICT,
    ADD COLUMN IF NOT EXISTS newest_ranked_day_version_id bigint;
ALTER TABLE boundary_publication_manifest_rows
    ALTER COLUMN input_identity DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS identity_manifest_id bigint;
ALTER TABLE boundary_publication_manifest_rows
    DROP CONSTRAINT IF EXISTS boundary_publication_manifest_rows_identity_source,
    DROP CONSTRAINT IF EXISTS boundary_publication_manifest_rows_identity_check;
ALTER TABLE boundary_publication_manifest_rows
    ADD CONSTRAINT boundary_publication_manifest_rows_identity_source
        FOREIGN KEY (identity_manifest_id, player_id)
        REFERENCES boundary_publication_manifest_rows (manifest_id, player_id)
        ON DELETE RESTRICT NOT VALID,
    ADD CONSTRAINT boundary_publication_manifest_rows_identity_check
        CHECK ((input_identity IS NULL) = (identity_manifest_id IS NOT NULL)) NOT VALID;

-- A base is always a sealed full manifest of the same Reset and kind, and a
-- reused identity is always one stored in full, so rebuilding any manifest
-- reads at most one other manifest's rows.
CREATE OR REPLACE FUNCTION clashlens_boundary_manifest_reuse_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'boundary_publication_manifests' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM boundary_publication_manifests AS base
            JOIN boundary_publication_generations AS base_generation
              ON base_generation.id = base.generation_id
            JOIN boundary_publication_generations AS generation
              ON generation.id = NEW.generation_id
            WHERE base.id = NEW.base_manifest_id
              AND base.base_manifest_id IS NULL AND base.rows_sealed
              AND base.artifact_kind = NEW.artifact_kind
              AND base_generation.boundary_at = generation.boundary_at
        ) THEN
            RAISE EXCEPTION 'a manifest can reuse only a sealed full manifest of its Reset and kind';
        END IF;
    ELSIF NOT EXISTS (
        SELECT 1
        FROM boundary_publication_manifest_rows AS source
        WHERE source.manifest_id = NEW.identity_manifest_id
          AND source.player_id = NEW.player_id
          AND source.input_identity IS NOT NULL
    ) THEN
        RAISE EXCEPTION 'a reused manifest identity must be stored in full';
    END IF;
    RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION clashlens_boundary_manifest_reuse_guard() FROM PUBLIC;
DROP TRIGGER IF EXISTS boundary_publication_manifests_reuse
    ON boundary_publication_manifests;
CREATE TRIGGER boundary_publication_manifests_reuse
BEFORE INSERT ON boundary_publication_manifests
FOR EACH ROW WHEN (NEW.base_manifest_id IS NOT NULL)
EXECUTE FUNCTION clashlens_boundary_manifest_reuse_guard();
DROP TRIGGER IF EXISTS boundary_publication_manifest_rows_reuse
    ON boundary_publication_manifest_rows;
CREATE TRIGGER boundary_publication_manifest_rows_reuse
BEFORE INSERT ON boundary_publication_manifest_rows
FOR EACH ROW WHEN (NEW.identity_manifest_id IS NOT NULL)
EXECUTE FUNCTION clashlens_boundary_manifest_reuse_guard();

-- Every row of manifest $1. A full manifest's rows are its own. One with a
-- base has the base's members in the base's order: a member it stored itself
-- takes its own row, any other takes the base's, and a row without its own
-- identity takes the one it names. A reused identity gets this manifest's
-- generation. identity_manifest_id names the manifest storing the identity
-- in full. Two branches let a full manifest read its own rows with no lookup
-- per row; the planner folds this into the caller's query.
CREATE OR REPLACE FUNCTION boundary_publication_manifest_entries(bigint)
RETURNS TABLE (
    manifest_id bigint,
    ordinal integer,
    player_id bigint,
    ranked_day_version_id bigint,
    input_hash text,
    classification text,
    unavailable_reason text,
    input_identity jsonb,
    identity_manifest_id bigint
)
LANGUAGE sql STABLE
AS $$
    SELECT entry.manifest_id, entry.ordinal, entry.player_id,
           entry.ranked_day_version_id, entry.input_hash, entry.classification,
           entry.unavailable_reason, entry.input_identity, entry.manifest_id
    FROM boundary_publication_manifest_rows AS entry
    WHERE entry.manifest_id = $1
      AND NOT EXISTS (
          SELECT 1 FROM boundary_publication_manifests AS manifest
          WHERE manifest.id = $1 AND manifest.base_manifest_id IS NOT NULL
      )
    UNION ALL
    SELECT manifest.id, entry.ordinal, entry.player_id,
           CASE WHEN own.manifest_id IS NULL THEN entry.ranked_day_version_id
                ELSE own.ranked_day_version_id END,
           CASE WHEN own.manifest_id IS NULL THEN entry.input_hash
                ELSE own.input_hash END,
           CASE WHEN own.manifest_id IS NULL THEN entry.classification
                ELSE own.classification END,
           CASE WHEN own.manifest_id IS NULL THEN entry.unavailable_reason
                ELSE own.unavailable_reason END,
           COALESCE(
               own.input_identity,
               COALESCE(source.input_identity, entry.input_identity)
                   || jsonb_build_object('generation', generation.generation)
           ),
           CASE WHEN own.manifest_id IS NULL THEN entry.manifest_id
                ELSE COALESCE(own.identity_manifest_id, own.manifest_id) END
    FROM boundary_publication_manifests AS manifest
    JOIN boundary_publication_generations AS generation
      ON generation.id = manifest.generation_id
    JOIN boundary_publication_manifest_rows AS entry
      ON entry.manifest_id = manifest.base_manifest_id
    LEFT JOIN boundary_publication_manifest_rows AS own
      ON own.manifest_id = manifest.id AND own.player_id = entry.player_id
    LEFT JOIN boundary_publication_manifest_rows AS source
      ON source.manifest_id = own.identity_manifest_id
     AND source.player_id = own.player_id
    WHERE manifest.id = $1
$$;
REVOKE ALL ON FUNCTION boundary_publication_manifest_entries(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION boundary_publication_manifest_entries(bigint)
    TO clashlens_python_worker, clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (81)
ON CONFLICT (version) DO NOTHING;

COMMIT;
