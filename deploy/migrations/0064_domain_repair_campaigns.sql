-- Clash Lens deployment migration 0064.
-- One saved repair campaign per Season: the reports, player days and Reset
-- publications that the 2-star/55% payout fix, the five-minute battle day
-- move, the unit catalogue v2 decodes and accepted Reset settlements change.
-- See python/src/clashlens/domain_repair.py.
--
-- Nothing is created here. An operator registers a campaign with the
-- republish-current-season command; it stays 'registered', holding nothing,
-- until activation, which refuses until every repair stage can run. While a
-- campaign is 'active' or 'paused', each of its unfinished publication items
-- holds that Reset's publication rebuilds and corrections.
--
-- Finite bookkeeping, never grown by collection: preview counts are
-- recorded at registration time.
BEGIN;

CREATE TABLE IF NOT EXISTS domain_repair_campaigns (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    official_season_id text NOT NULL UNIQUE CHECK (official_season_id <> ''),
    season_start timestamptz NOT NULL,
    season_end timestamptz NOT NULL
        CHECK (season_end = season_start + interval '28 days'),
    -- No campaign write at or after the Season's end plus seven days.
    write_deadline timestamptz NOT NULL
        CHECK (write_deadline = season_end + interval '7 days'),
    state text NOT NULL DEFAULT 'registered'
        CHECK (state IN ('registered', 'active', 'paused', 'complete')),
    -- Parser, trophy rule, battle day, catalogue and decoder versions the
    -- repair must reach, pinned when the inventory was read.
    target_versions jsonb NOT NULL CHECK (jsonb_typeof(target_versions) = 'object'),
    inventory_cutoff timestamptz NOT NULL,
    plan_digest text NOT NULL CHECK (plan_digest ~ '^[0-9a-f]{64}$'),
    counts jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(counts) = 'object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS domain_repair_items (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id bigint NOT NULL REFERENCES domain_repair_campaigns (id),
    -- source: one report to read again; decode_batch: up to 100 battles
    -- needing only a catalogue v2 decode; day: one player's Legend day;
    -- publication: one Reset's publication, held while unfinished.
    kind text NOT NULL
        CHECK (kind IN ('source', 'decode_batch', 'day', 'publication')),
    target_key text NOT NULL CHECK (target_key <> ''),
    reasons text[] NOT NULL CHECK (
        cardinality(reasons) > 0
        AND reasons <@ ARRAY['payout', 'moved', 'catalogue', 'settlement',
                             'dependency']::text[]
    ),
    player_id bigint,
    battle_id bigint,
    evidence_id bigint,
    observation_id bigint,
    ranked_day_start timestamptz,
    from_day timestamptz,
    to_day timestamptz,
    official_season_id text,
    boundary_at timestamptz,
    battle_ids bigint[] CHECK (cardinality(battle_ids) BETWEEN 1 AND 100),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'excluded', 'done', 'failed')),
    exclusion text CHECK (exclusion IN (
        'raw_unavailable', 'season_finalized', 'window_expired'
    )),
    CHECK ((state = 'excluded') = (exclusion IS NOT NULL)),
    CHECK (kind <> 'publication' OR boundary_at IS NOT NULL),
    UNIQUE (campaign_id, kind, target_key)
);

CREATE INDEX IF NOT EXISTS domain_repair_items_campaign_state
    ON domain_repair_items (campaign_id, kind, state);
-- The publication hold reads this on every Reset rebuild.
CREATE INDEX IF NOT EXISTS domain_repair_items_held_boundary
    ON domain_repair_items (boundary_at)
    WHERE kind = 'publication' AND state IN ('pending', 'failed');

-- The repair command runs in the worker container as the worker role.
REVOKE ALL ON domain_repair_campaigns, domain_repair_items FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON domain_repair_campaigns, domain_repair_items
    TO clashlens_python_worker;
-- Registering again replaces a dormant campaign's items.
GRANT DELETE ON domain_repair_items TO clashlens_python_worker;
GRANT USAGE ON SEQUENCE domain_repair_campaigns_id_seq,
    domain_repair_items_id_seq TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (64)
ON CONFLICT (version) DO NOTHING;
COMMIT;
