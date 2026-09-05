-- Separate immutable battle reports from the bounded record of a returned log.
-- Existing evidence and publication identities remain readable. No data is deleted.
BEGIN;

ALTER TABLE battle_source_rows
    ADD COLUMN IF NOT EXISTS report_hash text;
ALTER TABLE battle_source_rows
    DROP CONSTRAINT IF EXISTS battle_source_rows_identity_v4;
ALTER TABLE battle_source_rows
    ADD CONSTRAINT battle_source_rows_identity_v4 CHECK (
        parsed_payload_id IS NOT NULL OR battle_log_observation_id IS NOT NULL
        OR report_hash IS NOT NULL
    ) NOT VALID;
CREATE UNIQUE INDEX IF NOT EXISTS battle_source_rows_report_hash
    ON battle_source_rows (report_hash) WHERE report_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS battle_payload_rows (
    parsed_payload_id bigint NOT NULL REFERENCES parsed_source_payloads(id),
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    source_row_id bigint NOT NULL REFERENCES battle_source_rows(id),
    PRIMARY KEY (parsed_payload_id, source_row_index)
);
CREATE INDEX IF NOT EXISTS battle_payload_rows_source
    ON battle_payload_rows (source_row_id);
ALTER TABLE battle_log_observations
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id);
CREATE INDEX IF NOT EXISTS battle_log_observations_payload
    ON battle_log_observations (parsed_payload_id);

-- Only new reports and genuine corrections create evidence versions. Reverting
-- to an earlier report is a new correction, but reuses its source content.
CREATE UNIQUE INDEX IF NOT EXISTS battle_evidence_compact_report
    ON battle_evidence (observation_id, source_row_id, parser_version)
    WHERE observation_row_id IS NULL;
CREATE INDEX IF NOT EXISTS battle_evidence_compact_source_time
    ON battle_evidence (source_row_id, source_observed_at DESC, observation_id DESC, id DESC)
    WHERE observation_row_id IS NULL;

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
JOIN battle_source_rows AS source ON source.id = member.source_row_id
LEFT JOIN LATERAL (
    SELECT e.id FROM battle_evidence AS e
    WHERE e.source_row_id = source.id AND e.observation_row_id IS NULL
      AND (e.source_observed_at, e.observation_id) <= (log.observed_at, log.observation_id)
    ORDER BY e.source_observed_at DESC, e.observation_id DESC, e.id DESC
    LIMIT 1
) AS evidence ON true;

GRANT SELECT, INSERT ON battle_payload_rows TO clashlens_python_worker;
INSERT INTO clash_lens_schema_migrations(version) VALUES (16)
ON CONFLICT (version) DO NOTHING;
COMMIT;
