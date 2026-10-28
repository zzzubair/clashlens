-- Clash Lens deployment migration 0133.
-- Crew notebooks (api_notebook.py): every Crew gets one notebook, beside
-- each account's own, pointing to bases the same way, so a base in a Crew
-- and in several Clashers' notebooks keeps one set of results. A new crew
-- role, Uploader, adds bases to the Crew's notebook; owners and admins make
-- Uploaders. No existing row changes: the role check only gains a value,
-- and a personal notebook keeps its account.
--
-- Sizes: one notebook row a Crew (about 50 bytes) and an entry (about 70
-- bytes) for each base in it. A Crew keeping 50 bases a Season adds about
-- 4 KB a Season; at most 5 Crews an account.
BEGIN;

ALTER TABLE crew_accounts
    DROP CONSTRAINT crew_accounts_role_check,
    ADD CONSTRAINT crew_accounts_role_check
        CHECK (role IN ('owner', 'admin', 'uploader', 'member'));

-- A notebook belongs to one account or to one Crew, never both. Deleting
-- the Crew deletes its notebook and entries; the bases and their results
-- stay.
ALTER TABLE notebooks
    ALTER COLUMN account_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS crew_id bigint UNIQUE REFERENCES crews (id) ON DELETE CASCADE,
    ADD CONSTRAINT notebooks_one_owner CHECK ((account_id IS NULL) <> (crew_id IS NULL));

-- Who put a base in a notebook, so an Uploader can retire or remove the
-- bases they added to their Crew. Earlier entries have none.
ALTER TABLE notebook_entries
    ADD COLUMN IF NOT EXISTS added_by bigint
        REFERENCES clash_lens_accounts (id) ON DELETE SET NULL;

INSERT INTO clash_lens_schema_migrations (version) VALUES (133)
ON CONFLICT (version) DO NOTHING;
COMMIT;
