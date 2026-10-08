-- Clash Lens deployment migration 0086.
-- Equal trophies on the Live and Daily boards follow one order
-- (python/src/clashlens/analytics.py tie_order_key): the higher Season
-- average attack destruction first, players with no recorded attack last,
-- then more attacks, then MD5 of the tag, then the tag.
--
-- The Live board reads each player's current-Season attacks and summed
-- attack destruction here. The worker recounts them from recorded battles
-- every five minutes and overwrites each player's row when a Season
-- changes, so the table holds one row per player ever counted (about 12,000
-- on 8 October 2026, under 2 MB) and does not grow with Seasons. The Daily
-- board freezes the same counts, cut at its Reset, with its other inputs.
--
-- The Daily board's tag hash becomes MD5 (32 hex characters). Boards
-- already published keep their SHA-256 hashes, which the old check already
-- proved; NOT VALID skips reading their 4.6 million rows again.
BEGIN;

CREATE TABLE IF NOT EXISTS live_attack_tallies (
    player_id bigint PRIMARY KEY REFERENCES players (id),
    official_season_id text NOT NULL CHECK (official_season_id <> ''),
    attacks integer NOT NULL CHECK (attacks >= 0),
    destruction integer NOT NULL CHECK (destruction BETWEEN 0 AND attacks * 100),
    refreshed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

REVOKE ALL ON live_attack_tallies FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON live_attack_tallies TO clashlens_python_worker;
GRANT SELECT ON live_attack_tallies TO clashlens_python_api;

ALTER TABLE leaderboard_snapshot_entries
    DROP CONSTRAINT IF EXISTS leaderboard_snapshot_entries_tie_hash_check;
ALTER TABLE leaderboard_snapshot_entries
    ADD CONSTRAINT leaderboard_snapshot_entries_tie_hash_check
    CHECK (tie_hash ~ '^([0-9a-f]{32}|[0-9a-f]{64})$') NOT VALID;

INSERT INTO clash_lens_schema_migrations(version) VALUES (86)
ON CONFLICT (version) DO NOTHING;
COMMIT;
