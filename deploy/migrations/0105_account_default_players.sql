-- Clash Lens deployment migration 0105.
-- Each Clash Lens account can choose a default player on the website: the
-- verified player the Discord bot uses when a command needs one player and
-- none is named. An account with one verified player needs no row.
-- Unverifying the player or moving it to another account deletes the row in
-- the same transaction, so moving it away and back does not restore it.
-- Deleting the account or the player deletes the row. At most one row per
-- account, so the table stays as small as the account list.
BEGIN;

CREATE TABLE IF NOT EXISTS account_default_players (
    account_id bigint PRIMARY KEY REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    player_id bigint NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL PRIVILEGES ON TABLE account_default_players FROM PUBLIC;
-- The website saves it through the private API; the bot reads it with the
-- API's own code and database role.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE account_default_players TO clashlens_python_api;

CREATE OR REPLACE FUNCTION clashlens_forget_default_player()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF TG_OP = 'DELETE' OR OLD.account_id IS DISTINCT FROM NEW.account_id THEN
        DELETE FROM account_default_players
        WHERE account_id = OLD.account_id AND player_id = OLD.player_id;
    END IF;
    RETURN NULL;
END $$;
DO $$ BEGIN
    EXECUTE format(
        'ALTER FUNCTION clashlens_forget_default_player() SET search_path TO pg_catalog, %I',
        current_schema()
    );
END $$;
REVOKE ALL ON FUNCTION clashlens_forget_default_player() FROM PUBLIC;
DROP TRIGGER IF EXISTS verified_player_links_forget_default_player ON verified_player_links;
CREATE TRIGGER verified_player_links_forget_default_player
AFTER DELETE OR UPDATE OF account_id ON verified_player_links
FOR EACH ROW EXECUTE FUNCTION clashlens_forget_default_player();

INSERT INTO clash_lens_schema_migrations(version) VALUES (105)
ON CONFLICT (version) DO NOTHING;
COMMIT;
