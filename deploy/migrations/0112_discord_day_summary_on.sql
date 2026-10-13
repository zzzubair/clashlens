-- Clash Lens deployment migration 0112.
-- Legend day summary: off until the Clasher turns it on, per player, as
-- attack alerts are. Migration 0111 had every verified player's summary on
-- until turned off; nobody starts on here, so every discord_day_summary_off
-- row is dropped and no player's summary is on until the Clasher turns it on
-- on the website's Discord alerts page. The delivery log is kept as it is.
--
-- discord_day_summary_on holds one row per verified player whose summary is
-- on, so it is never larger than the verified player list. Turning the
-- summary off deletes the row. Unverifying the player or moving it to
-- another account deletes it in the same transaction, so moving it away and
-- back leaves its summary off.
BEGIN;

DROP TRIGGER IF EXISTS verified_player_links_forget_day_summary_off ON verified_player_links;
DROP FUNCTION IF EXISTS clashlens_forget_day_summary_off();
DROP TABLE IF EXISTS discord_day_summary_off;

CREATE TABLE IF NOT EXISTS discord_day_summary_on (
    account_id bigint NOT NULL REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    enabled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (account_id, player_id)
);

REVOKE ALL PRIVILEGES ON TABLE discord_day_summary_on FROM PUBLIC;
-- The website saves the switches through the private API, and the bot runs
-- with the API's database login.
GRANT SELECT, INSERT, DELETE ON TABLE discord_day_summary_on TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_day_summary_on()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.account_id IS DISTINCT FROM NEW.account_id THEN
        DELETE FROM discord_day_summary_on
        WHERE account_id = OLD.account_id AND player_id = OLD.player_id;
    END IF;
    RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS verified_player_links_forget_day_summary_on ON verified_player_links;
CREATE TRIGGER verified_player_links_forget_day_summary_on
AFTER DELETE OR UPDATE OF account_id ON verified_player_links
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_day_summary_on();

DO $$
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_forget_day_summary_on() SET search_path TO pg_catalog, %I, pg_temp',
        current_schema(), current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_day_summary_on() FROM PUBLIC;

INSERT INTO clash_lens_schema_migrations(version) VALUES (112)
ON CONFLICT (version) DO NOTHING;
COMMIT;
