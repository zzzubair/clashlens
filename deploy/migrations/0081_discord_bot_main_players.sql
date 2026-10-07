-- Clash Lens deployment migration 0081.
-- The Discord bot remembers one main player per Clash Lens account: the
-- verified player its single-player commands use when none is chosen.
-- The bot forgets a main as soon as it reads one that is no longer verified to
-- the same account, so unverifying or moving the player clears it. A player
-- moved away and back with no bot read in between keeps its old main.
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

INSERT INTO clash_lens_schema_migrations(version) VALUES (81)
ON CONFLICT (version) DO NOTHING;
COMMIT;
