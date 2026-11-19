-- Clash Lens deployment migration 0145.
-- Every function that runs with its owner's rights (SECURITY DEFINER) gets a
-- fixed name lookup order: pg_catalog, this schema, then the session's
-- temporary schema last. Replacing a function clears its saved lookup order,
-- so this reads the catalogue and sets it on every such function in the
-- schema that lacks it, instead of naming them one by one. Functions that
-- already have exactly this order are left as they are.
-- No runtime login creates temporary tables (only migrations do, as the
-- database owner), so the default right to create them is taken from PUBLIC.
-- Nothing is stored or rewritten, and running it again changes nothing.
BEGIN;

DO $$
DECLARE
    routine regprocedure;
BEGIN
    FOR routine IN
        SELECT p.oid::regprocedure
        FROM pg_proc AS p
        WHERE p.prosecdef
          AND p.pronamespace = to_regnamespace(current_schema())
          AND NOT EXISTS (
              SELECT 1 FROM unnest(COALESCE(p.proconfig, '{}')) AS setting
              WHERE setting = format('search_path=pg_catalog, %s, pg_temp',
                                     quote_ident(current_schema()))
          )
        ORDER BY p.oid
    LOOP
        EXECUTE format('ALTER ROUTINE %s SET search_path TO pg_catalog, %I, pg_temp',
                       routine, current_schema());
    END LOOP;
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;

INSERT INTO clash_lens_schema_migrations(version) VALUES (145)
ON CONFLICT (version) DO NOTHING;
COMMIT;
