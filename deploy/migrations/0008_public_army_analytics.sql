-- Clash Lens deployment migration 0008.
-- Perspective-preserving partial army decodes and public army publications.
BEGIN;

ALTER TABLE battle_army_decodes
    ADD COLUMN IF NOT EXISTS perspective text,
    ADD COLUMN IF NOT EXISTS unresolved_components jsonb NOT NULL DEFAULT '[]'::jsonb;
UPDATE battle_army_decodes AS decode
SET perspective = evidence.perspective
FROM battle_evidence AS evidence
WHERE evidence.id = decode.evidence_id AND decode.perspective IS NULL;
ALTER TABLE battle_army_decodes ALTER COLUMN perspective SET NOT NULL;
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_perspective_check;
ALTER TABLE battle_army_decodes
    ADD CONSTRAINT battle_army_decodes_perspective_check
    CHECK (perspective IN ('attacker','defender'));
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_status_check;
ALTER TABLE battle_army_decodes
    ADD CONSTRAINT battle_army_decodes_status_check
    CHECK (status IN ('decoded','partial','failed'));
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_check;
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_check1;
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_result_check;
ALTER TABLE battle_army_decodes
    ADD CONSTRAINT battle_army_decodes_result_check CHECK (
        (status = 'decoded' AND exact_army_id IS NOT NULL AND identity_hash IS NOT NULL)
        OR (status = 'partial' AND exact_army_id IS NULL AND identity_hash IS NULL
            AND jsonb_array_length(unresolved_components) > 0)
        OR status = 'failed'
    );
ALTER TABLE battle_army_decodes DROP CONSTRAINT IF EXISTS battle_army_decodes_failure_check;
ALTER TABLE battle_army_decodes
    ADD CONSTRAINT battle_army_decodes_failure_check CHECK (
        (status = 'failed' AND failure_category IS NOT NULL)
        OR status IN ('decoded','partial')
    );
DROP INDEX IF EXISTS battle_army_decodes_one_active_per_battle;
CREATE UNIQUE INDEX IF NOT EXISTS battle_army_decodes_one_active_per_perspective
    ON battle_army_decodes (battle_id, perspective, decoder_version, catalog_version)
    WHERE is_active;
CREATE INDEX IF NOT EXISTS battle_army_decodes_public_lookup
    ON battle_army_decodes (evidence_id, perspective, is_active);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = current_schema()
                 AND table_name = 'army_analytics_breakdowns'
                 AND column_name = 'hit_rate') THEN
        ALTER TABLE army_analytics_breakdowns RENAME COLUMN hit_rate TO three_star_rate;
    END IF;
END $$;
ALTER TABLE army_analytics_breakdowns
    ADD COLUMN IF NOT EXISTS avg_stars numeric(5,3),
    ADD COLUMN IF NOT EXISTS usage_denominator integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS unknown_excluded_attacks integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS army_analytics_battle_facts (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    battle_id bigint NOT NULL REFERENCES legend_battles(id),
    evidence_id bigint NOT NULL REFERENCES battle_evidence(id),
    decode_id bigint REFERENCES battle_army_decodes(id),
    source_ranked_day_version_id bigint NOT NULL REFERENCES ranked_day_versions(id),
    ranked_day_start timestamptz NOT NULL,
    official_season_id text NOT NULL,
    season_day_number integer NOT NULL CHECK (season_day_number BETWEEN 1 AND 28),
    lens text NOT NULL CHECK (lens IN ('offense','defense')),
    population_player_id bigint NOT NULL REFERENCES players(id),
    battle_time_trophies integer,
    stars integer NOT NULL CHECK (stars BETWEEN 0 AND 3),
    destruction_percentage integer NOT NULL CHECK (destruction_percentage BETWEEN 0 AND 100),
    army_state text NOT NULL,
    failure_reason text,
    home_troops jsonb NOT NULL DEFAULT '[]'::jsonb,
    spells jsonb NOT NULL DEFAULT '[]'::jsonb,
    siege jsonb NOT NULL DEFAULT '[]'::jsonb,
    cc_troops jsonb NOT NULL DEFAULT '[]'::jsonb,
    heroes jsonb NOT NULL DEFAULT '[]'::jsonb,
    unresolved_components jsonb NOT NULL DEFAULT '[]'::jsonb,
    perspective_disagreement boolean NOT NULL,
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL,
    is_current boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    supersedes_id bigint REFERENCES army_analytics_battle_facts(id),
    UNIQUE (battle_id, lens, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS army_analytics_battle_facts_one_current
    ON army_analytics_battle_facts(battle_id, lens) WHERE is_current;
CREATE INDEX IF NOT EXISTS army_analytics_battle_facts_selection
    ON army_analytics_battle_facts(official_season_id, season_day_number, lens, population_player_id)
    WHERE is_current;
CREATE INDEX IF NOT EXISTS army_analytics_battle_facts_trophies
    ON army_analytics_battle_facts(official_season_id, season_day_number, lens, battle_time_trophies)
    WHERE is_current;

-- Durable per-day marker written in the same transaction as the day's battle
-- facts, so public reads never serve a false empty result for a completed
-- Legend day whose army analytics job has not finished.
CREATE TABLE IF NOT EXISTS army_analytics_completed_days (
    ranked_day_start timestamptz PRIMARY KEY,
    official_season_id text NOT NULL,
    season_day_number integer NOT NULL CHECK (season_day_number BETWEEN 1 AND 28),
    fact_input_hash text NOT NULL CHECK (fact_input_hash ~ '^[0-9a-f]{64}$'),
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Army analytics pages are calculated on the spot from retained versioned
-- facts; there is no per-selection publication storage.

REVOKE ALL ON battle_army_decodes, army_analytics_breakdowns,
    army_analytics_battle_facts FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON battle_army_decodes, army_analytics_battle_facts
    TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON army_analytics_breakdowns
    TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON army_analytics_completed_days
    TO clashlens_python_worker;
GRANT SELECT ON battle_army_decodes, army_analytics_battle_facts,
    army_analytics_completed_days
    TO clashlens_python_api;
GRANT SELECT ON leaderboard_snapshots, leaderboard_snapshot_entries
    TO clashlens_python_api;
GRANT USAGE, SELECT ON SEQUENCE army_analytics_battle_facts_id_seq
    TO clashlens_python_worker;

-- Keep army jobs independently versioned while preserving the existing claim contract.
CREATE OR REPLACE FUNCTION clashlens_set_python_claim_compatibility_v3()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.claim_compatibility_version := CASE
        WHEN NEW.processing_version = 'clashlens-domain-processing-v1'
         AND NEW.domain_rule_version = 'clashlens-domain-rules-v1'
         AND (
            (NEW.work_type IN ('process_observation', 'replay_observation')
                AND NEW.parser_version IN ('supercell-source-parser-v1','supercell-source-parser-v2')
                AND EXISTS (SELECT 1 FROM collector_observations AS observation WHERE observation.id = COALESCE(NEW.observation_id, NEW.replay_observation_id) AND ((observation.endpoint = 'profile' AND observation.endpoint_version = 'profile-v1' AND observation.schema_version = 'profile-schema-v1') OR (observation.endpoint = 'battle_log' AND observation.endpoint_version = 'battle-log-v1' AND observation.schema_version = 'battle-log-schema-v1') OR (observation.endpoint = 'global_player_rankings' AND observation.endpoint_version = 'global-player-rankings-v1' AND observation.schema_version = 'global-player-rankings-schema-v1'))))
            OR (NEW.work_type IN ('reconcile_ranked_day','build_snapshot') AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_analytics' AND NEW.analytics_rule_version = 'legend-analytics-v1' AND NEW.input_json ? 'snapshot_id' AND NEW.input_json ? 'snapshot_version' AND NEW.input_json ? 'snapshot_input_hash' AND NEW.input_json ? 'source_ranked_day_version_id' AND (NEW.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$' AND (NEW.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$' AND (NEW.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$' AND length(NEW.input_json->>'snapshot_input_hash') > 0)
            OR (NEW.work_type IN ('build_army_analytics','redecode_army') AND NEW.analytics_rule_version = 'army-analytics-v2')
         )
        THEN CASE
            WHEN NEW.work_type IN ('build_army_analytics','redecode_army') THEN 3
            WHEN NEW.parser_version = 'supercell-source-parser-v2' THEN 2
            ELSE 1
        END
        ELSE 0
    END;
    RETURN NEW;
END $$;
UPDATE python_processing_jobs
SET claim_compatibility_version = claim_compatibility_version
WHERE work_type IN ('build_army_analytics','redecode_army');

-- v1 army jobs are no longer claimable under army-analytics-v2; retire them so
-- they cannot linger as unclaimable pending rows. Completed/failed history stays.
UPDATE python_processing_jobs
SET status = 'cancelled',
    failure_category = 'superseded_analytics_rule_version',
    updated_at = clock_timestamp()
WHERE work_type IN ('build_army_analytics', 'redecode_army')
  AND analytics_rule_version = 'legend-analytics-v1'
  AND status IN ('pending', 'waiting_retry', 'leased');

-- Rebuild in bounded batches under the new decoder without rewriting v1 history.
WITH numbered AS (
    SELECT id, (row_number() OVER (ORDER BY id) - 1) / 100 AS batch
    FROM legend_battles
), batches AS (
    SELECT jsonb_agg(id ORDER BY id) AS battle_ids, min(id) AS first_id, max(id) AS last_id
    FROM numbered GROUP BY batch
)
INSERT INTO python_processing_jobs (
    work_type, deduplication_key, input_json, processing_version,
    domain_rule_version, analytics_rule_version, due_at
)
SELECT 'redecode_army',
       format('redecode_army:army-decoder-v2:unit-catalog-v1:%s:%s', first_id, last_id),
       jsonb_build_object('battle_ids', battle_ids),
       'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
       'army-analytics-v2', clock_timestamp()
FROM batches ON CONFLICT (deduplication_key) DO NOTHING;

INSERT INTO clash_lens_schema_migrations(version) VALUES (8)
ON CONFLICT (version) DO NOTHING;
COMMIT;
