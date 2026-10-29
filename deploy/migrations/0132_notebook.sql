-- Clash Lens deployment migration 0132.
-- Notebook (api_notebook.py): a base stands on its own with one base ID,
-- and a notebook only points to it, so one base can sit in many notebooks
-- with one set of results. Each Clash Lens account has one personal
-- notebook. Each game account runs one base a Legend day across every
-- notebook: a day pick. Every defense number comes from the worker's
-- per-day defense counts (0106), so nothing here is written by the worker
-- or at the Reset.
--
-- Sizes: a base is about 200 bytes plus its note (at most 1,000
-- characters); an entry about 70 bytes; a day pick about 60 bytes, at most
-- one a game account a Legend day, so 28 a game account a Season. 1,000
-- Clashers keeping 50 bases a Season add about 15 MB a Season. Rows are
-- kept until the Clasher removes them or deletes their account; a base
-- with results is never deleted. Every table is new and empty while the
-- Notebook is off, so this locks nothing in use.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_bases (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- B- and 6 characters of Crockford's base-32 alphabet (no I, L, O or U),
    -- drawn at random when the base is made and never changed or reused.
    code text NOT NULL UNIQUE CHECK (code ~ '^B-[0-9A-HJKMNP-TV-Z]{6}$'),
    -- Only its maker edits it. NULL once the maker's account is deleted:
    -- the base and its results stay for every notebook pointing to it.
    made_by bigint REFERENCES clash_lens_accounts (id) ON DELETE SET NULL,
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 1000),
    base_link text CHECK (char_length(base_link) BETWEEN 1 AND 300),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- One personal notebook per Clash Lens account, made when it is first used.
CREATE TABLE IF NOT EXISTS notebooks (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL UNIQUE REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- A notebook pointing to a base. Removing it never touches the base or
-- its results.
CREATE TABLE IF NOT EXISTS notebook_entries (
    notebook_id bigint NOT NULL REFERENCES notebooks (id) ON DELETE CASCADE,
    base_id bigint NOT NULL REFERENCES notebook_bases (id) ON DELETE CASCADE,
    -- The Season page it was last put on, by adding it or bringing it
    -- back: the Season's official id, its start in Unix seconds.
    season_id text NOT NULL CHECK (season_id ~ '^[0-9]{1,12}$'),
    added_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When it was added or last brought back. It retires on its own 30 days
    -- after the later of this and its last counted Legend day.
    kept_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- Set when the Clasher retires it; NULL otherwise.
    retired_at timestamptz,
    PRIMARY KEY (notebook_id, base_id)
);
CREATE INDEX IF NOT EXISTS notebook_entries_base ON notebook_entries (base_id);

-- A game account's base from a Legend day on, until its next pick or the
-- first day it is out of Legend League: one base per game account per
-- Legend day, across every notebook, with no daily job. A base someone
-- picked is never deleted, so its results stay.
CREATE TABLE IF NOT EXISTS notebook_day_picks (
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    ranked_day_start timestamptz NOT NULL,
    -- NULL: no base from this day on.
    base_id bigint REFERENCES notebook_bases (id) ON DELETE RESTRICT,
    picked_by bigint REFERENCES clash_lens_accounts (id) ON DELETE SET NULL,
    picked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (player_id, ranked_day_start)
);
CREATE INDEX IF NOT EXISTS notebook_day_picks_base
    ON notebook_day_picks (base_id, ranked_day_start);

REVOKE ALL ON notebook_bases, notebooks, notebook_entries, notebook_day_picks FROM PUBLIC;
-- The website reads and saves notebooks through the private API.
GRANT SELECT, INSERT, UPDATE, DELETE
    ON notebook_bases, notebooks, notebook_entries, notebook_day_picks
    TO clashlens_python_api;
GRANT USAGE ON SEQUENCE notebook_bases_id_seq, notebooks_id_seq TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (132)
ON CONFLICT (version) DO NOTHING;
COMMIT;
