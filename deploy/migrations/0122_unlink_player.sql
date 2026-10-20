-- Clash Lens deployment migration 0122.
-- A Clasher can unlink one of their own linked players on the website's
-- Settings page, so the private API may now delete that player's row in
-- verified_player_links (api_accounts.unlink_player). Only the link goes:
-- its crew places go with it (migration 0095), and the migration 0105, 0108
-- and 0112 triggers forget it as the default player and drop its Discord
-- alerts and summary. The player and its game data are kept, and it can be
-- linked again. Deleting writes nothing new.
BEGIN;

GRANT DELETE ON TABLE verified_player_links TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (122)
ON CONFLICT (version) DO NOTHING;
COMMIT;
