-- Season replay metrics: every Daily board and saved day of one Season.
-- Run by development/season-replay with :season_start and :label set.
-- It writes nothing but a temporary table. Output is three tab-separated
-- tables: boards, reasons, days.
\pset footer off

-- The board each Reset shows (the newest published frozen version), and the
-- frozen input row of every Reset member with their entry and saved day.
CREATE TEMP TABLE season_board_cache AS
WITH board AS (
    SELECT sweep.boundary_at, shown.snapshot_id, shown.snapshot_version
    FROM collector_reset_sweeps AS sweep
    CROSS JOIN LATERAL (
        SELECT DISTINCT snapshot_id, snapshot_version
        FROM clashlens_game_ranking_board(sweep.boundary_at)
    ) AS shown
    WHERE sweep.boundary_at > :season_start
      AND sweep.boundary_at <= :season_start::timestamptz + interval '28 days'
)
SELECT board.boundary_at, board.snapshot_id, board.snapshot_version,
       member.player_id, member.input_identity ->> 'snapshot_quality' AS quality,
       entry.position, entry.confidence,
       day.state AS day_state, day.failure_reasons
FROM board
CROSS JOIN LATERAL (
    SELECT snapshot_manifest_id FROM boundary_publication_generations
    WHERE snapshot_id = board.snapshot_id AND snapshot_manifest_id IS NOT NULL
    ORDER BY generation DESC LIMIT 1
) AS generation
CROSS JOIN LATERAL boundary_publication_manifest_entries(generation.snapshot_manifest_id) AS member
LEFT JOIN leaderboard_snapshot_entries AS entry
  ON entry.snapshot_id = board.snapshot_id AND entry.player_id = member.player_id
LEFT JOIN ranked_day_versions AS day ON day.id = member.ranked_day_version_id;

-- The column names and their order follow section 2 of the boards state
-- report of 14 October 2026, so the two can be compared. A Partial entry is
-- counted once: no start reading first, then automatic loss unknown.
WITH counted AS (
    SELECT boundary_at, snapshot_id, snapshot_version,
           count(*) AS members,
           count(position) AS on_board,
           count(*) FILTER (WHERE position IS NOT NULL AND confidence = 'confirmed') AS confirmed,
           count(*) FILTER (WHERE position IS NOT NULL AND confidence = 'uncertain') AS uncertain,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state = 'Complete') AS day_complete,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state IN ('Inconsistent', 'Malformed')) AS inconsistent,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state = 'Partial'
                            AND failure_reasons ? 'automatic_defense_basis_unavailable'
                            AND NOT failure_reasons ? 'missing_start_baseline') AS partial_loss_unknown,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state = 'Partial'
                            AND failure_reasons ? 'missing_start_baseline') AS partial_no_start,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state = 'Partial'
                            AND NOT failure_reasons ?| array['missing_start_baseline', 'automatic_defense_basis_unavailable']) AS partial_other,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state IS DISTINCT FROM 'Complete'
                            AND day_state IS DISTINCT FROM 'Partial'
                            AND day_state NOT IN ('Inconsistent', 'Malformed')) AS no_saved_day,
           count(*) FILTER (WHERE position IS NOT NULL AND day_state = 'Complete' AND confidence = 'uncertain') AS complete_unproven,
           count(*) FILTER (WHERE position IS NOT NULL AND (confidence = 'uncertain' OR day_state IS DISTINCT FROM 'Complete')) AS not_proven,
           count(*) FILTER (WHERE position IS NULL AND quality = 'invalid') AS left_off_not_legend_i,
           count(*) FILTER (WHERE position IS NULL AND quality = 'profile_not_found') AS left_off_profile_not_found,
           count(*) FILTER (WHERE position IS NULL AND quality NOT IN ('invalid', 'profile_not_found')) AS left_off_other
    FROM season_board_cache
    GROUP BY boundary_at, snapshot_id, snapshot_version
),
score AS (
    SELECT DISTINCT ON (snapshot_id) snapshot_id, status, score
    FROM game_ranking_scores ORDER BY snapshot_id, scored_at DESC
)
SELECT :'label' AS run,
       to_char(counted.boundary_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS reset,
       to_char((counted.boundary_at - interval '1 day') AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS legend_day,
       counted.snapshot_id || ' v' || counted.snapshot_version AS board,
       on_board, inconsistent, partial_loss_unknown, partial_no_start, partial_other,
       complete_unproven, not_proven,
       round(100.0 * not_proven / nullif(on_board, 0), 2) AS not_proven_pct,
       left_off_not_legend_i, left_off_profile_not_found,
       members, confirmed, uncertain, day_complete, no_saved_day, left_off_other,
       -- The game's global top 200 and country lists, as the collector scores them.
       score.status AS game_score,
       score.score ->> 'global_on_board' AS global_on_board,
       score.score ->> 'global_exact_rank' AS global_exact_rank,
       score.score ->> 's2_global_inside_trophy_group' AS global_inside_ties,
       score.score ->> 's1_country_order_breaks_per_100k' AS country_breaks_per_100k,
       score.score ->> 's3_confirmed_entries_contradicted' AS confirmed_contradicted,
       score.score ->> 's5_live_trophies_match_percent' AS live_trophies_match_pct
FROM counted
LEFT JOIN score ON score.snapshot_id = counted.snapshot_id
ORDER BY counted.boundary_at;

-- Every reason behind the board counts: each on-board entry's day state and
-- its reasons, and each left-off member's board quality.
SELECT :'label' AS run,
       to_char(boundary_at AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS reset, kind, reason, count(*) AS entries
FROM (
    SELECT boundary_at, 'on_board:' || coalesce(day_state, 'no_saved_day') AS kind,
           coalesce(reason, '-') AS reason
    FROM season_board_cache
    LEFT JOIN LATERAL jsonb_array_elements_text(failure_reasons) AS reason ON true
    WHERE position IS NOT NULL
    UNION ALL
    SELECT boundary_at, 'left_off', coalesce(quality, 'none')
    FROM season_board_cache WHERE position IS NULL
) AS reasons
GROUP BY 2, 3, 4 ORDER BY 2, 3, 4;

-- Each Legend day of the Season: every player's newest saved day by state.
WITH newest AS (
    SELECT DISTINCT ON (player_id, ranked_day_start) ranked_day_start, state
    FROM ranked_day_versions
    WHERE ranked_day_start >= :season_start
      AND ranked_day_start < :season_start::timestamptz + interval '28 days'
    ORDER BY player_id, ranked_day_start, id DESC
)
SELECT :'label' AS run,
       to_char(ranked_day_start AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS legend_day,
       count(*) AS saved,
       count(*) FILTER (WHERE state = 'Complete') AS complete,
       count(*) FILTER (WHERE state = 'Partial') AS partial,
       count(*) FILTER (WHERE state = 'Inconsistent') AS inconsistent,
       count(*) FILTER (WHERE state = 'Malformed') AS malformed,
       count(*) FILTER (WHERE state = 'Live') AS live,
       round(100.0 * count(*) FILTER (WHERE state = 'Complete') / count(*), 2) AS complete_pct
FROM newest GROUP BY ranked_day_start ORDER BY ranked_day_start;
