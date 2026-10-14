-- Clash Lens deployment migration 0118.
-- The weekly league record, stage 4: tracking and Daily board membership
-- follow the record (python/src/clashlens/league_days.py track,
-- boundary_members.py), so the battle log's re-check of players stopped by
-- lower-league answers (#507) is deleted with the rest of league_drop.py.
-- Migration 0116's index served only that re-check's lookup of saved days
-- marked `player_not_eligible`; nothing reads it now. On a copy of
-- production on 14 October 2026 it was 184 kB and grew by about 70 kB a day.
--
-- The index is dropped without blocking writes, so this file runs outside a
-- transaction: ./ops sends it to psql one statement at a time.
DROP INDEX CONCURRENTLY IF EXISTS ranked_day_versions_not_eligible;

INSERT INTO clash_lens_schema_migrations(version) VALUES (118)
ON CONFLICT (version) DO NOTHING;
