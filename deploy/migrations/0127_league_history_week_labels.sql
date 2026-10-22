-- Clash Lens deployment migration 0127.
-- A league history row below Legend I is labelled with the Monday Reset
-- starting its week; a Legend I row with the Monday Reset ending its Season
-- (league_week.history_claim). Migration 0115's trigger marked the week
-- before the label for every row, so a Legend II row marked the week before
-- its own. It now marks the row's own week. Every lower league row's week,
-- and the week before it that the worker read it for, is marked again, so the
-- worker decides those players' Seasons again and recalculates the days a
-- changed decision affects.
--
-- CREATE OR REPLACE FUNCTION locks no table. The marks are one
-- INSERT ... SELECT over player_league_history_entries, which takes only
-- the lock any insert takes on league_week_dirty: on the copy of 14 October
-- 2026, 10,690 marks for 4,434 players, selected in 1.9 s from 144,597
-- history rows. The worker's league week thread then decides those
-- players' Seasons again, in minutes.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_mark_league_week_history()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    INSERT INTO league_week_dirty (player_id, week_start, source)
    SELECT DISTINCT player_id,
           clashlens_week_start(to_timestamp(league_season_id::bigint)
               - CASE WHEN league_tier_id = 105000036 THEN interval '7 days'
                      ELSE interval '0 days' END),
           'league_history'
    FROM new_rows;
    RETURN NULL;
END $$;

DO $$
BEGIN
    EXECUTE format('ALTER FUNCTION %I.clashlens_mark_league_week_history()'
                   ' SET search_path TO pg_catalog, %I, pg_temp',
                   current_schema(), current_schema());
    EXECUTE format('REVOKE ALL ON FUNCTION %I.clashlens_mark_league_week_history() FROM PUBLIC',
                   current_schema());
END $$;

INSERT INTO league_week_dirty (player_id, week_start, source)
SELECT DISTINCT history.player_id, clashlens_week_start(week.at), 'league_history'
FROM player_league_history_entries AS history
CROSS JOIN LATERAL (VALUES
    (to_timestamp(history.league_season_id::bigint)),
    (to_timestamp(history.league_season_id::bigint) - interval '7 days')) AS week (at)
WHERE history.league_tier_id IS DISTINCT FROM 105000036
  AND to_timestamp(history.league_season_id::bigint) >= timestamptz '2026-09-28 05:00:00+00'
ORDER BY 1, 2;

INSERT INTO clash_lens_schema_migrations(version) VALUES (127)
ON CONFLICT (version) DO NOTHING;
COMMIT;
