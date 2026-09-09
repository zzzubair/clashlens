-- Clash Lens deployment migration 0022.
-- Bounded regular-admission evidence for Step 9 validation. One run header
-- plus one row per scheduleDueRegular invocation inside the configured
-- capture interval. Disabled by default: the scheduler keeps its existing
-- single statement when no run is configured. No contract-version bump: this
-- is an optional backward-compatible table pair. No automatic deletion.
BEGIN;

CREATE TABLE IF NOT EXISTS collector_regular_admission_evidence_runs (
    run_id text PRIMARY KEY CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
    capture_start timestamptz NOT NULL,
    capture_end timestamptz NOT NULL,
    max_events integer NOT NULL CHECK (max_events BETWEEN 1 AND 108000),
    max_selected_entries bigint NOT NULL CHECK (max_selected_entries BETWEEN 1 AND 5000000),
    events_written integer NOT NULL DEFAULT 0,
    selected_entries_written bigint NOT NULL DEFAULT 0,
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'capacity_exceeded', 'capture_out_of_range')),
    stopped_at timestamptz,
    failure_code text,
    CHECK (capture_end > capture_start AND capture_end <= capture_start + interval '30 hours'),
    CHECK (events_written >= 0 AND events_written <= max_events),
    CHECK (selected_entries_written >= 0 AND selected_entries_written <= max_selected_entries),
    CHECK (
        (state = 'active' AND stopped_at IS NULL AND failure_code IS NULL)
        OR (state = 'capacity_exceeded' AND stopped_at IS NOT NULL AND failure_code = 'admission_evidence_capacity_exceeded')
        OR (state = 'capture_out_of_range' AND stopped_at IS NOT NULL AND failure_code = 'admission_evidence_capture_out_of_range')
    )
);

CREATE TABLE IF NOT EXISTS collector_regular_admission_evidence (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text NOT NULL REFERENCES collector_regular_admission_evidence_runs (run_id) ON DELETE RESTRICT,
    invocation_id text NOT NULL CHECK (invocation_id ~ '^[0-9a-f]{32,64}$'),
    capture_start timestamptz NOT NULL,
    capture_end timestamptz NOT NULL,
    cycle_at timestamptz NOT NULL,
    scheduler_at timestamptz NOT NULL,
    database_at timestamptz NOT NULL,
    gate_allowed boolean NOT NULL,
    gate_handoff_at timestamptz,
    batch_limit integer NOT NULL CHECK (batch_limit BETWEEN 1 AND 1000),
    visible_due_count integer NOT NULL CHECK (visible_due_count >= 0),
    visible_due_min_at timestamptz,
    unselected_visible_due_count integer NOT NULL CHECK (unselected_visible_due_count >= 0),
    unselected_visible_due_min_at timestamptz,
    unselected_visible_past_deadline_count integer NOT NULL CHECK (unselected_visible_past_deadline_count >= 0),
    unselected_visible_past_deadline_min_at timestamptz,
    selected_past_deadline_count integer NOT NULL CHECK (selected_past_deadline_count >= 0),
    selected_player_ids bigint[] NOT NULL,
    selected_due_ats timestamptz[] NOT NULL,
    selected_profile_version_ids bigint[] NOT NULL,
    selected_eligibility_states text[] NOT NULL,
    inserted_job_ids bigint[] NOT NULL,
    advanced_count integer NOT NULL CHECK (advanced_count >= 0),
    UNIQUE (run_id, invocation_id),
    CHECK (capture_end > capture_start AND capture_end <= capture_start + interval '30 hours'),
    CHECK (database_at >= capture_start AND database_at < capture_end),
    CHECK (cycle_at = date_bin(interval '5 minutes', cycle_at, timestamptz '2000-01-01 00:00:00+00')),
    CHECK (scheduler_at >= cycle_at AND scheduler_at < cycle_at + interval '5 minutes'),
    CHECK ((visible_due_count = 0) = (visible_due_min_at IS NULL)),
    CHECK ((unselected_visible_due_count = 0) = (unselected_visible_due_min_at IS NULL)),
    CHECK ((unselected_visible_past_deadline_count = 0) = (unselected_visible_past_deadline_min_at IS NULL)),
    CHECK (unselected_visible_due_count <= visible_due_count),
    CHECK (unselected_visible_past_deadline_count <= unselected_visible_due_count),
    CHECK (selected_past_deadline_count <= COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (COALESCE(cardinality(selected_player_ids), 0) <= batch_limit),
    CHECK (COALESCE(cardinality(selected_due_ats), 0) = COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (COALESCE(cardinality(selected_profile_version_ids), 0) = COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (COALESCE(cardinality(selected_eligibility_states), 0) = COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (COALESCE(cardinality(inserted_job_ids), 0) <= COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (advanced_count = COALESCE(cardinality(selected_player_ids), 0)),
    CHECK (array_position(selected_player_ids, NULL) IS NULL),
    CHECK (array_position(selected_due_ats, NULL) IS NULL),
    CHECK (gate_allowed OR COALESCE(cardinality(selected_player_ids), 0) = 0)
);

CREATE INDEX IF NOT EXISTS collector_regular_admission_evidence_run_cycle
    ON collector_regular_admission_evidence (run_id, cycle_at, database_at, id);

REVOKE ALL ON collector_regular_admission_evidence_runs, collector_regular_admission_evidence FROM PUBLIC;
GRANT SELECT, INSERT ON collector_regular_admission_evidence_runs TO clashlens_collector;
GRANT UPDATE (events_written, selected_entries_written, state, stopped_at, failure_code)
    ON collector_regular_admission_evidence_runs TO clashlens_collector;
GRANT INSERT ON collector_regular_admission_evidence TO clashlens_collector;
GRANT USAGE, SELECT ON SEQUENCE collector_regular_admission_evidence_id_seq TO clashlens_collector;

-- The collector-owned contract stays at version five. Version six would be a
-- future breaking scheduler contract; this evidence pair is optional.
INSERT INTO clash_lens_schema_migrations(version) VALUES (22)
ON CONFLICT (version) DO NOTHING;
COMMIT;
