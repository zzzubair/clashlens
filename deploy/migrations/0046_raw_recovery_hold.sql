-- Raw responses now become due exactly 86 days after their latest sighting
-- (was 56 days after the end of that sighting's 28-day season). The function
-- keeps its historical name so existing callers and triggers stay unchanged.
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

-- A stored deadline is old season end + 56 days and its latest sighting was
-- before that season end, so adding 30 days never gives less than 86 days.
-- Existing responses may wait up to 28 days longer than the new rule, once.
UPDATE archive_catalogue
SET retire_after = retire_after + interval '30 days'
WHERE availability = 'verified';
UPDATE collector_response_uploads
SET minimum_retire_after = minimum_retire_after + interval '30 days'
WHERE state <> 'complete' AND minimum_retire_after IS NOT NULL;

ALTER TABLE archive_catalogue ADD COLUMN retiring_since timestamptz;
-- Responses already marked by the old code start their recovery hold now.
UPDATE archive_catalogue SET retiring_since = clock_timestamp()
WHERE availability = 'retiring';

-- Many responses share one season deadline; keep each batch an index range.
DROP INDEX IF EXISTS archive_catalogue_retention;
CREATE INDEX archive_catalogue_retention
    ON archive_catalogue (availability, retire_after, archive_reference);
CREATE INDEX archive_catalogue_retiring
    ON archive_catalogue (retiring_since, archive_reference)
    WHERE availability = 'retiring';

INSERT INTO clash_lens_schema_migrations(version) VALUES (46)
ON CONFLICT (version) DO NOTHING;
COMMIT;
