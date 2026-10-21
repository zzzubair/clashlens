-- Clash Lens deployment migration 0125.
-- Notebook Seasons (api_notebook.py): a base still Active or To try when its
-- Season ends carries into the account's next notebook keeping its base ID,
-- so one ID now names one base in each Season it is in, never another
-- account's or another base's. A copy from a past Season gets a new ID and
-- remembers the one it came from, for support.
--
-- notebook_carries holds one row per Clash Lens account and Season whose
-- notebook was opened or changed, so the carry happens once and a base the
-- Clasher deletes afterwards stays deleted: about 50 bytes a row, 1,000
-- Clashers add about 50 KB a Season, kept until the account is deleted.
-- copied_from adds 9 bytes to a copied base. Every statement here locks
-- tables that are empty while the Notebook is off, so it runs in
-- milliseconds. Numbered 0125, after the notebook's 0124; other notebook
-- branches may need renumbering when they merge.
BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS notebook_bases_code_season
    ON notebook_bases (code, season_id);
ALTER TABLE notebook_bases DROP CONSTRAINT IF EXISTS notebook_bases_code_key;
-- The base ID of the base this one was copied from, if it was.
ALTER TABLE notebook_bases ADD COLUMN IF NOT EXISTS copied_from text
    CHECK (copied_from ~ '^B-[0-9A-HJKMNP-TV-Z]{6}$');

CREATE TABLE IF NOT EXISTS notebook_carries (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    season_id text NOT NULL CHECK (season_id ~ '^[0-9]{1,12}$'),
    carried_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, season_id)
);

REVOKE ALL ON notebook_carries FROM PUBLIC;
GRANT SELECT, INSERT ON notebook_carries TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (125)
ON CONFLICT (version) DO NOTHING;
COMMIT;
