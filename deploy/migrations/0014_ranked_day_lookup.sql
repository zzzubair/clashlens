-- Clash Lens deployment migration 0014.
-- Bound the decode-driven army enqueue lookup by ranked day.
BEGIN;

CREATE INDEX IF NOT EXISTS ranked_day_versions_completed_day_v1
    ON ranked_day_versions (ranked_day_start, id DESC)
    WHERE state = 'Complete' AND coverage_complete;

INSERT INTO clash_lens_schema_migrations(version) VALUES (14)
ON CONFLICT (version) DO NOTHING;
COMMIT;
