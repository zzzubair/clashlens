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

-- Recalculate every kept response: 86 days after the later of its latest
-- retained sighting (the exact location's observations, plus the hash's upload
-- row and compact state) and its first verification. The old season deadline
-- is discarded.
WITH by_reference AS (
    SELECT archive_reference, max(response_completed_at) AS seen_at
    FROM collector_observations WHERE archive_reference IS NOT NULL
    GROUP BY archive_reference
), by_hash AS (
    SELECT response_hash, max(seen_at) AS seen_at
    FROM (
        SELECT response_hash, latest_sighting_at AS seen_at FROM collector_response_uploads
        UNION ALL
        SELECT last_response_hash, last_seen_at FROM collector_response_state
    ) AS sighting
    GROUP BY response_hash
), latest AS (
    SELECT catalogue.response_hash, catalogue.archive_reference,
           GREATEST(by_reference.seen_at, by_hash.seen_at, catalogue.first_verified_at) AS seen_at
    FROM archive_catalogue AS catalogue
    LEFT JOIN by_reference USING (archive_reference)
    LEFT JOIN by_hash USING (response_hash)
    WHERE catalogue.availability IN ('verified', 'retiring')
)
UPDATE archive_catalogue AS catalogue
SET retire_after = clashlens_season_retire_after(latest.seen_at)
FROM latest
WHERE latest.response_hash = catalogue.response_hash
  AND latest.archive_reference = catalogue.archive_reference;

-- Pending uploads take the same rule at completion; the old season floor goes.
ALTER TABLE collector_response_uploads DROP COLUMN minimum_retire_after;

ALTER TABLE archive_catalogue ADD COLUMN retiring_since timestamptz;
-- Responses already marked by the old code start their recovery hold now, or
-- at their recalculated deadline if that is later.
UPDATE archive_catalogue
SET retiring_since = GREATEST(clock_timestamp(), retire_after)
WHERE availability = 'retiring';

-- Avoid sorting all due responses before selecting a bounded batch.
DROP INDEX IF EXISTS archive_catalogue_retention;
CREATE INDEX archive_catalogue_retention
    ON archive_catalogue (retire_after, archive_reference)
    WHERE availability = 'verified';
CREATE INDEX archive_catalogue_retiring
    ON archive_catalogue (retiring_since, archive_reference)
    WHERE availability = 'retiring';

INSERT INTO clash_lens_schema_migrations(version) VALUES (46)
ON CONFLICT (version) DO NOTHING;
COMMIT;
