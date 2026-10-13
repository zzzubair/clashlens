-- Clash Lens deployment migration 0110.
-- What the nightly re-check of finished Legend days (reconcile_pass.py)
-- found: one row per problem with a player's saved day, battle or Daily
-- board row, which check found it, what it expected and what was saved,
-- the version of the day it judged, and when it was found, last seen,
-- given a recalculation, seen again after that recalculation, and gone.
-- A problem the re-check no longer finds is resolved, and resolved rows are
-- deleted 48 hours later by the re-check itself. About 200 bytes a row; on
-- 9 October 2026 the checks would have found about 1,000 to 3,000 problems
-- at once, well under 1 MB. Nothing else is written.
BEGIN;

CREATE TABLE IF NOT EXISTS reconcile_findings (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    check_name text NOT NULL CHECK (check_name IN (
        'recount', 'chain', 'ledger', 'copies', 'missing_day', 'board'
    )),
    player_id bigint NOT NULL,
    ranked_day_start timestamptz NOT NULL,
    -- The battle a copies finding is about, the Daily board a board finding
    -- is about, else 0.
    subject_id bigint NOT NULL DEFAULT 0,
    ranked_day_version_id bigint,
    expected jsonb NOT NULL DEFAULT '{}'::jsonb,
    saved jsonb NOT NULL DEFAULT '{}'::jsonb,
    found_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    fix_queued_at timestamptz,
    escalated_at timestamptz,
    resolved_at timestamptz
);

-- One open finding per problem; a problem that comes back after it was
-- resolved is a new finding.
CREATE UNIQUE INDEX IF NOT EXISTS reconcile_findings_open
    ON reconcile_findings (ranked_day_start, player_id, check_name, subject_id)
    WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS reconcile_findings_found
    ON reconcile_findings (found_at);
CREATE INDEX IF NOT EXISTS reconcile_findings_resolved
    ON reconcile_findings (resolved_at) WHERE resolved_at IS NOT NULL;

REVOKE ALL ON reconcile_findings FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON reconcile_findings TO clashlens_python_worker;

INSERT INTO clash_lens_schema_migrations(version) VALUES (110)
ON CONFLICT (version) DO NOTHING;
COMMIT;
