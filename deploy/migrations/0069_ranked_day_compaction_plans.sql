-- Clash Lens deployment migration 0069.
-- Make each saved-copy cleanup batch (migrations 0052 and 0067) finish well
-- inside its 5-second limit. Since 0067, every run on production timed out on
-- October 2's first batch and made no progress. Two causes:
-- 1. The check that keeps a copy made current again, and the copy it
--    replaced, was worked out again for every deletable copy. MATERIALIZED
--    works it out once per batch: 25 players took over 5 s, now about 0.3 s.
-- 2. From the 6th batch on one connection, PostgreSQL reuses one plan made
--    without the batch's players and day, and that plan compares each copy
--    with every queued correction input one at a time (over 5 s again).
--    plan_cache_mode = force_custom_plan plans each batch for its own
--    players and day, at about 12 ms of planning per batch.
-- Nothing else changes: the same copies are kept, the same players make a
-- batch, and the days take turns as before.
BEGIN;

CREATE OR REPLACE FUNCTION clashlens_compact_ranked_days(
    ready_through timestamptz, player_limit integer
)
RETURNS TABLE (
    compacted_day timestamptz, checked_players integer,
    deleted_versions integer, deleted_logs integer, day_finished boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
-- A batch takes the lock a recalculation takes for each of its player-days.
-- Give up quickly instead of holding the worker; the next batch retries.
SET lock_timeout = '2s'
-- Plan each batch for its own players and day (see the top of this file).
SET plan_cache_mode = force_custom_plan
AS $$
DECLARE
    target_day timestamptz;
    newest_id bigint;
    progress ranked_day_compactions%ROWTYPE;
    batch_players bigint[];
    doomed bigint[];
    lock_key text;
BEGIN
    IF player_limit IS NULL OR player_limit NOT BETWEEN 1 AND 1000 THEN
        RAISE EXCEPTION 'player_limit must be between 1 and 1000' USING ERRCODE = '22023';
    END IF;
    -- Days ending at or before this boundary may be cleaned, never the open day.
    ready_through := least(
        ready_through,
        date_bin('24 hours', clock_timestamp(), timestamptz '2000-01-01 05:00+00')
    );
    -- One batch at a time; an overlapping call does nothing.
    IF NOT pg_try_advisory_xact_lock(hashtext('clashlens_compact_ranked_days')) THEN
        RETURN;
    END IF;

    -- Each batch goes to the ready day that has waited longest for one: a day
    -- no pass has started first, then the day whose last batch is oldest.
    -- A day ends 24 hours after it starts; ranked_day_versions_daily_selector
    -- (ranked_day_end, id DESC) answers each lookup from the index.
    SELECT candidate.start, candidate.newest_id INTO target_day, newest_id
    FROM generate_series(
        (SELECT min(version.ranked_day_end) FROM ranked_day_versions AS version)
            - interval '24 hours',
        ready_through - interval '24 hours',
        interval '24 hours'
    ) AS series(start)
    CROSS JOIN LATERAL (
        SELECT series.start,
               (SELECT max(version.id) FROM ranked_day_versions AS version
                WHERE version.ranked_day_end = series.start + interval '24 hours')
                   AS newest_id
    ) AS candidate
    LEFT JOIN ranked_day_compactions AS done ON done.ranked_day_start = candidate.start
    WHERE candidate.newest_id > coalesce(done.compacted_through_id, 0)
    ORDER BY done.updated_at NULLS FIRST, candidate.start
    LIMIT 1;
    IF target_day IS NULL THEN
        RETURN;
    END IF;

    INSERT INTO ranked_day_compactions (ranked_day_start) VALUES (target_day)
    ON CONFLICT (ranked_day_start) DO NOTHING;
    SELECT * INTO progress FROM ranked_day_compactions AS compaction
    WHERE compaction.ranked_day_start = target_day FOR UPDATE;
    IF progress.pass_through_id IS NULL THEN
        progress.pass_through_id := newest_id;
        progress.after_player_id := 0;
    END IF;

    SELECT coalesce(array_agg(player.id ORDER BY player.id), '{}') INTO batch_players
    FROM (
        SELECT player.id FROM players AS player
        WHERE player.id > progress.after_player_id
        ORDER BY player.id
        LIMIT player_limit
    ) AS player;

    -- Wait for any recalculation of these player-days and keep new ones out
    -- until this batch commits, so the choice below sees every copy saved.
    -- The key is the one reconciliation_db builds, with the day start in
    -- Python's isoformat; taken in the late-battle sweep's order.
    FOR lock_key IN
        SELECT 'ranked-day:' || batch.player_id || ':'
               || to_char(batch_day.start AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS')
               || '+00:00'
        FROM unnest(batch_players) AS batch(player_id)
        CROSS JOIN (VALUES (target_day - interval '24 hours'), (target_day))
            AS batch_day(start)
        ORDER BY batch.player_id, batch_day.start
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(lock_key, 0));
    END LOOP;

    WITH saved AS (
        SELECT version.id, version.player_id, version.ranked_day_start,
               version.reconciliation_rule_version, version.result_hash,
               version.replaces_version_id,
               version.version < max(version.version) OVER (
                   PARTITION BY version.player_id, version.ranked_day_start,
                                version.reconciliation_rule_version
               ) AS superseded
        FROM ranked_day_versions AS version
        WHERE version.player_id = ANY(batch_players)
          AND version.ranked_day_start IN (target_day - interval '24 hours', target_day)
    ), unneeded AS (
        SELECT saved.id FROM saved
        WHERE saved.superseded
          AND NOT EXISTS (
              SELECT 1 FROM (
                  SELECT log.ranked_day_version_id
                  FROM api_player_daily_logs AS log
                  WHERE log.player_id = saved.player_id
                    AND log.ranked_day_start = saved.ranked_day_start
                  ORDER BY log.version DESC
                  LIMIT 1
              ) AS newest_log
              WHERE newest_log.ranked_day_version_id = saved.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM (
                  SELECT later.input_evidence -> 'previous_day' ->> 'version_id' AS built_from
                  FROM ranked_day_versions AS later
                  WHERE later.player_id = saved.player_id
                    AND later.ranked_day_start = saved.ranked_day_start + interval '24 hours'
                    AND later.reconciliation_rule_version = saved.reconciliation_rule_version
                  ORDER BY later.version DESC
                  LIMIT 1
              ) AS next_day
              WHERE next_day.built_from = saved.id::text
          )
          AND NOT EXISTS (
              SELECT 1 FROM boundary_publication_generation_members AS member
              WHERE member.ranked_day_version_id = saved.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM boundary_publication_manifest_rows AS manifest_row
              WHERE manifest_row.ranked_day_version_id = saved.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM boundary_publication_corrections AS correction
              CROSS JOIN LATERAL jsonb_array_elements(correction.pending_inputs) AS pending(input)
              WHERE pending.input ->> 'ranked_day_version_id' = saved.id::text
          )
          AND NOT EXISTS (
              SELECT 1 FROM leaderboard_snapshots AS snapshot
              WHERE snapshot.source_ranked_day_version_id = saved.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM analytics_summaries AS summary
              WHERE summary.source_ranked_day_version_id = saved.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM army_analytics_battle_facts AS fact
              WHERE fact.source_ranked_day_version_id = saved.id
          )
    ), restored AS MATERIALIZED (
        -- Kept copies that made an earlier result current again, with the
        -- copy each replaced and the copy holding that earlier result.
        SELECT restoring.replaces_version_id, original.id AS original_id
        FROM saved AS restoring
        JOIN ranked_day_versions AS original
          ON original.player_id = restoring.player_id
         AND original.ranked_day_start = restoring.ranked_day_start
         AND original.reconciliation_rule_version = restoring.reconciliation_rule_version
        WHERE restoring.replaces_version_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM unneeded WHERE unneeded.id = restoring.id)
          AND restoring.result_hash = encode(sha256(convert_to(
              original.result_hash || ':restores-over:' || restoring.replaces_version_id,
              'UTF8'
          )), 'hex')
    )
    SELECT coalesce(array_agg(unneeded.id), '{}') INTO doomed
    FROM unneeded
    WHERE NOT EXISTS (
        SELECT 1 FROM restored
        WHERE unneeded.id IN (restored.replaces_version_id, restored.original_id)
    );

    deleted_versions := 0;
    deleted_logs := 0;
    IF cardinality(doomed) > 0 THEN
        DELETE FROM api_player_daily_logs AS log WHERE log.ranked_day_version_id = ANY(doomed);
        GET DIAGNOSTICS deleted_logs = ROW_COUNT;
        DELETE FROM ranked_day_adjustments AS adjustment
        WHERE adjustment.ranked_day_version_id = ANY(doomed);
        UPDATE ranked_day_versions AS kept
        SET replaces_version_id = (
            SELECT older.id FROM ranked_day_versions AS older
            WHERE older.player_id = kept.player_id
              AND older.ranked_day_start = kept.ranked_day_start
              AND older.reconciliation_rule_version = kept.reconciliation_rule_version
              AND older.version < kept.version
              AND older.id <> ALL(doomed)
            ORDER BY older.version DESC
            LIMIT 1
        )
        WHERE kept.replaces_version_id = ANY(doomed) AND kept.id <> ALL(doomed);
        DELETE FROM ranked_day_versions AS version WHERE version.id = ANY(doomed);
        GET DIAGNOSTICS deleted_versions = ROW_COUNT;
    END IF;

    day_finished := cardinality(batch_players) < player_limit;
    IF day_finished THEN
        UPDATE ranked_day_compactions AS compaction
        SET compacted_through_id = progress.pass_through_id, pass_through_id = NULL,
            after_player_id = 0, updated_at = clock_timestamp()
        WHERE compaction.ranked_day_start = target_day;
    ELSE
        UPDATE ranked_day_compactions AS compaction
        SET pass_through_id = progress.pass_through_id,
            after_player_id = batch_players[cardinality(batch_players)],
            updated_at = clock_timestamp()
        WHERE compaction.ranked_day_start = target_day;
    END IF;
    compacted_day := target_day;
    checked_players := cardinality(batch_players);
    RETURN NEXT;
END
$$;

DO $$
DECLARE runtime_schema_name text := current_schema();
BEGIN
    EXECUTE format(
        'ALTER FUNCTION %I.clashlens_compact_ranked_days(timestamptz, integer) SET search_path TO pg_catalog, %I, pg_temp',
        runtime_schema_name, runtime_schema_name
    );
END
$$;

INSERT INTO clash_lens_schema_migrations (version) VALUES (69)
ON CONFLICT (version) DO NOTHING;
COMMIT;
