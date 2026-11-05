-- Clash Lens deployment migration 0138.
-- The site owner: the Clash Lens accounts that may open the dashboard as any
-- player, read only (website "View as", api_view_as.py). Empty, so nobody
-- can, until an operator adds the owner's account by hand:
--
--   INSERT INTO site_owner_accounts (account_id)
--   SELECT id FROM clash_lens_accounts WHERE normalized_username = '<owner>';
--
-- The data service may only read it; no application role can add, change or
-- remove a row, so no website or API request can make anyone the owner.
-- One row per owner account: a few bytes, never growing on its own. This
-- migration deletes nothing.
BEGIN;

CREATE TABLE IF NOT EXISTS site_owner_accounts (
    account_id bigint PRIMARY KEY
        REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON site_owner_accounts FROM PUBLIC;
GRANT SELECT ON site_owner_accounts TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (138)
ON CONFLICT (version) DO NOTHING;
COMMIT;
