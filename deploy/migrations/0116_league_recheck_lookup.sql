-- Clash Lens deployment migration 0116.
-- Every battle log job looks again at the saved days of each player the log
-- names that lower-league answers stopped (`player_not_eligible`), since
-- #507 (`recheck_battle_players` in league_drop.py). Only about 1 saved day
-- version in 130 has that reason, but with no index on it the lookup read
-- every saved day of each named player this Season: about 1,500 pages a call
-- in production on 14 October 2026, 145 ms on average over 171,096 calls,
-- about 145 ms of each 1,105 ms Reset battle log. It reads more each Season
-- day. This index holds only those day versions. On a copy of production,
-- for 300 of 14 October's Reset battle logs, the lookup read 30 pages a call
-- instead of 1,089 and found the same 61 rows. The index was 184 kB for
-- 6,283 rows, and grew by about 2,400 rows (about 70 kB) a day on 13 and
-- 14 October.
--
-- The weekly league record (stage 4 of that redesign) replaces this re-check
-- and deletes it and this index together.
--
-- The index is built without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time. A build that
-- fails leaves an unusable index and records no version, so the next run
-- drops that index and builds it again. On the copy a build took 0.8 to 4
-- seconds, and another writer took the table's write lock in 10 ms while one
-- ran.
DROP INDEX CONCURRENTLY IF EXISTS ranked_day_versions_not_eligible;

CREATE INDEX CONCURRENTLY ranked_day_versions_not_eligible
    ON ranked_day_versions (player_id, ranked_day_start)
    WHERE failure_reasons ? 'player_not_eligible';

INSERT INTO clash_lens_schema_migrations(version) VALUES (116)
ON CONFLICT (version) DO NOTHING;
