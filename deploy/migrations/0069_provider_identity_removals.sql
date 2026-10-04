-- Clash Lens deployment migration 0069.
-- Removing a sign-in connection ends every browser login made through it.
-- The API stores when each provider identity was last removed, in the same
-- transaction as the removal, and the login check refuses any login cookie
-- issued at or before that time, on every browser. A login made after the
-- removal still works, and linking the identity again never clears the row.
-- A login cookie lives at most 24 hours, so the API deletes rows older than
-- 25 hours whenever it adds one; rows can outlive that while nobody removes
-- a connection. The table holds at most one row per identity removed in
-- about the last day. Removals before this migration are not recorded, so
-- the protection starts with the first removal after it.
BEGIN;

CREATE TABLE IF NOT EXISTS provider_identity_removals (
    provider text NOT NULL CHECK (provider IN ('google', 'discord')),
    provider_subject text NOT NULL CHECK (char_length(provider_subject) BETWEEN 1 AND 255),
    removed_at timestamptz NOT NULL,
    PRIMARY KEY (provider, provider_subject)
);

REVOKE ALL PRIVILEGES ON TABLE provider_identity_removals FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE provider_identity_removals TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (69)
ON CONFLICT (version) DO NOTHING;
COMMIT;
