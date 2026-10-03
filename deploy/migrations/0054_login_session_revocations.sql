-- Clash Lens deployment migration 0054.
-- Logout ends a browser login on the server. The website stores the SHA-256
-- of each logged-out login cookie here and refuses any copy of that cookie.
-- A login cookie lives at most 24 hours, so the API deletes rows older than
-- 25 hours whenever it adds one; the table holds about one day of logouts.
BEGIN;

CREATE TABLE IF NOT EXISTS login_session_revocations (
    session_hash text PRIMARY KEY CHECK (session_hash ~ '^[A-Za-z0-9_-]{43}$'),
    revoked_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, DELETE ON TABLE login_session_revocations TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (54)
ON CONFLICT (version) DO NOTHING;
COMMIT;
