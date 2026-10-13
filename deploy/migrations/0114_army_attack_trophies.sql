-- Clash Lens deployment migration 0114.
-- Each completed Legend day's attack counts per player and distinct army
-- (army_types.py) also save the trophies those attacks won, from the game's
-- Legend trophy table, as the defense counts save trophies_lost (0106). The
-- Armies page shows them as Avg. trophies.
--
-- One more 4-byte number a row: about 35,000 rows a day, so about 140 kB a
-- day and 4 MB for a 28-day Season. Days counted before this migration keep
-- an empty (NULL) value; nothing reads it, because the next statement makes
-- those days uncounted.
--
-- Forgetting which days were counted makes the worker count them again,
-- newest first, one day each run of its 10-second maintenance timer outside
-- 04:30 to 07:00 UTC: the current Season and the last 7 days, about 3
-- minutes. Until a day is counted again the Armies page adds it up from its
-- attacks, with the same numbers. Older days stay uncounted; nothing shows
-- army types for them.
--
-- Numbered 0114, leaving 0113 to another open change: ./ops applies every
-- migration the database lacks, in file order, so a gap is fine and either
-- can merge first.
BEGIN;

ALTER TABLE army_package_day_counts ADD COLUMN IF NOT EXISTS trophies integer;
DELETE FROM army_type_counted_days;

INSERT INTO clash_lens_schema_migrations(version) VALUES (114)
ON CONFLICT (version) DO NOTHING;
COMMIT;
