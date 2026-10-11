-- Clash Lens deployment migration 0097.
-- Freezing a Reset's Daily board inputs finds each member's newest accepted
-- profile at the Reset. It read every one of the member's profile versions
-- through the (player, semantic projection) index and then every version's
-- table row, scattered across the table, and every sighting's row too. On
-- 10 October 2026 that one statement ran for over six minutes, waiting on
-- disk reads, while the Daily board waited for the freeze.
--
-- The freeze now takes each version's newest sighting from the sightings'
-- own time index, and this index lists each player's accepted versions with
-- their time, so both lookups read only index pages. On a synthetic copy of
-- 13,400 members with 1.99 million profile versions and 3.29 million
-- sightings, from an empty cache, the statement read 37,091 instead of
-- 676,597 pages from disk and took 9 to 11 seconds instead of 51 to 117.
-- Sightings saved since the table's last vacuum still need their table row:
-- with none vacuumed it read 89,075 pages and took 20 to 55 seconds.
--
-- The index holds one entry of about 40 bytes per accepted profile version:
-- 76 MB for those 1.99 million. Each profile response adds at most one
-- version, and one more each time a new parser replays it; on 2 October 2026
-- production saved about 180,000 a day, so it grows by about 7 MB a day at
-- most, outside replays. On that copy, from an empty cache, the
-- build took 3.8 seconds.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that unusable index and builds it again.
DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_index WHERE indexrelid = to_regclass('player_profile_versions_accepted_player_time') AND NOT indisvalid) THEN DROP INDEX player_profile_versions_accepted_player_time; END IF; END $$;

CREATE INDEX CONCURRENTLY IF NOT EXISTS player_profile_versions_accepted_player_time
    ON player_profile_versions (player_id, observed_at DESC, id DESC)
    WHERE source_contract_state = 'accepted';

INSERT INTO clash_lens_schema_migrations(version) VALUES (97)
ON CONFLICT (version) DO NOTHING;
