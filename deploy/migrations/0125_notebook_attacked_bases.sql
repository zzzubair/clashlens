-- Clash Lens deployment migration 0125.
-- Save a base you attacked: a base saved from Bases you attacked keeps the
-- player whose base it was and the Legend day of the attack, so the base
-- page can compare how it held for them that day with how it holds for the
-- Clasher. Their record is read live from the worker's per-day defense
-- counts (0106), as the Clasher's is, so nothing else is saved.
--
-- About 20 bytes a saved base, NULL for every other base. The unique index
-- lets each attack's base be saved once per notebook. notebook_bases is new
-- in 0124 and empty while the Notebook is off, so the change takes a brief
-- lock on an empty table. Numbered 0125; notebook work on other branches
-- may need renumbering when it merges.
BEGIN;

ALTER TABLE notebook_bases
    -- The attacked player's tag, as their battle log names them; their
    -- name is never saved.
    ADD COLUMN IF NOT EXISTS from_tag text CHECK (from_tag ~ '^#[0289PYLQGRJCUV]{1,15}$'),
    -- The start of the Legend day of the attack.
    ADD COLUMN IF NOT EXISTS from_day_start timestamptz,
    ADD CONSTRAINT notebook_bases_from_both
        CHECK ((from_tag IS NULL) = (from_day_start IS NULL));
CREATE UNIQUE INDEX IF NOT EXISTS notebook_bases_from
    ON notebook_bases (account_id, season_id, from_tag, from_day_start)
    WHERE from_tag IS NOT NULL;

INSERT INTO clash_lens_schema_migrations (version) VALUES (125)
ON CONFLICT (version) DO NOTHING;
COMMIT;
