-- Keep the latest exact-byte sighting with its content-addressed upload row.
-- Compact response state can move to an ignored-field hash before a delayed
-- upload completes, so it cannot be the durable retention clock for that hash.
BEGIN;

ALTER TABLE collector_response_uploads
    ADD COLUMN latest_sighting_at timestamptz,
    ADD COLUMN minimum_retire_after timestamptz;

WITH exact_sightings AS (
    SELECT sighting.response_hash, max(sighting.seen_at) AS seen_at
    FROM (
        SELECT observation.response_hash,
               observation.response_completed_at AS seen_at
        FROM collector_observations AS observation
        UNION ALL
        SELECT state.last_response_hash AS response_hash,
               state.last_seen_at AS seen_at
        FROM collector_response_state AS state
    ) AS sighting
    GROUP BY sighting.response_hash
)
UPDATE collector_response_uploads AS upload
SET latest_sighting_at = exact_sightings.seen_at
FROM exact_sightings
WHERE exact_sightings.response_hash = upload.response_hash;

-- created_at is a known lower bound for legacy rows whose exact-byte sightings
-- were already compacted away; it is not presented as a reconstructed sighting.
UPDATE collector_response_uploads
SET latest_sighting_at = COALESCE(latest_sighting_at, created_at),
    minimum_retire_after = clashlens_season_retire_after(clock_timestamp())
WHERE latest_sighting_at IS NULL OR minimum_retire_after IS NULL;

-- Preserve every existing deadline and give already verified legacy objects a
-- migration-time safety floor. This prevents an unknowable compact sighting
-- from causing immediate retirement without inventing an observation time.
UPDATE archive_catalogue
SET retire_after = GREATEST(
    retire_after,
    clashlens_season_retire_after(clock_timestamp())
)
WHERE availability = 'verified';

ALTER TABLE collector_response_uploads
    ALTER COLUMN latest_sighting_at SET NOT NULL,
    ALTER COLUMN latest_sighting_at SET DEFAULT clock_timestamp();

INSERT INTO clash_lens_schema_migrations(version) VALUES (28)
ON CONFLICT (version) DO NOTHING;
COMMIT;
