-- Remote retention is six calendar months since last sighting, not upload age.
-- Keep tombstones and use a new immutable location after retirement: an unknown
-- DELETE outcome on an old key must never delete newly collected evidence.
BEGIN;
ALTER TABLE archive_catalogue
    ADD COLUMN IF NOT EXISTS last_seen_before timestamptz NOT NULL
        DEFAULT (date_trunc('hour', clock_timestamp()) + interval '1 hour'),
    ADD COLUMN IF NOT EXISTS availability text NOT NULL DEFAULT 'verified'
        CHECK (availability IN ('verified', 'retiring', 'expired'));
-- The existing composite unique constraint remains the observation FK target.
ALTER TABLE archive_catalogue DROP CONSTRAINT IF EXISTS archive_catalogue_pkey;
CREATE INDEX IF NOT EXISTS archive_catalogue_current_hash
    ON archive_catalogue (response_hash, first_verified_at DESC, archive_reference)
    WHERE availability = 'verified';
CREATE INDEX IF NOT EXISTS archive_catalogue_retention
    ON archive_catalogue (availability, last_seen_before);
CREATE INDEX IF NOT EXISTS collector_observations_archive_reference
    ON collector_observations (archive_reference, id);

CREATE OR REPLACE FUNCTION clashlens_observed_archive_retention()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.archive_catalogue_hash IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM archive_catalogue
        WHERE response_hash = NEW.response_hash
          AND archive_reference = NEW.archive_reference
          AND availability = 'verified'
    ) THEN
        RAISE EXCEPTION 'observation requires a currently verified archive location';
    END IF;
    -- An upper bound avoids a hot shared-hash update on every duplicate poll.
    -- Expiry can be delayed by up to one hour, never advanced before six months.
    UPDATE archive_catalogue
    SET last_seen_before = date_trunc('hour', GREATEST(
        NEW.response_completed_at, clock_timestamp()
    )) + interval '1 hour'
    WHERE response_hash = NEW.response_hash
      AND archive_reference = NEW.archive_reference
      AND last_seen_before < GREATEST(NEW.response_completed_at, clock_timestamp());
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS collector_observations_archive_retention ON collector_observations;
CREATE TRIGGER collector_observations_archive_retention
BEFORE INSERT ON collector_observations
FOR EACH ROW EXECUTE FUNCTION clashlens_observed_archive_retention();

CREATE OR REPLACE FUNCTION clashlens_reject_expired_job_source()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    -- Acquire the observation fence BEFORE checking availability. Relying on
    -- the later FK check lets an INSERT pass this trigger, wait for retirement,
    -- then create active replay work against an already expired location.
    PERFORM 1 FROM collector_observations
    WHERE id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
    FOR KEY SHARE;
    IF NEW.status NOT IN ('complete', 'cancelled', 'failed') AND EXISTS (
        SELECT 1 FROM collector_observations AS observation
        JOIN archive_catalogue AS catalogue
          ON catalogue.response_hash = observation.archive_catalogue_hash
         AND catalogue.archive_reference = observation.archive_reference
        WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id)
          AND catalogue.availability <> 'verified'
    ) THEN
        RAISE EXCEPTION 'raw evidence expired; historical poll replay is unavailable';
    END IF;
    RETURN NEW;
END $$;
DO $$ BEGIN
    EXECUTE format(
        'ALTER FUNCTION clashlens_reject_expired_job_source() SET search_path TO pg_catalog, %I',
        current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_reject_expired_job_source() FROM PUBLIC;
DROP TRIGGER IF EXISTS python_processing_jobs_live_archive ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_live_archive
BEFORE INSERT OR UPDATE OF status, observation_id, replay_observation_id
ON python_processing_jobs
FOR EACH ROW EXECUTE FUNCTION clashlens_reject_expired_job_source();

INSERT INTO clash_lens_schema_migrations(version) VALUES (18)
ON CONFLICT (version) DO NOTHING;
COMMIT;
