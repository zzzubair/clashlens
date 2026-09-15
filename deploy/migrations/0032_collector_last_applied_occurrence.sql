-- Keep crash-replay identity separate from the freshest provider response.
BEGIN;

ALTER TABLE collector_response_state
    ADD COLUMN last_applied_occurrence_key text;

UPDATE collector_response_state
SET last_applied_occurrence_key = last_occurrence_key;

ALTER TABLE collector_response_state
    ALTER COLUMN last_applied_occurrence_key SET NOT NULL;

INSERT INTO clash_lens_schema_migrations(version) VALUES (32)
ON CONFLICT (version) DO NOTHING;
COMMIT;
