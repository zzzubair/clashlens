-- Clash Lens deployment migration 0113.
-- OBS overlay: a Clasher sets up a stream overlay of one verified player's
-- live Legend day on the website, and pastes one private link into OBS.
--
-- obs_overlays holds one row per account that opened the overlay page, so it
-- is never larger than the account list, and never grows: about 300 bytes of
-- settings and the link. Making a new link replaces link_id in place, so the
-- old link shows nothing from then on. The player shown is only shown while
-- it is still one of the account's verified players.
--
-- last_seen_at is when an overlay in OBS last asked for its settings through
-- the link, written at most once a minute. While it is under 5 minutes old,
-- the collector checks the player first in its queue, 30 seconds apart.
BEGIN;

CREATE TABLE IF NOT EXISTS obs_overlays (
    account_id bigint PRIMARY KEY REFERENCES clash_lens_accounts (id) ON DELETE CASCADE,
    -- 16 random bytes, URL-safe base64.
    link_id text NOT NULL UNIQUE CHECK (link_id ~ '^[A-Za-z0-9_-]{22}$'),
    player_id bigint REFERENCES players (id) ON DELETE SET NULL,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(settings) = 'object' AND octet_length(settings::text) <= 2048),
    -- Goes up by one on every save, so an open overlay applies each change once.
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz
);

REVOKE ALL PRIVILEGES ON TABLE obs_overlays FROM PUBLIC;
-- The website saves and reads overlays through the private API.
GRANT SELECT, INSERT, UPDATE ON TABLE obs_overlays TO clashlens_python_api;
-- The collector reads which players an open overlay shows.
GRANT SELECT (player_id, last_seen_at) ON TABLE obs_overlays TO clashlens_collector;

INSERT INTO clash_lens_schema_migrations(version) VALUES (113)
ON CONFLICT (version) DO NOTHING;
COMMIT;
