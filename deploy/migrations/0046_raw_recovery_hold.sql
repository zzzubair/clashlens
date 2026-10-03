-- Replace the season-based deadline with 86 days after the latest sighting.
-- Existing records use the later of their retained sighting and first
-- verification. Keep the function's historical name so existing callers
-- and triggers stay unchanged.
-- Retirement first marks a response 'retiring', which
-- blocks every new use, and deletes the bytes only after the promised
-- seven-day recovery window plus a restore allowance has passed, so a
-- restore to any promised point never references deleted bytes.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_season_retire_after(observed_at timestamptz)
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT to_timestamp(extract(epoch FROM observed_at) + 7430400)
$$;
ALTER FUNCTION clashlens_season_retire_after(timestamptz) SECURITY DEFINER;
DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_season_retire_after(timestamptz) SET search_path TO pg_catalog, %I',
        current_schema(), current_schema()
    );
END
$$;

-- The UPDATE below writes a new copy of all 1.15 million rows on production
-- on 2026-10-03, adding an entry to every index for each. The old deadline
-- index is replaced below. The current-hash index is not rebuilt: 0051 drops
-- it, and the unique (response_hash, archive_reference) index serves the same
-- lookups.
DROP INDEX IF EXISTS archive_catalogue_retention;
DROP INDEX IF EXISTS archive_catalogue_current_hash;

-- Recalculate every kept response: 86 days after the later of its latest
-- retained sighting (the exact location's observations, plus the hash's upload
-- row and compact state) and its first verification. The old season deadline
-- is discarded. The catalogue joins itself on archive_reference alone, which
-- is unique, and the upload row is joined directly by its key: matching on
-- two columns, or on a summary of all hashes, made the planner expect one row
-- and loop over 1.1 million on production on 2026-10-03.
WITH by_reference AS (
    SELECT archive_reference, max(response_completed_at) AS seen_at
    FROM collector_observations WHERE archive_reference IS NOT NULL
    GROUP BY archive_reference
), by_state AS (
    SELECT last_response_hash AS response_hash, max(last_seen_at) AS seen_at
    FROM collector_response_state
    GROUP BY last_response_hash
)
UPDATE archive_catalogue AS catalogue
SET retire_after = clashlens_season_retire_after(GREATEST(
    by_reference.seen_at, upload.latest_sighting_at, by_state.seen_at,
    latest.first_verified_at
))
FROM archive_catalogue AS latest
LEFT JOIN by_reference USING (archive_reference)
LEFT JOIN collector_response_uploads AS upload USING (response_hash)
LEFT JOIN by_state USING (response_hash)
WHERE latest.archive_reference = catalogue.archive_reference
  AND latest.availability IN ('verified', 'retiring');

-- Pending uploads take the same rule at completion; the old season floor goes.
ALTER TABLE collector_response_uploads DROP COLUMN minimum_retire_after;

ALTER TABLE archive_catalogue ADD COLUMN retiring_since timestamptz;
-- Responses already marked by the old code start their recovery hold now, or
-- at their recalculated deadline if that is later.
UPDATE archive_catalogue
SET retiring_since = GREATEST(clock_timestamp(), retire_after)
WHERE availability = 'retiring';

-- Avoid sorting all due responses before selecting a bounded batch.
CREATE INDEX archive_catalogue_retention
    ON archive_catalogue (retire_after, archive_reference)
    WHERE availability = 'verified';
CREATE INDEX archive_catalogue_retiring
    ON archive_catalogue (retiring_since, archive_reference)
    WHERE availability = 'retiring';

INSERT INTO clash_lens_schema_migrations(version) VALUES (46)
ON CONFLICT (version) DO NOTHING;
COMMIT;
