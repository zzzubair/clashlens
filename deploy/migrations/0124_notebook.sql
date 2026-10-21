-- Clash Lens deployment migration 0124.
-- Season notebook: a Clasher keeps their bases, one notebook per Clash Lens
-- account per Season, and says which base each verified player ran on each
-- Legend day. Every defense number comes from the worker's per-day defense
-- counts (0106), so nothing here is written by the worker or at the Reset.
--
-- notebook_bases holds one row per base: an ID like B-7K3QX9, a name, a
-- note, an in-game base link and a status. About 200 bytes a base plus its
-- note (at most 1,000 characters); 1,000 Clashers keeping 50 bases a Season
-- add about 10 MB a Season. notebook_day_picks holds one row per player and
-- Legend day a base was picked from, at most one a day: 28 a player a
-- Season, about 60 bytes each. No limit on bases; rows are kept until the
-- Clasher deletes the base or their account. Numbered 0124, leaving 0122
-- to work in progress on another branch.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_bases (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- B- and 6 characters of Crockford's base-32 alphabet (no I, L, O or U),
    -- drawn at random when the base is made and never changed or reused.
    code text NOT NULL UNIQUE CHECK (code ~ '^B-[0-9A-HJKMNP-TV-Z]{6}$'),
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    -- The notebook's Season: its official id, the Season's start in Unix seconds.
    season_id text NOT NULL CHECK (season_id ~ '^[0-9]{1,12}$'),
    name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
    note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 1000),
    base_link text CHECK (char_length(base_link) BETWEEN 1 AND 300),
    -- Active while one of the account's players runs it from tomorrow, on a
    -- Legend day. Otherwise To try or Retired as the Clasher sets it; when a
    -- change stops it, or puts it on or takes it off a day, Retired if it
    -- ran and To try if it never did (api_notebook.py).
    status text NOT NULL CHECK (status IN ('active', 'to_try', 'retired')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    -- When its status last changed, for the Library's Recent order.
    status_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS notebook_bases_notebook
    ON notebook_bases (account_id, season_id);

-- A player's base from a Legend day on, until their next pick or the first
-- day they are out of Legend League: so one base per player per Legend day,
-- and carrying a base forward needs no daily job.
CREATE TABLE IF NOT EXISTS notebook_day_picks (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    ranked_day_start timestamptz NOT NULL,
    -- NULL: no base from this day on.
    base_id bigint REFERENCES notebook_bases (id) ON DELETE SET NULL,
    picked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, player_id, ranked_day_start)
);
CREATE INDEX IF NOT EXISTS notebook_day_picks_base ON notebook_day_picks (base_id);

REVOKE ALL ON notebook_bases, notebook_day_picks FROM PUBLIC;
-- The website reads and saves notebooks through the private API.
GRANT SELECT, INSERT, UPDATE, DELETE ON notebook_bases, notebook_day_picks
    TO clashlens_python_api;
GRANT USAGE ON SEQUENCE notebook_bases_id_seq TO clashlens_python_api;
-- Bases are set, and records counted, on Legend days only: days in a week
-- the weekly league record (0115) says was Legend I.
GRANT SELECT (player_id, week_start, league) ON player_league_weeks TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (124)
ON CONFLICT (version) DO NOTHING;
COMMIT;
