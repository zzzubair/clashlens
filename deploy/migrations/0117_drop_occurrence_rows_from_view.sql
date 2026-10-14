-- Clash Lens deployment migration 0117.
-- battle_log_observation_source_rows lists each saved battle log's rows from
-- four storage layouts. This drops the second layout, which reads
-- battle_log_observation_rows, and the observation_row_id column only that
-- layout filled. Nothing has written that table since compact battle storage
-- (0016). On production on 14 October 2026 it held 0 rows, and every battle
-- log had a compact payload, so that layout returned nothing. The view
-- returns the same rows without it. If the table holds any row, this
-- migration stops instead.
--
-- At 16:56 UTC that day an automatic vacuum recorded the empty table as
-- holding 0 rows. Before, it had never been measured. battle_evidence links
-- to that table, and the database expects one linked battle report per row of
-- the table it links to. With 0 rows, it expected all 3.7 million saved
-- battle reports to match each battle log. So each day result's battle log
-- coverage read (ranked_day_inputs._coverage_sql) built a lookup table of
-- every battle report again for each battle log, too big for memory, and
-- wrote it to disk. A day result took 10 to 70 seconds, and live day results
-- fell 38 minutes behind until the table's statistics were reset by hand at
-- 17:40. The next vacuum or ANALYZE of the table records 0 rows again.
--
-- On a copy of production with the table recorded as empty, for 6
-- player-days of 15 to 43 battle logs, the old view took 10.3 to 45.9
-- seconds a read (median of 5 runs) and wrote 1.9 to 5.6 GB of temporary
-- files. This view took 8 to 16 ms and wrote none, the same as the hand fix,
-- and returned the same rows. After a fresh ANALYZE of every table the read
-- uses, the old view took 10.7 to 32.4 seconds again and this view 7 to 15 ms.
--
-- The table stays, empty.
BEGIN;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM battle_log_observation_rows) THEN
        RAISE EXCEPTION 'battle_log_observation_rows holds rows this view would stop listing';
    END IF;
END
$$;

DROP VIEW battle_log_observation_source_rows;

CREATE VIEW battle_log_observation_source_rows AS
SELECT source.battle_log_observation_id,
       source.id AS source_row_id,
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
SELECT log.id, source.id, member.source_row_index,
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
SELECT log.id, source.id, (member.position - 1)::integer,
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

GRANT SELECT ON battle_log_observation_source_rows TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (117)
ON CONFLICT (version) DO NOTHING;
COMMIT;
