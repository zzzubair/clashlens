-- Whole-season unit and quantity usage with 1/2/3-star outcome counts.
-- Existing finalized summaries lack
-- this evidence and remain untouched; the API reports them unavailable.
-- Rebuild nonfinalized seasons from facts before finalizing them.
BEGIN;
ALTER TABLE army_season_summaries ADD COLUMN IF NOT EXISTS unit_usage jsonb
    CHECK (unit_usage IS NULL OR (jsonb_typeof(unit_usage) = 'array'
        AND octet_length(unit_usage::text) <= 524288));
-- Rebuilding a nonfinalized season removes its obsolete combination rows.
GRANT DELETE ON army_season_summaries TO clashlens_python_worker;
INSERT INTO clash_lens_schema_migrations(version) VALUES (35)
ON CONFLICT (version) DO NOTHING;
COMMIT;
