-- Clash Lens deployment migration 0111.
-- Legend day summary: once the Daily board for a Reset is published, the
-- Discord bot sends each Discord-connected account one DM summing up the
-- Legend day that Reset ended, for every verified player whose summary is
-- on. Players are on unless turned off on the website's Discord alerts page;
-- the players turned off are kept in the account's existing preferences
-- (preferences.discord_day_summary_off), so this migration adds no switch.
--
-- discord_day_summary_deliveries is the bot's log of every summary it
-- decided on: one row per account and Reset, written before the first DM is
-- sent, so no account is sent the same day's summary twice, across restarts
-- too. A summary too long for one DM goes as several, sent in order; each is
-- tried again like an attack alert DM, within 2 minutes of its own first
-- try, and a crash leaves the row unfinished, never tried again. A summary
-- with no finished day to show is logged as skipped and sends nothing. The
-- bot deletes rows older than 7 days, so at most 7 rows are kept per
-- Discord-connected account.
BEGIN;

CREATE TABLE IF NOT EXISTS discord_day_summary_deliveries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    -- The Reset that ended the summed-up Legend day; its Daily board is the
    -- one the summary's places come from.
    reset_at timestamptz NOT NULL,
    -- sending: claimed and not finished; skipped: no finished day to show.
    -- refused and failed as for attack alerts (migration 0108).
    state text NOT NULL CHECK (state IN ('sending', 'sent', 'skipped', 'refused', 'failed')),
    reason text CHECK (reason IN ('no_shared_server', 'dms_closed', 'unknown_user', 'other')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE (account_id, reset_at)
);

REVOKE ALL PRIVILEGES ON TABLE discord_day_summary_deliveries FROM PUBLIC;
-- The bot runs with the API's database login.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE discord_day_summary_deliveries
    TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (111)
ON CONFLICT (version) DO NOTHING;
COMMIT;
