-- Clash Lens deployment migration 0089.
-- The Discord bot remembers one main player per Clash Lens account: the
-- verified player its single-player commands use when none is chosen.
-- Unverifying the player or moving it to another account deletes its main row
-- in the same transaction, so moving it away and back does not restore it.
-- Deleting the account or the player deletes the row. At most one row per
-- account, so the table stays as small as the account list.
BEGIN;

CREATE TABLE IF NOT EXISTS discord_bot_main_players (
    account_id bigint PRIMARY KEY REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL PRIVILEGES ON TABLE discord_bot_main_players FROM PUBLIC;
-- The bot runs the API's own read code with the API's database role.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE discord_bot_main_players TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_discord_bot_main()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.account_id IS DISTINCT FROM NEW.account_id THEN
        DELETE FROM discord_bot_main_players
        WHERE account_id = OLD.account_id AND player_id = OLD.player_id;
    END IF;
    RETURN NULL;
END $$;
DO $$ BEGIN
    EXECUTE format(
        'ALTER FUNCTION clashlens_forget_discord_bot_main() SET search_path TO pg_catalog, %I',
        current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_discord_bot_main() FROM PUBLIC;
DROP TRIGGER IF EXISTS verified_player_links_forget_discord_bot_main ON verified_player_links;
CREATE TRIGGER verified_player_links_forget_discord_bot_main
AFTER DELETE OR UPDATE OF account_id ON verified_player_links
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_discord_bot_main();

INSERT INTO clash_lens_schema_migrations(version) VALUES (89)
ON CONFLICT (version) DO NOTHING;
COMMIT;
