-- Remember which exact upload claim reached a terminal state so a caller can
-- safely reconcile a retry after losing the database commit acknowledgement.
BEGIN;

ALTER TABLE collector_response_uploads
    ADD COLUMN settled_lease_token uuid,
    ADD COLUMN last_error_retryable boolean;

INSERT INTO clash_lens_schema_migrations(version) VALUES (29)
ON CONFLICT (version) DO NOTHING;
COMMIT;
