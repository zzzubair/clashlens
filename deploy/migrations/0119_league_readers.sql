-- Clash Lens deployment migration 0119.
-- The weekly league record, stage 3: every reader asks the record which
-- league a day or a player is in, through two views, and the data service
-- loses its direct read of the saved days so no reader can ask anything
-- else (docs/domain.md, Weekly league record).
--
-- legend_days: the saved days a reader may show, every version of each. A
-- day in a week the record decided below Legend I is left out. A day in a
-- week the record has not decided as Legend I (unknown, or not decided yet)
-- is kept, because the player may well have been in Legend I, and carries
-- the reason league_not_confirmed so every reader shows it as not
-- confirmed. league says which: 'legend_i' or 'unknown'.
--
-- player_leagues_now: each player's league this week, from the record; a
-- week the record has not decided is 'unknown'.
--
-- Both are plain views, read with the migration owner's rights; they hold
-- no rows and cost nothing to keep.
BEGIN;

CREATE OR REPLACE VIEW legend_days AS
SELECT day.id, day.player_id, day.ranked_day_start, day.version, day.state,
       day.coverage, day.adjustments, day.battles,
       CASE WHEN week.league = 'legend_i'
                 OR day.partial_reasons ? 'league_not_confirmed'
            THEN day.partial_reasons
            ELSE day.partial_reasons || '["league_not_confirmed"]'::jsonb
       END AS partial_reasons,
       day.published_at, day.ranked_day_end, day.official_season_id,
       day.season_day_number, day.confidence, day.attack_count,
       day.attack_three_star_count, day.attack_gain, day.defense_count,
       day.defense_three_star_count, day.defense_loss, day.net_trophy_change,
       day.ranked_day_version_id,
       COALESCE(week.league, 'unknown') AS league
FROM api_player_daily_logs AS day
LEFT JOIN player_league_weeks AS week
  ON week.player_id = day.player_id
 AND week.week_start = clashlens_week_start(day.ranked_day_start)
WHERE week.league IS DISTINCT FROM 'below_legend_i';

CREATE OR REPLACE VIEW player_leagues_now AS
SELECT player.id AS player_id, COALESCE(this_week.league, 'unknown') AS league
FROM players AS player
LEFT JOIN player_league_weeks AS this_week
  ON this_week.player_id = player.id
 AND this_week.week_start = clashlens_week_start(now());

REVOKE ALL PRIVILEGES ON TABLE legend_days, player_leagues_now FROM PUBLIC;
GRANT SELECT ON TABLE legend_days, player_leagues_now
    TO clashlens_python_api, clashlens_python_worker;
REVOKE ALL PRIVILEGES ON TABLE api_player_daily_logs FROM clashlens_python_api;
-- A Season summary whose every day is in a week below Legend I has no
-- Legend day left to summarize; the worker deletes it.
GRANT DELETE ON TABLE player_season_summaries TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (119)
ON CONFLICT (version) DO NOTHING;
COMMIT;
