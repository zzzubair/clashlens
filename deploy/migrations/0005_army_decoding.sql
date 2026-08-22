-- Clash Lens deployment migration 0005.
-- Army share-code decoding, normalized exact armies, and durable army analytics.

BEGIN;

ALTER TABLE battle_evidence ALTER COLUMN army_share_code DROP NOT NULL;

CREATE TABLE IF NOT EXISTS unit_catalog_versions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version text NOT NULL UNIQUE,
    content_hash text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    provenance text NOT NULL,
    license text NOT NULL,
    entries jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- seed v1
INSERT INTO unit_catalog_versions (version, content_hash, provenance, license, entries)
VALUES (
    'unit-catalog-v1',
    'cf2095b66a4f9614981725fc022f1891e29b46e78a2cbc9171c650e3f326ce70',
    'ClashKingInc/clashy.py@0703aee64a24c48aef296856bd688704d434181f coc/static/static_data.json blob b90a6b2bfbccac3b755a68f78fe8885b35bc80d6 sha256 3fa1e2b9ccd4a24f48ca7ade5a23d54c8ce0c3f17ad986270f2b913584f9c1d5; fixture h0p9e14_32d1x53u2x58-1x97s2x2 observed 2026-08-21',
    'MIT; Supercell Fan Content Policy applies to game metadata',
    '{"equipment:0":{"category":"equipment","is_siege":false,"name":"Barbarian Puppet"},"equipment:1":{"category":"equipment","is_siege":false,"name":"Rage Vial"},"equipment:10":{"category":"equipment","is_siege":false,"name":"Giant Gauntlet"},"equipment:11":{"category":"equipment","is_siege":false,"name":"Vampstache"},"equipment:12":{"category":"equipment","is_siege":false,"name":"Haste Vial"},"equipment:13":{"category":"equipment","is_siege":false,"name":"Rocket Spear"},"equipment:14":{"category":"equipment","is_siege":false,"name":"Spiky Ball"},"equipment:15":{"category":"equipment","is_siege":false,"name":"Frozen Arrow"},"equipment:16":{"category":"equipment","is_siege":false,"name":"Monolith Arrow"},"equipment:17":{"category":"equipment","is_siege":false,"name":"Giant Arrow"},"equipment:19":{"category":"equipment","is_siege":false,"name":"Heroic Torch"},"equipment:2":{"category":"equipment","is_siege":false,"name":"Archer Puppet"},"equipment:20":{"category":"equipment","is_siege":false,"name":"Healer Puppet"},"equipment:22":{"category":"equipment","is_siege":false,"name":"Fireball"},"equipment:24":{"category":"equipment","is_siege":false,"name":"Rage Gem"},"equipment:3":{"category":"equipment","is_siege":false,"name":"Invisibility Vial"},"equipment:32":{"category":"equipment","is_siege":false,"name":"Snake Bracelet"},"equipment:34":{"category":"equipment","is_siege":false,"name":"Healing Tome"},"equipment:35":{"category":"equipment","is_siege":false,"name":"Dark Crown"},"equipment:39":{"category":"equipment","is_siege":false,"name":"Magic Mirror"},"equipment:4":{"category":"equipment","is_siege":false,"name":"Eternal Tome"},"equipment:40":{"category":"equipment","is_siege":false,"name":"Electro Boots"},"equipment:41":{"category":"equipment","is_siege":false,"name":"Lavaloon Puppet"},"equipment:42":{"category":"equipment","is_siege":false,"name":"Henchmen Puppet"},"equipment:43":{"category":"equipment","is_siege":false,"name":"Dark Orb"},"equipment:44":{"category":"equipment","is_siege":false,"name":"Metal Pants"},"equipment:47":{"category":"equipment","is_siege":false,"name":"Noble Iron"},"equipment:48":{"category":"equipment","is_siege":false,"name":"Action Figure"},"equipment:49":{"category":"equipment","is_siege":false,"name":"Meteor Staff"},"equipment:5":{"category":"equipment","is_siege":false,"name":"Life Gem"},"equipment:50":{"category":"equipment","is_siege":false,"name":"Frost Flake"},"equipment:51":{"category":"equipment","is_siege":false,"name":"Stick Horse"},"equipment:52":{"category":"equipment","is_siege":false,"name":"Fire Heart"},"equipment:53":{"category":"equipment","is_siege":false,"name":"Rocket Backpack"},"equipment:56":{"category":"equipment","is_siege":false,"name":"Stun Blaster"},"equipment:57":{"category":"equipment","is_siege":false,"name":"Flame Blower"},"equipment:59":{"category":"equipment","is_siege":false,"name":"Electro Fangs"},"equipment:6":{"category":"equipment","is_siege":false,"name":"Seeking Shield"},"equipment:7":{"category":"equipment","is_siege":false,"name":"Royal Gem"},"equipment:8":{"category":"equipment","is_siege":false,"name":"Earthquake Boots"},"equipment:9":{"category":"equipment","is_siege":false,"name":"Hog Rider Puppet"},"hero:0":{"category":"hero","is_siege":false,"name":"Barbarian King"},"hero:1":{"category":"hero","is_siege":false,"name":"Archer Queen"},"hero:2":{"category":"hero","is_siege":false,"name":"Grand Warden"},"hero:4":{"category":"hero","is_siege":false,"name":"Royal Champion"},"hero:6":{"category":"hero","is_siege":false,"name":"Minion Prince"},"hero:7":{"category":"hero","is_siege":false,"name":"Dragon Duke"},"pet:0":{"category":"pet","is_siege":false,"name":"L.A.S.S.I"},"pet:1":{"category":"pet","is_siege":false,"name":"Mighty Yak"},"pet:10":{"category":"pet","is_siege":false,"name":"Spirit Fox"},"pet:11":{"category":"pet","is_siege":false,"name":"Angry Jelly"},"pet:16":{"category":"pet","is_siege":false,"name":"Sneezy"},"pet:17":{"category":"pet","is_siege":false,"name":"Greedy Raven"},"pet:2":{"category":"pet","is_siege":false,"name":"Electro Owl"},"pet:3":{"category":"pet","is_siege":false,"name":"Unicorn"},"pet:4":{"category":"pet","is_siege":false,"name":"Phoenix"},"pet:7":{"category":"pet","is_siege":false,"name":"Poison Lizard"},"pet:8":{"category":"pet","is_siege":false,"name":"Diggy"},"pet:9":{"category":"pet","is_siege":false,"name":"Frosty"},"spell:0":{"category":"spell","is_siege":false,"name":"Lightning Spell"},"spell:1":{"category":"spell","is_siege":false,"name":"Healing Spell"},"spell:10":{"category":"spell","is_siege":false,"name":"Earthquake Spell"},"spell:109":{"category":"spell","is_siege":false,"name":"Ice Block Spell"},"spell:11":{"category":"spell","is_siege":false,"name":"Haste Spell"},"spell:120":{"category":"spell","is_siege":false,"name":"Totem Spell"},"spell:123":{"category":"spell","is_siege":false,"name":"Angry Spell"},"spell:16":{"category":"spell","is_siege":false,"name":"Clone Spell"},"spell:17":{"category":"spell","is_siege":false,"name":"Skeleton Spell"},"spell:2":{"category":"spell","is_siege":false,"name":"Rage Spell"},"spell:28":{"category":"spell","is_siege":false,"name":"Bat Spell"},"spell:3":{"category":"spell","is_siege":false,"name":"Jump Spell"},"spell:35":{"category":"spell","is_siege":false,"name":"Invisibility Spell"},"spell:5":{"category":"spell","is_siege":false,"name":"Freeze Spell"},"spell:53":{"category":"spell","is_siege":false,"name":"Recall Spell"},"spell:6":{"category":"spell","is_siege":false,"name":"Santa''s Surprise"},"spell:70":{"category":"spell","is_siege":false,"name":"Overgrowth Spell"},"spell:73":{"category":"spell","is_siege":false,"name":"Bag of Frostmites"},"spell:9":{"category":"spell","is_siege":false,"name":"Poison Spell"},"spell:98":{"category":"spell","is_siege":false,"name":"Revive Spell"},"troop:0":{"category":"troop","is_siege":false,"name":"Barbarian"},"troop:1":{"category":"troop","is_siege":false,"name":"Archer"},"troop:10":{"category":"troop","is_siege":false,"name":"Minion"},"troop:101":{"category":"troop","is_siege":false,"name":"Barcher"},"troop:102":{"category":"troop","is_siege":false,"name":"Witch Golem"},"troop:103":{"category":"troop","is_siege":false,"name":"Hog Wizard"},"troop:104":{"category":"troop","is_siege":false,"name":"Lavaloon"},"troop:109":{"category":"troop","is_siege":false,"name":"Ruin Witch"},"troop:11":{"category":"troop","is_siege":false,"name":"Hog Rider"},"troop:110":{"category":"troop","is_siege":false,"name":"Root Rider"},"troop:118":{"category":"troop","is_siege":false,"name":"C.O.O.K.I.E"},"troop:119":{"category":"troop","is_siege":false,"name":"Firecracker"},"troop:12":{"category":"troop","is_siege":false,"name":"Valkyrie"},"troop:120":{"category":"troop","is_siege":false,"name":"Azure Dragon"},"troop:121":{"category":"troop","is_siege":false,"name":"Barbarian Kicker"},"troop:122":{"category":"troop","is_siege":false,"name":"Giant Thrower"},"troop:123":{"category":"troop","is_siege":false,"name":"Druid"},"troop:125":{"category":"troop","is_siege":false,"name":"Broom Witch"},"troop:13":{"category":"troop","is_siege":false,"name":"Golem"},"troop:130":{"category":"troop","is_siege":false,"name":"Ice Minion"},"troop:132":{"category":"troop","is_siege":false,"name":"Thrower"},"troop:135":{"category":"troop","is_siege":true,"name":"Troop Launcher"},"troop:136":{"category":"troop","is_siege":false,"name":"Debt Collector"},"troop:142":{"category":"troop","is_siege":false,"name":"Snake Barrel"},"troop:147":{"category":"troop","is_siege":false,"name":"Super Yeti"},"troop:15":{"category":"troop","is_siege":false,"name":"Witch"},"troop:150":{"category":"troop","is_siege":false,"name":"Furnace"},"troop:156":{"category":"troop","is_siege":false,"name":"Giant Giant"},"troop:157":{"category":"troop","is_siege":false,"name":"K.A.N.E"},"troop:158":{"category":"troop","is_siege":false,"name":"The Disarmer"},"troop:159":{"category":"troop","is_siege":false,"name":"YEETer"},"troop:167":{"category":"troop","is_siege":false,"name":"Meteor Golem"},"troop:17":{"category":"troop","is_siege":false,"name":"Lava Hound"},"troop:177":{"category":"troop","is_siege":false,"name":"Meteor Golem"},"troop:188":{"category":"troop","is_siege":true,"name":"Sky Wagon"},"troop:2":{"category":"troop","is_siege":false,"name":"Goblin"},"troop:22":{"category":"troop","is_siege":false,"name":"Bowler"},"troop:23":{"category":"troop","is_siege":false,"name":"Baby Dragon"},"troop:24":{"category":"troop","is_siege":false,"name":"Miner"},"troop:26":{"category":"troop","is_siege":false,"name":"Super Barbarian"},"troop:27":{"category":"troop","is_siege":false,"name":"Super Archer"},"troop:28":{"category":"troop","is_siege":false,"name":"Super Wall Breaker"},"troop:29":{"category":"troop","is_siege":false,"name":"Super Giant"},"troop:3":{"category":"troop","is_siege":false,"name":"Giant"},"troop:30":{"category":"troop","is_siege":false,"name":"Ice Wizard"},"troop:4":{"category":"troop","is_siege":false,"name":"Wall Breaker"},"troop:45":{"category":"troop","is_siege":false,"name":"Battle Ram"},"troop:47":{"category":"troop","is_siege":false,"name":"Royal Ghost"},"troop:48":{"category":"troop","is_siege":false,"name":"Pumpkin Barbarian"},"troop:5":{"category":"troop","is_siege":false,"name":"Balloon"},"troop:50":{"category":"troop","is_siege":false,"name":"Giant Skeleton"},"troop:51":{"category":"troop","is_siege":true,"name":"Wall Wrecker"},"troop:52":{"category":"troop","is_siege":true,"name":"Battle Blimp"},"troop:53":{"category":"troop","is_siege":false,"name":"Yeti"},"troop:55":{"category":"troop","is_siege":false,"name":"Sneaky Goblin"},"troop:56":{"category":"troop","is_siege":false,"name":"Super Miner"},"troop:57":{"category":"troop","is_siege":false,"name":"Rocket Balloon"},"troop:58":{"category":"troop","is_siege":false,"name":"Ice Golem"},"troop:59":{"category":"troop","is_siege":false,"name":"Electro Dragon"},"troop:6":{"category":"troop","is_siege":false,"name":"Wizard"},"troop:61":{"category":"troop","is_siege":false,"name":"Skeleton Barrel"},"troop:62":{"category":"troop","is_siege":true,"name":"Stone Slammer"},"troop:63":{"category":"troop","is_siege":false,"name":"Inferno Dragon"},"troop:64":{"category":"troop","is_siege":false,"name":"Super Valkyrie"},"troop:65":{"category":"troop","is_siege":false,"name":"Dragon Rider"},"troop:66":{"category":"troop","is_siege":false,"name":"Super Witch"},"troop:67":{"category":"troop","is_siege":false,"name":"M.E.C.H.A"},"troop:7":{"category":"troop","is_siege":false,"name":"Healer"},"troop:72":{"category":"troop","is_siege":false,"name":"Party Wizard"},"troop:75":{"category":"troop","is_siege":true,"name":"Siege Barracks"},"troop:76":{"category":"troop","is_siege":false,"name":"Ice Hound"},"troop:8":{"category":"troop","is_siege":false,"name":"Dragon"},"troop:80":{"category":"troop","is_siege":false,"name":"Super Bowler"},"troop:81":{"category":"troop","is_siege":false,"name":"Super Dragon"},"troop:82":{"category":"troop","is_siege":false,"name":"Headhunter"},"troop:83":{"category":"troop","is_siege":false,"name":"Super Wizard"},"troop:84":{"category":"troop","is_siege":false,"name":"Super Minion"},"troop:87":{"category":"troop","is_siege":true,"name":"Log Launcher"},"troop:9":{"category":"troop","is_siege":false,"name":"P.E.K.K.A"},"troop:91":{"category":"troop","is_siege":true,"name":"Flame Flinger"},"troop:92":{"category":"troop","is_siege":true,"name":"Battle Drill"},"troop:94":{"category":"troop","is_siege":false,"name":"Ram Rider"},"troop:95":{"category":"troop","is_siege":false,"name":"Electro Titan"},"troop:97":{"category":"troop","is_siege":false,"name":"Apprentice Warden"},"troop:98":{"category":"troop","is_siege":false,"name":"Super Hog Rider"}}'::jsonb
)
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS exact_armies (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    identity_hash text NOT NULL UNIQUE CHECK (identity_hash ~ '^[0-9a-f]{64}$'),
    decoder_version text NOT NULL,
    catalog_version text NOT NULL,
    catalog_hash text NOT NULL CHECK (catalog_hash ~ '^[0-9a-f]{64}$'),
    home_troops jsonb NOT NULL,
    spells jsonb NOT NULL,
    heroes jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS battle_army_decodes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    battle_id bigint NOT NULL REFERENCES legend_battles (id),
    evidence_id bigint NOT NULL REFERENCES battle_evidence (id),
    raw_code text,
    decoder_version text NOT NULL,
    catalog_version text NOT NULL,
    catalog_hash text NOT NULL CHECK (catalog_hash ~ '^[0-9a-f]{64}$'),
    status text NOT NULL CHECK (status IN ('decoded','failed')),
    failure_category text,
    failure_detail text,
    exact_army_id bigint REFERENCES exact_armies (id),
    identity_hash text CHECK (identity_hash ~ '^[0-9a-f]{64}$'),
    home_troops jsonb,
    spells jsonb,
    home_spells jsonb,
    cc_spells jsonb,
    siege jsonb,
    cc_troops jsonb,
    heroes jsonb,
    raw_m jsonb,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    supersedes_id bigint REFERENCES battle_army_decodes (id),
    CHECK ((status = 'decoded' AND exact_army_id IS NOT NULL AND identity_hash IS NOT NULL) OR (status = 'failed')),
    CHECK ((status = 'failed' AND failure_category IS NOT NULL) OR (status = 'decoded'))
);
CREATE UNIQUE INDEX IF NOT EXISTS battle_army_decodes_one_active_per_battle
    ON battle_army_decodes (battle_id, decoder_version, catalog_version) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS battle_army_decodes_evidence ON battle_army_decodes (evidence_id);
CREATE INDEX IF NOT EXISTS battle_army_decodes_identity ON battle_army_decodes (identity_hash) WHERE status = 'decoded';

-- Army analytics: overall is stored with exact_trophies = -1 sentinel (overall), cohorts use actual trophy value.
CREATE TABLE IF NOT EXISTS army_analytics_day_summaries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ranked_day_start timestamptz NOT NULL,
    official_season_id text NOT NULL,
    exact_trophies integer NOT NULL CHECK (exact_trophies >= -1),
    total_attacks integer NOT NULL CHECK (total_attacks >= 0),
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    excluded_attacks integer NOT NULL CHECK (excluded_attacks >= 0),
    excluded_breakdown jsonb NOT NULL DEFAULT '{}'::jsonb,
    decoder_version text NOT NULL,
    catalog_version text NOT NULL,
    catalog_hash text NOT NULL,
    analytics_rule_version text NOT NULL,
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL DEFAULT 1,
    is_published boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    supersedes_id bigint REFERENCES army_analytics_day_summaries (id)
);
-- keep history: only one published per day/trophy/version set
DROP INDEX IF EXISTS army_analytics_day_summaries_ranked_day_start_exact_trophies_decoder_version_catalog_version_analytics_rule_version_key;
ALTER TABLE army_analytics_day_summaries DROP CONSTRAINT IF EXISTS army_analytics_day_summaries_ranked_day_start_exact_trophies_decoder_version_catalog_version_analytics_rule_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS army_day_summaries_published_unique
    ON army_analytics_day_summaries (ranked_day_start, exact_trophies, decoder_version, catalog_version, analytics_rule_version)
    WHERE is_published = true;
CREATE INDEX IF NOT EXISTS army_day_summaries_season ON army_analytics_day_summaries (official_season_id, ranked_day_start);
CREATE INDEX IF NOT EXISTS army_day_summaries_history ON army_analytics_day_summaries (ranked_day_start, exact_trophies, is_published);

CREATE TABLE IF NOT EXISTS army_analytics_season_summaries (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    official_season_id text NOT NULL,
    exact_trophies integer NOT NULL CHECK (exact_trophies >= -1),
    total_attacks integer NOT NULL CHECK (total_attacks >= 0),
    sample_size integer NOT NULL CHECK (sample_size >= 0),
    excluded_attacks integer NOT NULL CHECK (excluded_attacks >= 0),
    excluded_breakdown jsonb NOT NULL DEFAULT '{}'::jsonb,
    decoder_version text NOT NULL,
    catalog_version text NOT NULL,
    catalog_hash text NOT NULL,
    analytics_rule_version text NOT NULL,
    result_hash text NOT NULL CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    version integer NOT NULL DEFAULT 1,
    is_published boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    supersedes_id bigint REFERENCES army_analytics_season_summaries (id)
);
DROP INDEX IF EXISTS army_analytics_season_summaries_official_season_id_exact_trophies_decoder_version_catalog_version_analytics_rule_version_key;
ALTER TABLE army_analytics_season_summaries DROP CONSTRAINT IF EXISTS army_analytics_season_summaries_official_season_id_exact_trophies_decoder_version_catalog_version_analytics_rule_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS army_season_summaries_published_unique
    ON army_analytics_season_summaries (official_season_id, exact_trophies, decoder_version, catalog_version, analytics_rule_version)
    WHERE is_published = true;
CREATE INDEX IF NOT EXISTS army_season_history ON army_analytics_season_summaries (official_season_id, exact_trophies, is_published);

CREATE TABLE IF NOT EXISTS army_analytics_breakdowns (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    summary_kind text NOT NULL CHECK (summary_kind IN ('day','season')),
    summary_id bigint NOT NULL,
    category text NOT NULL CHECK (category IN ('home_troop','spell','hero','pet','equipment','equipment_for_hero','siege','cc_troop','hero_pet','hero_equipment','cc_composition')),
    typed_id text,
    hero_typed_id text,
    combination_key text,
    usage_count integer NOT NULL CHECK (usage_count >= 0),
    usage_rate numeric(8,7),
    star_counts jsonb NOT NULL,
    star_rates jsonb NOT NULL,
    avg_destruction numeric(6,3),
    hit_rate numeric(8,7),
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE UNIQUE INDEX IF NOT EXISTS army_breakdowns_unique
    ON army_analytics_breakdowns (summary_kind, summary_id, category, COALESCE(typed_id,''), COALESCE(hero_typed_id,''), COALESCE(combination_key,''));
CREATE INDEX IF NOT EXISTS army_breakdowns_summary ON army_analytics_breakdowns (summary_kind, summary_id);

ALTER TABLE python_processing_jobs DROP CONSTRAINT IF EXISTS python_processing_jobs_work_type_v2_check;
ALTER TABLE python_processing_jobs ADD CONSTRAINT python_processing_jobs_work_type_v2_check CHECK (work_type IN (
    'process_observation','replay_observation','reconcile_ranked_day','build_snapshot','build_analytics','build_export','build_army_analytics','redecode_army'
));

ALTER TABLE python_processing_jobs DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v2_check;
ALTER TABLE python_processing_jobs ADD CONSTRAINT python_processing_jobs_input_v2_check CHECK (
    jsonb_typeof(input_json) = 'object'
    AND COALESCE(CASE work_type
        WHEN 'process_observation' THEN input_json = '{}'::jsonb
        WHEN 'replay_observation' THEN jsonb_typeof(input_json -> 'replay_request_id') = 'number' AND (input_json ->> 'replay_request_id')::bigint > 0
        WHEN 'reconcile_ranked_day' THEN jsonb_typeof(input_json -> 'player_id') = 'number' AND (input_json ->> 'player_id')::bigint > 0 AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
        WHEN 'build_snapshot' THEN input_json ->> 'boundary_at' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$' AND (jsonb_typeof(input_json -> 'ranked_day_version_id') = 'number' AND (input_json ->> 'ranked_day_version_id')::bigint > 0 OR NOT (input_json ? 'ranked_day_version_id'))
        WHEN 'build_analytics' THEN (jsonb_typeof(input_json -> 'snapshot_id') = 'number' AND (input_json ->> 'snapshot_id')::bigint > 0 AND jsonb_typeof(input_json -> 'snapshot_version') = 'number' AND (input_json ->> 'snapshot_version')::integer > 0 AND input_json ->> 'snapshot_input_hash' ~ '^[0-9a-f]{64}$' AND jsonb_typeof(input_json -> 'source_ranked_day_version_id') = 'number' AND (input_json ->> 'source_ranked_day_version_id')::bigint > 0) OR (jsonb_typeof(input_json -> 'selection') = 'object' AND jsonb_typeof(input_json -> 'selection' -> 'ranked_day_version_id') = 'number' AND (input_json -> 'selection' ->> 'ranked_day_version_id')::bigint > 0) OR (analytics_rule_version IN ('analytics-v1', 'legend-analytics-v1') AND deduplication_key LIKE 'analytics:%' AND jsonb_typeof(input_json -> 'snapshot_id') = 'number' AND (input_json ->> 'snapshot_id')::bigint > 0 AND NOT (input_json ? 'snapshot_version') AND NOT (input_json ? 'snapshot_input_hash') AND NOT (input_json ? 'source_ranked_day_version_id'))
        WHEN 'build_export' THEN jsonb_typeof(input_json -> 'export_request_id') = 'number' AND (input_json ->> 'export_request_id')::bigint > 0
        WHEN 'build_army_analytics' THEN jsonb_typeof(input_json -> 'ranked_day_start') = 'string' AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00' AND jsonb_typeof(input_json -> 'official_season_id') = 'string' AND input_json ->> 'official_season_id' <> ''
        WHEN 'redecode_army' THEN (jsonb_typeof(input_json -> 'battle_id') = 'number' AND (input_json ->> 'battle_id')::bigint > 0) OR (jsonb_typeof(input_json -> 'battle_ids') = 'array' AND jsonb_array_length(input_json -> 'battle_ids') BETWEEN 1 AND 100)
        ELSE false END, false)
);

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
            OR (NEW.work_type = 'reconcile_ranked_day' AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_snapshot' AND NEW.analytics_rule_version = 'legend-analytics-v1')
            OR (NEW.work_type = 'build_analytics' AND NEW.analytics_rule_version = 'legend-analytics-v1' AND NEW.input_json ? 'snapshot_id' AND NEW.input_json ? 'snapshot_version' AND NEW.input_json ? 'snapshot_input_hash' AND NEW.input_json ? 'source_ranked_day_version_id' AND (NEW.input_json->>'snapshot_id') ~ '^[1-9][0-9]*$' AND (NEW.input_json->>'snapshot_version') ~ '^[1-9][0-9]*$' AND (NEW.input_json->>'source_ranked_day_version_id') ~ '^[1-9][0-9]*$' AND length(NEW.input_json->>'snapshot_input_hash') > 0)
            OR (NEW.work_type IN ('build_army_analytics','redecode_army') AND NEW.processing_version = 'clashlens-domain-processing-v1' AND NEW.domain_rule_version = 'clashlens-domain-rules-v1' AND NEW.analytics_rule_version = 'legend-analytics-v1')
         )
        THEN CASE WHEN NEW.work_type IN ('build_army_analytics','redecode_army') THEN 3 ELSE 1 END
        ELSE 0
    END;
    IF NEW.parser_version = 'supercell-source-parser-v2' AND NEW.claim_compatibility_version = 1 THEN
        NEW.claim_compatibility_version := 2;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS python_processing_jobs_claim_compatibility_v3 ON python_processing_jobs;
CREATE TRIGGER python_processing_jobs_claim_compatibility_v3
BEFORE INSERT OR UPDATE OF observation_id, replay_observation_id, work_type, parser_version, processing_version, domain_rule_version, analytics_rule_version, input_json, claim_compatibility_version
ON python_processing_jobs FOR EACH ROW EXECUTE FUNCTION clashlens_set_python_claim_compatibility_v3();

UPDATE python_processing_jobs
SET claim_compatibility_version = claim_compatibility_version
WHERE work_type IN ('build_army_analytics', 'redecode_army');

-- Bounded initial historical decode. Later decoder/catalog versions enqueue the
-- same work shape with their own versioned deduplication keys.
WITH numbered AS (
    SELECT id, (row_number() OVER (ORDER BY id) - 1) / 100 AS batch
    FROM legend_battles
), batches AS (
    SELECT batch, jsonb_agg(id ORDER BY id) AS battle_ids,
           min(id) AS first_id, max(id) AS last_id
    FROM numbered
    GROUP BY batch
)
INSERT INTO python_processing_jobs (
    work_type, deduplication_key, input_json, processing_version,
    domain_rule_version, analytics_rule_version, due_at
)
SELECT 'redecode_army',
       format(
           'redecode_army:army-decoder-v1:unit-catalog-v1:%s:%s',
           first_id, last_id
       ),
       jsonb_build_object('battle_ids', battle_ids),
       'clashlens-domain-processing-v1',
       'clashlens-domain-rules-v1',
       'legend-analytics-v1',
       clock_timestamp()
FROM batches
ON CONFLICT (deduplication_key) DO NOTHING;

DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2 ON python_processing_jobs (priority, due_at, created_at, id) WHERE status IN ('pending','waiting_retry') AND claim_compatibility_version IN (1,2,3) AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2 ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority) WHERE status = 'leased' AND claim_compatibility_version IN (1,2,3) AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_unknown_priority_v2;
CREATE INDEX python_processing_jobs_unknown_priority_v2 ON python_processing_jobs (due_at, created_at, id, priority) WHERE status IN ('pending','waiting_retry') AND claim_compatibility_version IN (1,2,3) AND attempt_count < max_attempts AND priority NOT IN (100);

CREATE OR REPLACE VIEW python_processing_jobs_worker AS SELECT id, observation_id, replay_observation_id, work_type, deduplication_key, input_json, priority, status AS state, due_at, parser_version, processing_version, domain_rule_version, snapshot_rule_version, analytics_rule_version, export_schema_version, lease_owner, lease_token, lease_expires_at, lease_generation, attempt_count, max_attempts, outcome, failure_category, failure_detail, last_error, created_at, updated_at, completed_at, claim_compatibility_version FROM python_processing_jobs;

-- Least-privilege grants for army tables (search_path aware for tests and public in production)
GRANT SELECT ON TABLE unit_catalog_versions TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE exact_armies TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE battle_army_decodes TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE army_analytics_day_summaries TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE ON TABLE army_analytics_season_summaries TO clashlens_python_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE army_analytics_breakdowns TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE exact_armies_id_seq TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE battle_army_decodes_id_seq TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE army_analytics_day_summaries_id_seq TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE army_analytics_season_summaries_id_seq TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE army_analytics_breakdowns_id_seq TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations (version) VALUES (5) ON CONFLICT (version) DO NOTHING;

COMMIT;
