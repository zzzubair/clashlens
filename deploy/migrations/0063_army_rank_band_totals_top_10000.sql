-- Clash Lens deployment migration 0063.
-- Saved army totals per rank band reach rank 10,000.
--
-- The Armies page adds Top 2,000, 5,000 and 10,000 and the rank ranges
-- 1,001-2,000, 2,001-5,000 and 5,001-10,000. The worker saves totals for
-- those three bands too, 17 in all, so their first positions go past 1,000.
-- The worker notices the missing bands and counts them on its next check.
BEGIN;

ALTER TABLE army_analytics_rank_band_totals
    DROP CONSTRAINT IF EXISTS army_analytics_rank_band_totals_first_position_check;
ALTER TABLE army_analytics_rank_band_totals
    ADD CONSTRAINT army_analytics_rank_band_totals_first_position_check
    CHECK (first_position BETWEEN 1 AND 10000);

INSERT INTO clash_lens_schema_migrations(version) VALUES (63)
ON CONFLICT (version) DO NOTHING;
COMMIT;
