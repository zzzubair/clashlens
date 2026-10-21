-- Clash Lens deployment migration 0125.
-- Notebook pictures: one picture per base, kept in the private R2 bucket as
-- a picture at most 1,600 pixels on its longer side and a 96-pixel square
-- small copy for lists (api_notebook_pictures.py). This table holds one row
-- per upload: about 100 bytes, and its two stored files about 200 KB.
-- Uploads are limited to 30 per Clash Lens account in any 24 hours, so a
-- row a day per Clasher at most in practice; a picture replaced, removed,
-- or left behind by its deleted base or account is deleted, row and files,
-- once it has been off its base for a day, so Undo can still put it back.
-- Numbered 0125 after the notebook's 0124; other notebook branches may
-- need renumbering when they merge.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_pictures (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- No reference to the account: a deleted account's uploads still count
    -- until swept, and its files are found and deleted from these rows.
    account_id bigint NOT NULL,
    -- NULL while uploading, and once replaced, removed or left behind.
    base_id bigint REFERENCES notebook_bases (id) ON DELETE SET NULL,
    -- The files' names in the bucket: <key>/full.webp and <key>/small.webp.
    key text NOT NULL UNIQUE CHECK (key ~ '^[0-9a-f]{32}$'),
    -- Both files together.
    bytes integer NOT NULL CHECK (bytes > 0),
    uploaded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When it came off its base; NULL while on it, and when its base or
    -- account was deleted outright, which uploaded_at then stands in for.
    detached_at timestamptz
);
-- One picture per base.
CREATE UNIQUE INDEX IF NOT EXISTS notebook_pictures_base
    ON notebook_pictures (base_id) WHERE base_id IS NOT NULL;
-- The 30-a-day limit.
CREATE INDEX IF NOT EXISTS notebook_pictures_account
    ON notebook_pictures (account_id, uploaded_at);
-- Pictures no base uses, oldest first, for deletion.
CREATE INDEX IF NOT EXISTS notebook_pictures_loose
    ON notebook_pictures ((COALESCE(detached_at, uploaded_at))) WHERE base_id IS NULL;

REVOKE ALL ON notebook_pictures FROM PUBLIC;
-- The private API uploads, links, removes and deletes pictures, and the
-- alert check's weekly usage line reads them through the API's role.
GRANT SELECT, INSERT, UPDATE, DELETE ON notebook_pictures TO clashlens_python_api;
GRANT USAGE ON SEQUENCE notebook_pictures_id_seq TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (125)
ON CONFLICT (version) DO NOTHING;
COMMIT;
