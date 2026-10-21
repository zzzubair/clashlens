-- Clash Lens deployment migration 0125.
-- Shared bases: a Clasher shares one notebook base through an unlisted
-- link, the same kind as the OBS overlay link (0113), that shows the base's
-- live record to anyone who has it.
--
-- notebook_shares holds one row per base while it is shared, so it is never
-- larger than notebook_bases: about 120 bytes a row. Stop sharing deletes
-- the row, so its link shows nothing from then on and is never made again
-- (16 random bytes); sharing again makes a new link. A base deleted from
-- the notebook takes its share with it. Numbered 0125 after the notebook's
-- 0124; other notebook work in progress may need renumbering.
BEGIN;

CREATE TABLE IF NOT EXISTS notebook_shares (
    base_id bigint PRIMARY KEY REFERENCES notebook_bases (id) ON DELETE CASCADE,
    -- 16 random bytes, URL-safe base64.
    link_id text NOT NULL UNIQUE CHECK (link_id ~ '^[A-Za-z0-9_-]{22}$'),
    -- Whether the page offers the base's in-game link.
    include_link boolean NOT NULL DEFAULT true,
    -- Whether the page names the Clasher's players that ran the base.
    show_names boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON notebook_shares FROM PUBLIC;
-- The website shares bases and reads shared ones through the private API.
GRANT SELECT, INSERT, UPDATE, DELETE ON notebook_shares TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations (version) VALUES (125)
ON CONFLICT (version) DO NOTHING;
COMMIT;
