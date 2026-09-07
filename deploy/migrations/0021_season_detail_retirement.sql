-- Clash Lens deployment migration 0021.
-- Durable completed-season detail-retirement record. One row per retired
-- season stores the established boundaries, summary identities/digests, and
-- retirement progress so it survives deletion of daily logs and battle
-- facts. This migration creates the record only; no data is deleted.
BEGIN;

CREATE TABLE IF NOT EXISTS season_detail_retirements (
    official_season_id text PRIMARY KEY CHECK (
        official_season_id <> '' AND length(official_season_id) <= 128
    ),
    status text NOT NULL DEFAULT 'finalized' CHECK (
        status IN ('finalized', 'retired')
    ),
    season_start timestamptz,
    season_end timestamptz,
    player_summary_count integer NOT NULL DEFAULT 0 CHECK (
        player_summary_count >= 0
    ),
    player_summary_digest text NOT NULL DEFAULT repeat('0', 64) CHECK (
        player_summary_digest ~ '^[0-9a-f]{64}$'
    ),
    army_summary_digest text NOT NULL DEFAULT repeat('0', 64) CHECK (
        army_summary_digest ~ '^[0-9a-f]{64}$'
    ),
    finalized_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    retired_at timestamptz,
    progress jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(progress) = 'object'
    ),
    CHECK (season_end IS NULL OR season_start IS NULL OR season_end > season_start),
    CHECK (status <> 'retired' OR retired_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS season_detail_retirements_status
    ON season_detail_retirements (status);

-- Finalization fences writers; the worker and API read this table to refuse
-- retired-detail rebuilds. Operator cleanup deletes detail; summaries stay.
GRANT SELECT, INSERT, UPDATE ON TABLE season_detail_retirements
    TO clashlens_python_worker;
GRANT SELECT ON TABLE season_detail_retirements TO clashlens_python_api;

INSERT INTO clash_lens_schema_migrations(version) VALUES (21)
ON CONFLICT (version) DO NOTHING;
COMMIT;
