-- Deduplicate parsed content while retaining every processing occurrence.
-- Historical occurrence rows remain readable; only new writes use canonical
-- payload/source-row identities.
BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

DO $$
DECLARE current_version integer;
BEGIN
    SELECT version INTO current_version
    FROM clash_lens_contract
    WHERE singleton;
    IF current_version NOT IN (4, 5) THEN
        RAISE EXCEPTION 'parsed-content migration requires contract version 4 or 5 (got %)', current_version;
    END IF;
END $$;

-- A processing result points at the exact canonical parsed payload when one
-- exists. Failed parsing outcomes intentionally keep this nullable.
ALTER TABLE observation_processing_outcomes
    ADD COLUMN IF NOT EXISTS attempt_id bigint REFERENCES python_processing_attempts(id),
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id);

UPDATE observation_processing_outcomes AS outcome
SET parsed_payload_id = payload.id
FROM parsed_source_payloads AS payload
WHERE outcome.parsed_payload_id IS NULL
  AND outcome.endpoint = payload.endpoint
  AND outcome.response_hash = payload.response_hash
  AND outcome.parser_version = payload.parser_version;

CREATE INDEX IF NOT EXISTS observation_processing_outcomes_payload_v4
    ON observation_processing_outcomes (parsed_payload_id, observation_id);

CREATE OR REPLACE FUNCTION clashlens_validate_processing_payload_v4()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE payload parsed_source_payloads%ROWTYPE;
       source collector_observations%ROWTYPE;
BEGIN
    IF NEW.parsed_payload_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT * INTO payload FROM parsed_source_payloads WHERE id = NEW.parsed_payload_id;
    IF NOT FOUND
       OR payload.endpoint IS DISTINCT FROM NEW.endpoint
       OR payload.response_hash IS DISTINCT FROM NEW.response_hash
       OR payload.parser_version IS DISTINCT FROM NEW.parser_version
       OR payload.parse_outcome NOT IN ('valid', 'valid_with_gaps') THEN
        RAISE EXCEPTION 'processing outcome canonical payload identity does not match';
    END IF;
    SELECT * INTO source FROM collector_observations WHERE id = NEW.observation_id;
    IF NOT FOUND
       OR source.response_hash IS DISTINCT FROM NEW.response_hash
       OR source.endpoint IS DISTINCT FROM NEW.endpoint THEN
        RAISE EXCEPTION 'processing outcome does not match collector observation';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS observation_processing_outcomes_validate_payload_v4
    ON observation_processing_outcomes;
CREATE TRIGGER observation_processing_outcomes_validate_payload_v4
BEFORE INSERT OR UPDATE OF endpoint, response_hash, parser_version, parsed_payload_id
ON observation_processing_outcomes
FOR EACH ROW EXECUTE FUNCTION clashlens_validate_processing_payload_v4();

-- Parsed payload identity is immutable. Conflicting parser/schema/content
-- attributes are an integrity error rather than an overwrite.
CREATE OR REPLACE FUNCTION clashlens_immutable_parsed_source_payload_v4()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.endpoint IS DISTINCT FROM OLD.endpoint
       OR NEW.response_hash IS DISTINCT FROM OLD.response_hash
       OR NEW.parser_version IS DISTINCT FROM OLD.parser_version
       OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
       OR NEW.parse_outcome IS DISTINCT FROM OLD.parse_outcome
       OR NEW.parsed_json IS DISTINCT FROM OLD.parsed_json THEN
        RAISE EXCEPTION 'parsed source payload identity is immutable';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS parsed_source_payloads_immutable_v4
    ON parsed_source_payloads;
CREATE TRIGGER parsed_source_payloads_immutable_v4
BEFORE UPDATE ON parsed_source_payloads
FOR EACH ROW EXECUTE FUNCTION clashlens_immutable_parsed_source_payload_v4();

-- Semantic profile versions retain one immutable projection. The first
-- observation remains the historical identity; effects carry later freshness
-- and occurrence metadata.
ALTER TABLE player_profile_versions
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id),
    ADD COLUMN IF NOT EXISTS semantic_projection jsonb NOT NULL DEFAULT '{}'::jsonb;

UPDATE player_profile_versions
SET semantic_projection = jsonb_build_object(
    'normalized_tag', normalized_tag,
    'name', name,
    'trophies', trophies,
    'league_tier_id', league_tier_id,
    'league_tier_name', league_tier_name,
    'eligibility_state', eligibility_state,
    'eligibility_reason', eligibility_reason,
    'source_contract_state', source_contract_state,
    'current_league_season_id', current_league_season_id,
    'previous_league_season_id', previous_league_season_id,
    'season_anchor_state', season_anchor_state,
    'clan_name', CASE
        WHEN jsonb_typeof(profile_json -> 'clan') = 'object'
        THEN profile_json -> 'clan' -> 'name'
        ELSE NULL
    END
)
WHERE semantic_projection = '{}'::jsonb;

UPDATE player_profile_versions AS profile
SET parsed_payload_id = payload.id
FROM collector_observations AS observation
JOIN parsed_source_payloads AS payload
  ON payload.endpoint = observation.endpoint
 AND payload.response_hash = observation.response_hash
WHERE profile.parsed_payload_id IS NULL
  AND profile.observation_id = observation.id
  AND payload.parser_version = profile.parser_version;

CREATE INDEX IF NOT EXISTS player_profile_versions_semantic_projection_v4
    ON player_profile_versions (player_id, semantic_projection);

ALTER TABLE player_profile_effects
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id),
    ADD COLUMN IF NOT EXISTS attempt_id bigint REFERENCES python_processing_attempts(id),
    ADD COLUMN IF NOT EXISTS processing_outcome_id bigint REFERENCES observation_processing_outcomes(id),
    ADD COLUMN IF NOT EXISTS observed_at timestamptz,
    ADD COLUMN IF NOT EXISTS source_http_status integer,
    ADD COLUMN IF NOT EXISTS endpoint_version text,
    ADD COLUMN IF NOT EXISTS schema_version text,
    ADD COLUMN IF NOT EXISTS parser_version text;

UPDATE player_profile_effects AS effect
SET observed_at = profile.observed_at,
    source_http_status = profile.source_http_status,
    endpoint_version = profile.endpoint_version,
    schema_version = profile.schema_version,
    parser_version = profile.parser_version,
    parsed_payload_id = profile.parsed_payload_id,
    attempt_id = NULL
FROM player_profile_versions AS profile,
     collector_observations AS observation
WHERE effect.profile_version_id = profile.id
  AND observation.id = effect.observation_id;

ALTER TABLE player_profile_effects
    ALTER COLUMN observed_at SET NOT NULL,
    ALTER COLUMN source_http_status SET NOT NULL,
    ALTER COLUMN endpoint_version SET NOT NULL,
    ALTER COLUMN schema_version SET NOT NULL,
    ALTER COLUMN parser_version SET NOT NULL;
ALTER TABLE player_profile_effects
    DROP CONSTRAINT IF EXISTS player_profile_effects_observation_id_effect_kind_key;
CREATE UNIQUE INDEX IF NOT EXISTS player_profile_effects_observation_parser_kind_v4
    ON player_profile_effects (observation_id, parser_version, effect_kind);
CREATE INDEX IF NOT EXISTS player_profile_effects_current_v4
    ON player_profile_effects (profile_version_id, observed_at DESC, id DESC);

-- Canonical battle source rows are keyed by parsed payload and source order.
-- Old rows keep their observation ownership and are exposed through the
-- compatibility view below.
ALTER TABLE battle_source_rows
    ALTER COLUMN battle_log_observation_id DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id);
CREATE INDEX IF NOT EXISTS battle_source_rows_payload_order_v4
    ON battle_source_rows (parsed_payload_id, source_row_index);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'battle_source_rows'::regclass
          AND conname = 'battle_source_rows_payload_order_v4_key'
    ) THEN
        ALTER TABLE battle_source_rows
            ADD CONSTRAINT battle_source_rows_payload_order_v4_key
            UNIQUE (parsed_payload_id, source_row_index);
    END IF;
END
$$;
ALTER TABLE battle_source_rows
    DROP CONSTRAINT IF EXISTS battle_source_rows_identity_v4;
ALTER TABLE battle_source_rows
    ADD CONSTRAINT battle_source_rows_identity_v4 CHECK (
        parsed_payload_id IS NOT NULL OR battle_log_observation_id IS NOT NULL
    ) NOT VALID;

CREATE TABLE IF NOT EXISTS battle_log_observation_rows (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    battle_log_observation_id bigint NOT NULL REFERENCES battle_log_observations(id),
    source_row_id bigint NOT NULL REFERENCES battle_source_rows(id),
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    outcome text NOT NULL CHECK (outcome IN (
        'valid_legend', 'ignored_non_legend', 'malformed_legend_row'
    )),
    failure_category text,
    reporting_player_id bigint NOT NULL REFERENCES players(id),
    observed_at timestamptz NOT NULL,
    parser_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (battle_log_observation_id, source_row_index),
    UNIQUE (battle_log_observation_id, source_row_id)
);
CREATE INDEX IF NOT EXISTS battle_log_observation_rows_source_v4
    ON battle_log_observation_rows (source_row_id, battle_log_observation_id);

CREATE OR REPLACE VIEW battle_log_observation_source_rows AS
SELECT source.battle_log_observation_id,
       source.id AS source_row_id,
       NULL::bigint AS observation_row_id,
       source.source_row_index,
       source.outcome,
       source.failure_category,
       source.source_json
FROM battle_source_rows AS source
WHERE source.battle_log_observation_id IS NOT NULL
UNION ALL
SELECT occurrence.battle_log_observation_id,
       occurrence.source_row_id,
       occurrence.id AS observation_row_id,
       occurrence.source_row_index,
       occurrence.outcome,
       occurrence.failure_category,
       source.source_json
FROM battle_log_observation_rows AS occurrence
JOIN battle_source_rows AS source ON source.id = occurrence.source_row_id;

-- Source rows can now support more than one occurrence. New evidence remains
-- one-to-one with its occurrence link; historical evidence stays readable.
ALTER TABLE battle_evidence
    DROP CONSTRAINT IF EXISTS battle_evidence_source_row_id_key,
    ADD COLUMN IF NOT EXISTS observation_row_id bigint
        REFERENCES battle_log_observation_rows(id);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'battle_evidence'::regclass
          AND conname = 'battle_evidence_observation_row_v4_key'
    ) THEN
        ALTER TABLE battle_evidence
            ADD CONSTRAINT battle_evidence_observation_row_v4_key
            UNIQUE (observation_row_id);
    END IF;
END
$$;

-- Ranking entries use the same source-row/content split. Existing rows keep
-- version_id; new canonical rows have no version owner and are linked below.
ALTER TABLE official_top200_entries
    ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY,
    ADD COLUMN IF NOT EXISTS parsed_payload_id bigint REFERENCES parsed_source_payloads(id),
    ADD COLUMN IF NOT EXISTS source_row_index integer;
UPDATE official_top200_entries
SET source_row_index = rank - 1
WHERE source_row_index IS NULL;
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'official_top200_entries'::regclass
          AND conname = 'official_top200_entries_pkey'
          AND pg_get_constraintdef(oid) LIKE '%(version_id, rank)%'
    ) THEN
        ALTER TABLE official_top200_entries
            DROP CONSTRAINT official_top200_entries_pkey;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'official_top200_entries'::regclass
          AND conname = 'official_top200_entries_pkey'
    ) THEN
        ALTER TABLE official_top200_entries
            ADD CONSTRAINT official_top200_entries_pkey PRIMARY KEY (id);
    END IF;
END
$$;
ALTER TABLE official_top200_entries
    ALTER COLUMN version_id DROP NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS official_top200_entries_version_rank_v4
    ON official_top200_entries (version_id, rank)
    WHERE version_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS official_top200_entries_version_player_v4
    ON official_top200_entries (version_id, player_id)
    WHERE version_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS official_top200_entries_version_tag_v4
    ON official_top200_entries (version_id, normalized_tag)
    WHERE version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS official_top200_entries_payload_order_v4
    ON official_top200_entries (parsed_payload_id, source_row_index);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'official_top200_entries'::regclass
          AND conname = 'official_top200_entries_payload_order_v4_key'
    ) THEN
        ALTER TABLE official_top200_entries
            ADD CONSTRAINT official_top200_entries_payload_order_v4_key
            UNIQUE (parsed_payload_id, source_row_index);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS official_top200_version_entries (
    version_id bigint NOT NULL REFERENCES official_top200_versions(id),
    source_row_id bigint NOT NULL REFERENCES official_top200_entries(id),
    rank integer NOT NULL CHECK (rank BETWEEN 1 AND 200),
    player_id bigint NOT NULL REFERENCES players(id),
    normalized_tag text NOT NULL,
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (version_id, rank),
    UNIQUE (version_id, source_row_id),
    UNIQUE (version_id, player_id),
    UNIQUE (version_id, normalized_tag)
);
CREATE INDEX IF NOT EXISTS official_top200_version_entries_source_v4
    ON official_top200_version_entries (source_row_id, version_id);

CREATE TABLE IF NOT EXISTS official_top200_attempt_entries (
    attempt_id bigint NOT NULL REFERENCES official_top200_attempts(id),
    source_row_id bigint NOT NULL REFERENCES official_top200_entries(id),
    rank integer NOT NULL CHECK (rank BETWEEN 1 AND 200),
    player_id bigint NOT NULL REFERENCES players(id),
    normalized_tag text NOT NULL,
    source_row_index integer NOT NULL CHECK (source_row_index >= 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (attempt_id, source_row_index),
    UNIQUE (attempt_id, source_row_id)
);
CREATE INDEX IF NOT EXISTS official_top200_attempt_entries_source_v4
    ON official_top200_attempt_entries (source_row_id, attempt_id);

INSERT INTO official_top200_version_entries (
    version_id, source_row_id, rank, player_id, normalized_tag, source_row_index
)
SELECT version_id, id, rank, player_id, normalized_tag, source_row_index
FROM official_top200_entries
WHERE version_id IS NOT NULL
ON CONFLICT (version_id, rank) DO NOTHING;

-- Worker may insert canonical content and occurrence links, but cannot mutate
-- an immutable parsed payload after insertion.
REVOKE UPDATE ON parsed_source_payloads FROM clashlens_python_worker;
GRANT SELECT, INSERT ON parsed_source_payloads TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON battle_log_observation_rows,
    official_top200_version_entries, official_top200_attempt_entries
    TO clashlens_python_worker;
DO $$
DECLARE sequence_name text;
BEGIN
    FOREACH sequence_name IN ARRAY ARRAY[
        'battle_log_observation_rows_id_seq', 'official_top200_entries_id_seq'
    ] LOOP
        IF to_regclass(sequence_name) IS NOT NULL THEN
            EXECUTE format(
                'GRANT USAGE ON SEQUENCE %I TO clashlens_python_worker',
                sequence_name
            );
        END IF;
    END LOOP;
END
$$;

CREATE OR REPLACE FUNCTION clashlens_validate_reset_baseline_evidence_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    baseline collector_reset_baseline_sweeps%ROWTYPE;
    root_job collector_jobs%ROWTYPE;
    attempt_job_id bigint;
    source_observation collector_observations%ROWTYPE;
    processing observation_processing_outcomes%ROWTYPE;
    profile_version_exists boolean;
    battle_log_valid boolean;
BEGIN
    IF NEW.evidence_key IS NULL THEN
        NEW.evidence_key := format(
            'reset-baseline:%s:%s:%s:%s:%s:%s:%s',
            COALESCE(NEW.reset_baseline_sweep_id, 0),
            NEW.version,
            COALESCE(NEW.profile_observation_id, 0),
            COALESCE(NEW.battle_log_observation_id, 0),
            NEW.state,
            NEW.profile_valid,
            NEW.battle_log_valid
        );
    END IF;

    IF NEW.reset_baseline_sweep_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT * INTO baseline
    FROM collector_reset_baseline_sweeps
    WHERE id = NEW.reset_baseline_sweep_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reset evidence references an unknown baseline sweep';
    END IF;
    IF NEW.sweep_id IS DISTINCT FROM baseline.reset_sweep_id
       OR NEW.player_id IS DISTINCT FROM baseline.player_id
       OR NEW.boundary_at IS DISTINCT FROM baseline.boundary_at THEN
        RAISE EXCEPTION 'reset evidence identity does not match its immutable sweep';
    END IF;

    IF NEW.collection_job_id IS NULL THEN
        IF NEW.state = 'complete' THEN
            RAISE EXCEPTION 'complete reset evidence requires a collection job';
        END IF;
        RETURN NEW;
    END IF;

    SELECT * INTO root_job
    FROM collector_jobs
    WHERE id = NEW.collection_job_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'reset evidence references an unknown collection job';
    END IF;
    IF root_job.work_type NOT IN ('reset_baseline', 'legacy_reset_profile')
       OR root_job.reset_baseline_sweep_id IS DISTINCT FROM NEW.reset_baseline_sweep_id
       OR root_job.sweep_id IS DISTINCT FROM baseline.reset_sweep_id
       OR root_job.player_id IS DISTINCT FROM NEW.player_id THEN
        RAISE EXCEPTION 'reset evidence collection job identity does not match';
    END IF;
    IF NEW.attempt_id IS NULL THEN
        IF NEW.state = 'complete' THEN
            RAISE EXCEPTION 'complete reset evidence requires an attempt';
        END IF;
    ELSE
        SELECT job_id INTO attempt_job_id
        FROM collector_attempts
        WHERE id = NEW.attempt_id;
        IF attempt_job_id IS DISTINCT FROM root_job.id THEN
            RAISE EXCEPTION 'reset evidence attempt is not the root attempt';
        END IF;
    END IF;

    IF NEW.profile_observation_id IS NOT NULL THEN
        SELECT * INTO source_observation
        FROM collector_observations
        WHERE id = NEW.profile_observation_id;
        IF NOT FOUND OR source_observation.endpoint <> 'profile' THEN
            RAISE EXCEPTION 'reset evidence profile reference is not a profile observation';
        END IF;
        IF NEW.state = 'complete' THEN
            IF source_observation.attempt_id IS DISTINCT FROM NEW.attempt_id
               OR source_observation.player_id IS DISTINCT FROM NEW.player_id
               OR source_observation.response_completed_at < baseline.boundary_at
               OR NOT clashlens_reset_job_lineage_v2(
                    source_observation.collection_job_id, root_job.id
               ) THEN
                RAISE EXCEPTION 'complete reset evidence profile is outside its job attempt';
            END IF;
        END IF;
    END IF;

    IF NEW.battle_log_observation_id IS NOT NULL THEN
        SELECT * INTO source_observation
        FROM collector_observations
        WHERE id = NEW.battle_log_observation_id;
        IF NOT FOUND OR source_observation.endpoint <> 'battle_log' THEN
            RAISE EXCEPTION 'reset evidence battle-log reference is not a battle-log observation';
        END IF;
        IF NEW.state = 'complete' THEN
            IF source_observation.attempt_id IS DISTINCT FROM NEW.attempt_id
               OR source_observation.player_id IS DISTINCT FROM NEW.player_id
               OR source_observation.response_completed_at < baseline.boundary_at
               OR NOT clashlens_reset_job_lineage_v2(
                    source_observation.collection_job_id, root_job.id
               ) THEN
                RAISE EXCEPTION 'complete reset evidence battle log is outside its job attempt';
            END IF;
        END IF;
    END IF;

    IF NEW.state = 'complete' THEN
        SELECT * INTO processing
        FROM observation_processing_outcomes
        WHERE id = NEW.profile_processing_outcome_id;
        IF NOT FOUND OR processing.endpoint <> 'profile'
           OR processing.observation_id IS DISTINCT FROM NEW.profile_observation_id
           OR processing.outcome <> 'processed' THEN
            RAISE EXCEPTION 'complete reset evidence profile was not successfully processed';
        END IF;
        SELECT * INTO processing
        FROM observation_processing_outcomes
        WHERE id = NEW.battle_log_processing_outcome_id;
        IF NOT FOUND OR processing.endpoint <> 'battle_log'
           OR processing.observation_id IS DISTINCT FROM NEW.battle_log_observation_id
           OR processing.outcome <> 'processed' THEN
            RAISE EXCEPTION 'complete reset evidence battle log was not successfully processed';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM player_profile_versions AS profile
            LEFT JOIN player_profile_effects AS effect
              ON effect.profile_version_id = profile.id
            WHERE (profile.observation_id = NEW.profile_observation_id
                   OR effect.observation_id = NEW.profile_observation_id)
              AND COALESCE(effect.parser_version, profile.parser_version) = NEW.parser_version
              AND profile.source_contract_state = 'accepted'
              AND profile.eligibility_state = 'eligible'
        ) INTO profile_version_exists;
        IF NOT profile_version_exists THEN
            RAISE EXCEPTION 'complete reset evidence profile is not accepted Legend I evidence';
        END IF;
        SELECT EXISTS (
            SELECT 1
            FROM battle_log_observations AS log
            WHERE log.observation_id = NEW.battle_log_observation_id
              AND log.parser_version = NEW.parser_version
              AND NOT log.has_row_gap
        ) INTO battle_log_valid;
        IF NOT battle_log_valid THEN
            RAISE EXCEPTION 'complete reset evidence battle log contains a parse gap';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM battle_evidence AS evidence
            JOIN legend_battles AS battle ON battle.id = evidence.battle_id
            JOIN collector_observations AS profile_observation
              ON profile_observation.id = NEW.profile_observation_id
            WHERE (
                    battle.attacker_player_id = NEW.player_id
                    OR battle.defender_player_id = NEW.player_id
                )
              AND evidence.battle_timestamp >= baseline.boundary_at
              AND evidence.battle_timestamp < baseline.boundary_at + interval '1 day'
              AND evidence.battle_timestamp <= profile_observation.response_completed_at
        ) THEN
            RAISE EXCEPTION 'complete reset evidence profile does not precede the first retained event';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM collector_endpoint_results AS result
            WHERE result.attempt_id = NEW.attempt_id
              AND result.endpoint = 'profile'
              AND result.observation_id = NEW.profile_observation_id
              AND result.outcome = 'observed'
        ) OR NOT EXISTS (
            SELECT 1 FROM collector_endpoint_results AS result
            WHERE result.attempt_id = NEW.attempt_id
              AND result.endpoint = 'battle_log'
              AND result.observation_id = NEW.battle_log_observation_id
              AND result.outcome = 'observed'
        ) THEN
            RAISE EXCEPTION 'complete reset evidence does not match both endpoint results';
        END IF;
    END IF;
    RETURN NEW;
END
$$;

UPDATE clash_lens_contract SET version = 5 WHERE singleton;
INSERT INTO clash_lens_schema_migrations(version) VALUES (12)
ON CONFLICT (version) DO NOTHING;

COMMIT;
