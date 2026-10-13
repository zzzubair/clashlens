-- Clash Lens deployment migration 0111.
-- Legend day summary: once the Daily board for a Reset is published, the
-- Discord bot sends each Discord-connected account one DM summing up the
-- Legend day that Reset ended, for every verified player whose summary is
-- on. Players are on unless turned off on the website's Discord alerts page.
--
-- discord_day_summary_off holds one row per verified player whose summary is
-- off, so it is never larger than the verified player list. Turning the
-- summary on again deletes the row. Unverifying the player or moving it to
-- another account deletes it in the same transaction, so moving it away and
-- back turns its summary on again.
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

CREATE TABLE IF NOT EXISTS discord_day_summary_off (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    PRIMARY KEY (account_id, player_id)
);

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

REVOKE ALL PRIVILEGES ON TABLE discord_day_summary_off, discord_day_summary_deliveries
    FROM PUBLIC;
-- The website saves the switches through the private API, and the bot runs
-- with the API's database login.
GRANT SELECT, INSERT, DELETE ON TABLE discord_day_summary_off TO clashlens_python_api;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE discord_day_summary_deliveries
    TO clashlens_python_api;
-- A summary's end of day is the one the worker saved, as on the Season page.
GRANT SELECT (final_trophies_before_reset) ON TABLE ranked_day_versions
    TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_day_summary_off()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.account_id IS DISTINCT FROM NEW.account_id THEN
        DELETE FROM discord_day_summary_off
        WHERE account_id = OLD.account_id AND player_id = OLD.player_id;
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS verified_player_links_forget_day_summary_off ON verified_player_links;
CREATE TRIGGER verified_player_links_forget_day_summary_off
AFTER DELETE OR UPDATE OF account_id ON verified_player_links
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_day_summary_off();

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_forget_day_summary_off() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_day_summary_off() FROM PUBLIC;

INSERT INTO clash_lens_schema_migrations(version) VALUES (111)
ON CONFLICT (version) DO NOTHING;
COMMIT;
