-- Clash Lens deployment migration 0023.
-- Bounded fixed-population bootstrap (issue #92 B2) run record and durable
-- run-scoped endpoint request budgets. One bootstrap run row pins the
-- manifest digest/count plus aggregate outcomes so a repeated run-id replays
-- idempotently and any colliding run fails closed. One budget row per
-- (run, endpoint) carries an immutable cap, a monotonic consumed counter,
-- and a deadline; reservation is a single atomic UPDATE consumed only when
-- below cap and before the deadline, and consumed is never decremented.
-- Migration 0022 stays reserved for separate admission evidence.
BEGIN;

CREATE TABLE IF NOT EXISTS population_bootstrap_runs (
    run_id text PRIMARY KEY CHECK (
        run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
    ),
    manifest_sha256 text NOT NULL CHECK (
        manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    manifest_count integer NOT NULL CHECK (
        manifest_count BETWEEN 1 AND 20000
    ),
    normalized_set_sha256 text NOT NULL CHECK (
        normalized_set_sha256 ~ '^[0-9a-f]{64}$'
    ),
    status text NOT NULL DEFAULT 'started' CHECK (
        status IN ('started', 'complete')
    ),
    batch_size integer NOT NULL CHECK (
        batch_size BETWEEN 1 AND 500
    ),
    players_registered integer NOT NULL DEFAULT 0 CHECK (
        players_registered >= 0
    ),
    discovery_jobs_created integer NOT NULL DEFAULT 0 CHECK (
        discovery_jobs_created >= 0
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (status <> 'complete' OR completed_at IS NOT NULL)
);
REVOKE ALL ON TABLE population_bootstrap_runs FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON TABLE population_bootstrap_runs
    TO clashlens_python_worker;

CREATE TABLE IF NOT EXISTS collector_endpoint_budgets (
    run_id text NOT NULL CHECK (
        run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
    ),
    endpoint text NOT NULL CHECK (
        endpoint IN ('profile', 'global_player_rankings', 'battle_log')
    ),
    cap integer NOT NULL CHECK (cap >= 0),
    consumed integer NOT NULL DEFAULT 0 CHECK (
        consumed >= 0 AND consumed <= cap
    ),
    deadline_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, endpoint)
);
REVOKE ALL ON TABLE collector_endpoint_budgets FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON TABLE collector_endpoint_budgets
    TO clashlens_collector;
-- The population monitor reads budget aggregates through the existing
-- Python-worker role; it must never mint or consume budget units.
GRANT SELECT ON TABLE collector_endpoint_budgets TO clashlens_python_worker;

-- The bootstrap precheck runs under the existing Python-worker role, so it
-- reads only the ranking cycle identity column, never ranking evidence.
GRANT SELECT (cycle_at) ON TABLE global_rankings_intents
    TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations (version) VALUES (23)
ON CONFLICT (version) DO NOTHING;
COMMIT;
