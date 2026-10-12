-- Clash Lens deployment migration 0104.
-- Trophies in each completed Legend day's army counts (army_types.py), so
-- the dashboard's army cards compare a player with every tracked player from
-- the saved counts instead of reading each battle's army: about 95 seconds a
-- day on 12 Oct 2026, against a 35,000-row read.
--
-- attacker_gain is the trophies the attacker won; defender_loss the trophies
-- the defenders lost, which leaves out the 0 to 4 trophies a 0-star attack
-- wins. Both are NULL on rows counted before this migration: about 8 bytes a
-- row, 280 kB a day and 8 MB a 28-day Season, kept like the other counts.
--
-- with_trophies marks a day counted with them. The worker counts the current
-- Season's unmarked days again, newest first, one a run, as it counts each
-- new day; the Armies page keeps reading their earlier counts meanwhile.
BEGIN;

ALTER TABLE army_package_day_counts
    ADD COLUMN IF NOT EXISTS attacker_gain integer,
    ADD COLUMN IF NOT EXISTS defender_loss integer;

ALTER TABLE army_type_counted_days
    ADD COLUMN IF NOT EXISTS with_trophies boolean NOT NULL DEFAULT false;

INSERT INTO clash_lens_schema_migrations(version) VALUES (104)
ON CONFLICT (version) DO NOTHING;

COMMIT;
