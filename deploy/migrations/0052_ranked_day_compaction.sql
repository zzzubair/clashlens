-- Delete the extra saved copies of each player's ended Legend days. Every
-- battle saves a complete new copy of the player's day result and its daily
-- log; on 2026-10-02 that was about 13 copies per player-day, 1.5-2 GB a day,
-- and only the newest is shown. A copy is kept when it is the newest, when
-- the newest daily log of its day points at it, when a publication, analytics
-- row or queued publication correction points at it, or when the following
-- day's newest copy was built from it. Every other copy of an ended day goes,
-- with its daily log and adjustments. A kept copy that named a deleted copy
-- as the one it replaced names the nearest older kept copy instead, or none.
--
-- clashlens_compact_ranked_days does one bounded batch: up to player_limit
-- players of the oldest day that has copies no finished pass has covered,
-- plus the day before it, whose copies kept for the following day can go once
-- that day's newest copy moved on. The caller says which ended days are ready;
-- the open Legend day is never touched. The worker role may call it but holds
-- no DELETE privilege itself.
--
-- Deleting a copy makes PostgreSQL look for rows still pointing at it. Four
-- pointing columns had no index, so each deleted copy would read those whole
-- tables. The partial copy-to-copy index holds only kept copies that name an
-- earlier copy: about 7 MB per 330,000 rows before the first cleanup, and
-- little after it. The others grow with their tables: about 0.3 MB a day for
-- the 13,000 publication members, and about 4 MB a day for army facts once
-- army publication runs (empty on 2026-10-03).
--
-- `up` applies this while services are stopped. The copy-to-copy index reads
-- ranked_day_versions once (461 MB of rows on 2026-10-03).
BEGIN;

CREATE INDEX IF NOT EXISTS ranked_day_versions_replaces
    ON ranked_day_versions (replaces_version_id)
    WHERE replaces_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS boundary_publication_generation_members_ranked_day
    ON boundary_publication_generation_members (ranked_day_version_id)
    WHERE ranked_day_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS boundary_publication_manifest_rows_ranked_day
    ON boundary_publication_manifest_rows (ranked_day_version_id)
    WHERE ranked_day_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS army_analytics_battle_facts_ranked_day
    ON army_analytics_battle_facts (source_ranked_day_version_id);

-- One row per Legend day that a cleanup pass has started.
CREATE TABLE IF NOT EXISTS ranked_day_compactions (
    ranked_day_start timestamptz PRIMARY KEY,
    -- The day's newest copy when the running pass started; NULL between passes.
    pass_through_id bigint,
    -- The running pass has finished every player up to this id.
    after_player_id bigint NOT NULL DEFAULT 0,
    -- Every copy up to this id was there when a finished pass started. A
    -- newer copy, such as a late correction, starts another pass.
    compacted_through_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION clashlens_compact_ranked_days(
    ready_through timestamptz, player_limit integer
)
RETURNS TABLE (
    compacted_day timestamptz, checked_players integer,
    deleted_versions integer, deleted_logs integer, day_finished boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
-- A live recalculation locks its day's newest copy, which a batch may update.
-- Give up quickly instead of holding the worker; the next batch retries.
SET lock_timeout = '2s'
AS $$
DECLARE
    target_day timestamptz;
    newest_id bigint;
    progress ranked_day_compactions%ROWTYPE;
    batch_players bigint[];
    doomed bigint[];
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

    SELECT candidate.start, candidate.newest_id INTO target_day, newest_id
    FROM generate_series(
        (SELECT min(version.ranked_day_start) FROM ranked_day_versions AS version),
        ready_through - interval '24 hours',
        interval '24 hours'
    ) AS series(start)
    CROSS JOIN LATERAL (
        SELECT series.start,
               (SELECT max(version.id) FROM ranked_day_versions AS version
                WHERE version.ranked_day_start = series.start) AS newest_id
    ) AS candidate
    LEFT JOIN ranked_day_compactions AS done ON done.ranked_day_start = candidate.start
    WHERE candidate.newest_id > coalesce(done.compacted_through_id, 0)
    ORDER BY candidate.start
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

    WITH saved AS (
        SELECT version.id, version.player_id, version.ranked_day_start,
               version.reconciliation_rule_version,
               version.version < max(version.version) OVER (
                   PARTITION BY version.player_id, version.ranked_day_start,
                                version.reconciliation_rule_version
               ) AS superseded
        FROM ranked_day_versions AS version
        WHERE version.player_id = ANY(batch_players)
          AND version.ranked_day_start IN (target_day - interval '24 hours', target_day)
    )
    SELECT coalesce(array_agg(saved.id), '{}') INTO doomed
    FROM saved
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
REVOKE ALL ON FUNCTION clashlens_compact_ranked_days(timestamptz, integer)
    FROM PUBLIC, clashlens_collector, clashlens_python_api;
GRANT EXECUTE ON FUNCTION clashlens_compact_ranked_days(timestamptz, integer)
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations (version) VALUES (52)
ON CONFLICT (version) DO NOTHING;
COMMIT;
