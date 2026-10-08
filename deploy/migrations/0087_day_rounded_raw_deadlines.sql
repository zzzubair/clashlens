-- Clash Lens deployment migration 0087.
-- A raw response is now kept until the end of its latest sighting's UTC day,
-- plus 86 days: never less than 86 days after the sighting, at most one day
-- more. Every sighting on the same day then gives the same deadline, so only
-- a body's first sighting each day rewrites its deadline and its upload's
-- latest sighting. On 8 October 2026 these rewrites, repeated whenever a body
-- was seen again over 10 minutes later, and the lock before them wrote 27 to
-- 30 GB/day of the database change log, about a third of all of it, in two
-- 5-minute samples. Existing deadlines stay as they are: each moves to the
-- rounded deadline at the body's next sighting, and one never seen again
-- keeps its exact 86 days. The function keeps its historical name so callers
-- and the observation trigger stay unchanged.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_season_retire_after(observed_at timestamptz)
RETURNS timestamptz
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT to_timestamp((floor(extract(epoch FROM observed_at) / 86400) + 1) * 86400 + 7430400)
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

INSERT INTO clash_lens_schema_migrations(version) VALUES (87)
ON CONFLICT (version) DO NOTHING;
COMMIT;
