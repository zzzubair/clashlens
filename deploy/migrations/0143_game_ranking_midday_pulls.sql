-- Clash Lens deployment migration 0143.
-- Every country's top 200 is pulled once more at 17:00 UTC, 12 hours after
-- each Reset, only to hand the untracked Legend I players in it to discovery,
-- so they are tracked before the next Reset's sweep and on its first board
-- (game_rankings.py). On 10 November 2026, 197 Legend I players first seen
-- in the lists pulled just after the Reset joined that Reset's board from
-- 05:56. These pulls keep their raw bytes under their own purpose, which the
-- score never reads, and are deleted 28 days after their Reset like the
-- others: about 255 pulls and 4 MB compressed a day, so about 115 MB more at
-- most.
BEGIN;

ALTER TABLE game_ranking_pulls
    DROP CONSTRAINT game_ranking_pulls_purpose_check,
    ADD CONSTRAINT game_ranking_pulls_purpose_check CHECK (purpose IN (
        'locations', 'global_early', 'global_poll', 'global_reset', 'country',
        'country_midday'
    ));

INSERT INTO clash_lens_schema_migrations(version) VALUES (143)
ON CONFLICT (version) DO NOTHING;
COMMIT;
