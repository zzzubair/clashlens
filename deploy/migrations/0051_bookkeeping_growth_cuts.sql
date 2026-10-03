-- Slow database growth from internal bookkeeping only. On 2026-10-02 the
-- database grew about 8.6 GB a day. No existing row is deleted or rewritten.
--
-- 1. Discovery rows: every battle-log fetch re-recorded about 32 opponents,
--    about 6 million rows (1.1 GB) a day, and no code reads them. New rows are
--    now kept once per player and source. Older rows stay, so the rule covers
--    only rows added after this migration; a player already recorded may get
--    one more row per source. Building the index reads the whole table once
--    but indexes only new rows: 1.6 seconds locally on 15 million rows
--    (1.3 GB), about production's size.
-- 2. Battle-log membership: each fetch re-listed about 48 battles as 48 rows,
--    about 8.8 million rows (1.0 GB) a day, though about 47 were already
--    listed by the previous fetch. New fetches store one row holding the list
--    of battle source ids, in log order. Position N holds the log's row N - 1;
--    an empty position is a battle whose season detail was retired. The view
--    returns the same rows for both storage shapes.
-- 3. Duplicate indexes: archive_catalogue_current_hash (303 MB) served no
--    query that the composite unique index on (response_hash,
--    archive_reference) cannot, including the archive retention trigger.
--    python_processing_attempts_job_order_v2 (56 MB) repeats the unique
--    (job_id, attempt_number) constraint index.
--
-- `up` applies this while services are stopped; apart from the discovery index
-- build, every step changes only the catalogue.
BEGIN;

DO $$
BEGIN
    EXECUTE format(
        'CREATE UNIQUE INDEX IF NOT EXISTS known_player_discoveries_first_seen '
        'ON known_player_discoveries (player_id, source_kind) WHERE id > %s',
        (SELECT coalesce(max(id), 0) FROM known_player_discoveries)
    );
END
$$;

CREATE TABLE IF NOT EXISTS battle_payload_row_lists (
    parsed_payload_id bigint NOT NULL
        REFERENCES parsed_source_payloads(id) ON DELETE CASCADE,
    reporting_player_id bigint NOT NULL REFERENCES players(id),
    source_row_ids bigint[] NOT NULL CHECK (
        array_ndims(source_row_ids) = 1 AND array_lower(source_row_ids, 1) = 1
        AND cardinality(source_row_ids) <= 50
    ),
    PRIMARY KEY (parsed_payload_id, reporting_player_id)
);
-- Each battle-log job looks up up to 50 sources here before processing. A
-- pending insert list would be rescanned by every lookup, so insert directly.
CREATE INDEX IF NOT EXISTS battle_payload_row_lists_source
    ON battle_payload_row_lists USING gin (source_row_ids)
    WITH (fastupdate = off);
GRANT SELECT, INSERT ON battle_payload_row_lists TO clashlens_python_worker;

-- An array cannot carry a foreign key. Keep the restrictive reference the
-- per-row table had: a listed battle source cannot be deleted.
CREATE OR REPLACE FUNCTION clashlens_listed_battle_source_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM battle_payload_row_lists
        WHERE source_row_ids @> ARRAY[OLD.id]
    ) THEN
        RAISE EXCEPTION 'battle source row % is still listed by a battle log', OLD.id
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN OLD;
END $$;
DROP TRIGGER IF EXISTS battle_source_rows_listed_guard ON battle_source_rows;
CREATE TRIGGER battle_source_rows_listed_guard
BEFORE DELETE ON battle_source_rows
FOR EACH ROW EXECUTE FUNCTION clashlens_listed_battle_source_guard();

CREATE OR REPLACE VIEW battle_log_observation_source_rows AS
SELECT source.battle_log_observation_id,
       source.id AS source_row_id,
       NULL::bigint AS observation_row_id,
       source.source_row_index,
       source.outcome,
       source.failure_category,
       source.source_json,
       evidence.id AS evidence_id
FROM battle_source_rows AS source
JOIN battle_log_observations AS log ON log.id = source.battle_log_observation_id
LEFT JOIN battle_evidence AS evidence ON evidence.source_row_id = source.id
    AND evidence.observation_row_id IS NULL
WHERE log.parsed_payload_id IS NULL
UNION ALL
SELECT occurrence.battle_log_observation_id,
       source.id, occurrence.id, occurrence.source_row_index,
       occurrence.outcome, occurrence.failure_category, source.source_json,
       evidence.id
FROM battle_log_observation_rows AS occurrence
JOIN battle_source_rows AS source ON source.id = occurrence.source_row_id
JOIN battle_log_observations AS log ON log.id = occurrence.battle_log_observation_id
LEFT JOIN battle_evidence AS evidence ON evidence.observation_row_id = occurrence.id
WHERE log.parsed_payload_id IS NULL
UNION ALL
SELECT log.id, source.id, NULL::bigint, member.source_row_index,
       source.outcome, source.failure_category, source.source_json, evidence.id
FROM battle_log_observations AS log
JOIN battle_payload_rows AS member ON member.parsed_payload_id = log.parsed_payload_id
    AND member.reporting_player_id = log.player_id
JOIN battle_source_rows AS source ON source.id = member.source_row_id
LEFT JOIN LATERAL (
    SELECT e.id FROM battle_evidence AS e
    WHERE e.source_row_id = source.id AND e.observation_row_id IS NULL
      AND (e.source_observed_at, e.observation_id) <= (log.observed_at, log.observation_id)
    ORDER BY e.source_observed_at DESC, e.observation_id DESC, e.id DESC
    LIMIT 1
) AS evidence ON true
UNION ALL
SELECT log.id, source.id, NULL::bigint, (member.position - 1)::integer,
       source.outcome, source.failure_category, source.source_json, evidence.id
FROM battle_log_observations AS log
JOIN battle_payload_row_lists AS list ON list.parsed_payload_id = log.parsed_payload_id
    AND list.reporting_player_id = log.player_id
CROSS JOIN LATERAL unnest(list.source_row_ids)
    WITH ORDINALITY AS member (source_row_id, position)
JOIN battle_source_rows AS source ON source.id = member.source_row_id
LEFT JOIN LATERAL (
    SELECT e.id FROM battle_evidence AS e
    WHERE e.source_row_id = source.id AND e.observation_row_id IS NULL
      AND (e.source_observed_at, e.observation_id) <= (log.observed_at, log.observation_id)
    ORDER BY e.source_observed_at DESC, e.observation_id DESC, e.id DESC
    LIMIT 1
) AS evidence ON true;

DROP INDEX IF EXISTS archive_catalogue_current_hash;
DROP INDEX IF EXISTS python_processing_attempts_job_order_v2;

INSERT INTO clash_lens_schema_migrations(version) VALUES (51)
ON CONFLICT (version) DO NOTHING;
COMMIT;
