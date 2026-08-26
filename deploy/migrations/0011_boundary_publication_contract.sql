-- Clash Lens deployment migration 0011.
-- Persist canonical coordinator inputs and complete the mixed-runtime contract.
BEGIN;

LOCK TABLE clash_lens_contract IN ACCESS EXCLUSIVE MODE;

CREATE TABLE IF NOT EXISTS boundary_publication_manifests (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generation_id bigint NOT NULL REFERENCES boundary_publication_generations(id),
    artifact_kind text NOT NULL CHECK (artifact_kind IN ('snapshot','army')),
    rule_versions jsonb NOT NULL,
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    frozen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    enqueued_at timestamptz,
    rows_sealed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (generation_id, artifact_kind),
    UNIQUE (generation_id, artifact_kind, digest)
);
CREATE TABLE IF NOT EXISTS boundary_publication_manifest_rows (
    manifest_id bigint NOT NULL REFERENCES boundary_publication_manifests(id) ON DELETE RESTRICT,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    player_id bigint NOT NULL REFERENCES players(id),
    ranked_day_version_id bigint REFERENCES ranked_day_versions(id),
    input_hash text CHECK (input_hash IS NULL OR input_hash ~ '^[0-9a-f]{64}$'),
    classification text NOT NULL CHECK (classification IN ('Complete','Partial','Malformed','Inconsistent','Missing','Unavailable','Failed','Pending')),
    unavailable_reason text,
    input_identity jsonb NOT NULL,
    PRIMARY KEY (manifest_id, player_id),
    UNIQUE (manifest_id, ordinal)
);
CREATE INDEX IF NOT EXISTS boundary_publication_manifest_rows_order
    ON boundary_publication_manifest_rows (manifest_id, ordinal, player_id);
ALTER TABLE boundary_publication_manifest_rows
    DROP CONSTRAINT IF EXISTS boundary_publication_manifest_rows_classification_check;
ALTER TABLE boundary_publication_manifest_rows
    ADD CONSTRAINT boundary_publication_manifest_rows_classification_check
    CHECK (classification IN ('Complete','Partial','Malformed','Inconsistent','Missing','Unavailable','Failed','Pending'));
ALTER TABLE boundary_publication_manifests
    ADD COLUMN IF NOT EXISTS rows_sealed boolean NOT NULL DEFAULT false;
UPDATE boundary_publication_manifests
SET rows_sealed = true
WHERE rows_sealed = false;

ALTER TABLE leaderboard_snapshots
    ADD COLUMN IF NOT EXISTS excluded_unavailable_count integer NOT NULL DEFAULT 0
    CHECK (excluded_unavailable_count >= 0),
    ADD COLUMN IF NOT EXISTS excluded_partial_count integer NOT NULL DEFAULT 0
    CHECK (excluded_partial_count >= 0),
    ADD COLUMN IF NOT EXISTS excluded_inconsistent_count integer NOT NULL DEFAULT 0
    CHECK (excluded_inconsistent_count >= 0);

-- Keep every persisted coverage value immutable once a snapshot is published;
-- the base v2 trigger predates these correction classifications.
CREATE OR REPLACE FUNCTION clashlens_guard_snapshot_immutable_v2()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'published leaderboard snapshots are immutable';
    END IF;
    IF OLD.state = 'published' AND NEW.state = 'superseded' THEN
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.snapshot_kind IS DISTINCT FROM OLD.snapshot_kind
           OR NEW.boundary_at IS DISTINCT FROM OLD.boundary_at
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.correction_of_id IS DISTINCT FROM OLD.correction_of_id
           OR NEW.ordering_rule_version IS DISTINCT FROM OLD.ordering_rule_version
           OR NEW.freshness_rule_version IS DISTINCT FROM OLD.freshness_rule_version
           OR NEW.source_ranked_day_version_id IS DISTINCT FROM OLD.source_ranked_day_version_id
           OR NEW.measured_coverage IS DISTINCT FROM OLD.measured_coverage
           OR NEW.stale_entry_count IS DISTINCT FROM OLD.stale_entry_count
           OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
           OR NEW.eligible_population_count IS DISTINCT FROM OLD.eligible_population_count
           OR NEW.included_entry_count IS DISTINCT FROM OLD.included_entry_count
           OR NEW.fresh_entry_count IS DISTINCT FROM OLD.fresh_entry_count
           OR NEW.excluded_missing_count IS DISTINCT FROM OLD.excluded_missing_count
           OR NEW.excluded_invalid_count IS DISTINCT FROM OLD.excluded_invalid_count
           OR NEW.excluded_malformed_count IS DISTINCT FROM OLD.excluded_malformed_count
           OR NEW.excluded_conflicting_count IS DISTINCT FROM OLD.excluded_conflicting_count
           OR NEW.excluded_unavailable_count IS DISTINCT FROM OLD.excluded_unavailable_count
           OR NEW.excluded_partial_count IS DISTINCT FROM OLD.excluded_partial_count
           OR NEW.excluded_inconsistent_count IS DISTINCT FROM OLD.excluded_inconsistent_count
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.published_at IS DISTINCT FROM OLD.published_at
        THEN
            RAISE EXCEPTION 'leaderboard snapshot fields are immutable after insert';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.state <> 'building' THEN
        RAISE EXCEPTION 'published leaderboard snapshots are immutable';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.snapshot_kind IS DISTINCT FROM OLD.snapshot_kind
       OR NEW.boundary_at IS DISTINCT FROM OLD.boundary_at
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.correction_of_id IS DISTINCT FROM OLD.correction_of_id
       OR NEW.ordering_rule_version IS DISTINCT FROM OLD.ordering_rule_version
       OR NEW.freshness_rule_version IS DISTINCT FROM OLD.freshness_rule_version
       OR NEW.source_ranked_day_version_id IS DISTINCT FROM OLD.source_ranked_day_version_id
       OR NEW.measured_coverage IS DISTINCT FROM OLD.measured_coverage
       OR NEW.stale_entry_count IS DISTINCT FROM OLD.stale_entry_count
       OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
       OR NEW.eligible_population_count IS DISTINCT FROM OLD.eligible_population_count
       OR NEW.included_entry_count IS DISTINCT FROM OLD.included_entry_count
       OR NEW.fresh_entry_count IS DISTINCT FROM OLD.fresh_entry_count
       OR NEW.excluded_missing_count IS DISTINCT FROM OLD.excluded_missing_count
       OR NEW.excluded_invalid_count IS DISTINCT FROM OLD.excluded_invalid_count
       OR NEW.excluded_malformed_count IS DISTINCT FROM OLD.excluded_malformed_count
       OR NEW.excluded_conflicting_count IS DISTINCT FROM OLD.excluded_conflicting_count
       OR NEW.excluded_unavailable_count IS DISTINCT FROM OLD.excluded_unavailable_count
       OR NEW.excluded_partial_count IS DISTINCT FROM OLD.excluded_partial_count
       OR NEW.excluded_inconsistent_count IS DISTINCT FROM OLD.excluded_inconsistent_count
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.state <> 'published'
       OR NEW.published_at IS NULL
    THEN
        RAISE EXCEPTION 'leaderboard snapshot fields are immutable after insert';
    END IF;
    RETURN NEW;
END
$$;
ALTER TABLE api_player_daily_logs
    ADD COLUMN IF NOT EXISTS ranked_day_version_id bigint REFERENCES ranked_day_versions(id);
ALTER TABLE leaderboard_snapshot_entries
    ADD COLUMN IF NOT EXISTS profile_version_id bigint REFERENCES player_profile_versions(id);
CREATE INDEX IF NOT EXISTS api_player_daily_logs_ranked_day_version
    ON api_player_daily_logs (ranked_day_version_id);

ALTER TABLE boundary_publication_generation_members
    ADD COLUMN IF NOT EXISTS snapshot_status text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS army_status text NOT NULL DEFAULT 'pending';
UPDATE boundary_publication_generation_members
SET snapshot_status = CASE status
        WHEN 'terminal' THEN 'complete'
        WHEN 'unavailable' THEN 'unavailable'
        ELSE 'pending'
    END,
    army_status = CASE status
        WHEN 'terminal' THEN 'complete'
        WHEN 'unavailable' THEN 'unavailable'
        ELSE 'pending'
    END
WHERE snapshot_status = 'pending' AND army_status = 'pending';
ALTER TABLE boundary_publication_generation_members
    DROP CONSTRAINT IF EXISTS boundary_publication_generation_members_snapshot_status_check,
    DROP CONSTRAINT IF EXISTS boundary_publication_generation_members_army_status_check;
ALTER TABLE boundary_publication_generation_members
    ADD CONSTRAINT boundary_publication_generation_members_snapshot_status_check
        CHECK (snapshot_status IN ('pending','complete','partial','failed','missing','unavailable','inconsistent','malformed')),
    ADD CONSTRAINT boundary_publication_generation_members_army_status_check
        CHECK (army_status IN ('pending','complete','partial','failed','missing','unavailable','inconsistent','malformed'));

ALTER TABLE boundary_publication_generations
    ADD COLUMN IF NOT EXISTS source_generation_id bigint REFERENCES boundary_publication_generations(id),
    ADD COLUMN IF NOT EXISTS correction_state text NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS affected_artifacts text[] NOT NULL DEFAULT ARRAY[]::text[],
    ADD COLUMN IF NOT EXISTS snapshot_manifest_id bigint REFERENCES boundary_publication_manifests(id),
    ADD COLUMN IF NOT EXISTS army_manifest_id bigint REFERENCES boundary_publication_manifests(id),
    ADD COLUMN IF NOT EXISTS snapshot_analytics_publication_id bigint,
    ADD COLUMN IF NOT EXISTS army_publication_id bigint,
    ADD COLUMN IF NOT EXISTS snapshot_coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS army_coverage jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS collector_jobs_parent_attempt_pending
    ON collector_jobs (parent_attempt_id, id)
    WHERE status IN ('pending','leased','waiting_retry','waiting_dependency');
CREATE INDEX IF NOT EXISTS collector_jobs_sweep_pending
    ON collector_jobs (sweep_id, id)
    WHERE status IN ('pending','leased','waiting_retry','waiting_dependency');
ALTER TABLE collector_reset_sweeps
    ADD COLUMN IF NOT EXISTS membership_rule_version text NOT NULL DEFAULT 'active-members-v1',
    ADD COLUMN IF NOT EXISTS membership_captured_at timestamptz;
ALTER TABLE boundary_publication_generations
    ADD COLUMN IF NOT EXISTS membership_rule_version text NOT NULL DEFAULT 'active-members-v1',
    ADD COLUMN IF NOT EXISTS membership_captured_at timestamptz,
    ADD COLUMN IF NOT EXISTS snapshot_rule_version text NOT NULL DEFAULT 'legend-analytics-v1',
    ADD COLUMN IF NOT EXISTS army_rule_version text NOT NULL DEFAULT 'army-analytics-v2',
    ADD COLUMN IF NOT EXISTS target_rule text NOT NULL DEFAULT 'boundary-delay-v1',
    ADD COLUMN IF NOT EXISTS target_at timestamptz;
UPDATE collector_reset_sweeps
SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
WHERE EXISTS (
    SELECT 1 FROM collector_reset_sweep_members AS member
    WHERE member.sweep_id = collector_reset_sweeps.id
);
UPDATE boundary_publication_generations AS generation
SET membership_rule_version = sweep.membership_rule_version,
    membership_captured_at = COALESCE(generation.membership_captured_at, clock_timestamp()),
    target_at = COALESCE(
        generation.target_at,
        generation.boundary_at + CASE
            WHEN EXTRACT(ISODOW FROM generation.boundary_at AT TIME ZONE 'UTC') = 1
                THEN interval '10 minutes'
            ELSE interval '5 minutes'
        END
    )
FROM collector_reset_sweeps AS sweep
WHERE generation.sweep_id = sweep.id;
UPDATE boundary_publication_generations
SET target_at = COALESCE(
        target_at,
        boundary_at + CASE
            WHEN EXTRACT(ISODOW FROM boundary_at AT TIME ZONE 'UTC') = 1
                THEN interval '10 minutes'
            ELSE interval '5 minutes'
        END
    ),
    membership_captured_at = COALESCE(membership_captured_at, clock_timestamp());
ALTER TABLE boundary_publication_generations
    ALTER COLUMN target_at SET NOT NULL;

CREATE OR REPLACE FUNCTION clashlens_guard_reset_membership_capture()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'INSERT'
       OR EXISTS (
           SELECT 1 FROM collector_reset_sweeps AS sweep
           WHERE sweep.id = COALESCE(NEW.sweep_id, OLD.sweep_id)
             AND sweep.membership_captured_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'reset sweep membership is immutable after capture';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION clashlens_guard_reset_sweep_inputs()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.membership_captured_at IS NOT NULL
       AND (
           NEW.boundary_at IS DISTINCT FROM OLD.boundary_at
           OR NEW.membership_rule_version IS DISTINCT FROM OLD.membership_rule_version
           OR NEW.membership_captured_at IS DISTINCT FROM OLD.membership_captured_at
       ) THEN
        RAISE EXCEPTION 'reset sweep inputs are immutable after membership capture';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS collector_reset_sweep_inputs_guard
    ON collector_reset_sweeps;
CREATE TRIGGER collector_reset_sweep_inputs_guard
BEFORE UPDATE ON collector_reset_sweeps
FOR EACH ROW EXECUTE FUNCTION clashlens_guard_reset_sweep_inputs();
DROP TRIGGER IF EXISTS collector_reset_sweep_membership_capture_guard
    ON collector_reset_sweep_members;
CREATE TRIGGER collector_reset_sweep_membership_capture_guard
BEFORE INSERT OR UPDATE OR DELETE ON collector_reset_sweep_members
FOR EACH ROW EXECUTE FUNCTION clashlens_guard_reset_membership_capture();

CREATE OR REPLACE FUNCTION clashlens_guard_generation_capture()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'boundary generation membership is immutable after capture';
    END IF;
    IF TG_OP = 'INSERT'
       AND EXISTS (
           SELECT 1 FROM boundary_publication_generations AS generation
           WHERE generation.id = NEW.generation_id
             AND generation.membership_captured_at IS NOT NULL
       ) THEN
        RAISE EXCEPTION 'boundary generation membership is immutable after capture';
    END IF;
    IF TG_OP = 'UPDATE'
       AND (
           NEW.generation_id IS DISTINCT FROM OLD.generation_id
           OR NEW.player_id IS DISTINCT FROM OLD.player_id
           OR (
               EXISTS (
                   SELECT 1
                   FROM boundary_publication_generations AS generation
                   WHERE generation.id = OLD.generation_id
                     AND (generation.snapshot_manifest_id IS NOT NULL
                          OR generation.army_manifest_id IS NOT NULL)
               )
               AND (
                   NEW.ranked_day_version_id IS DISTINCT FROM OLD.ranked_day_version_id
                   OR NEW.ranked_day_input_hash IS DISTINCT FROM OLD.ranked_day_input_hash
               )
           )
       ) THEN
        RAISE EXCEPTION 'boundary generation source identity is immutable after publication capture';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS boundary_generation_membership_capture_guard
    ON boundary_publication_generation_members;
CREATE TRIGGER boundary_generation_membership_capture_guard
BEFORE INSERT OR UPDATE OR DELETE ON boundary_publication_generation_members
FOR EACH ROW EXECUTE FUNCTION clashlens_guard_generation_capture();

CREATE OR REPLACE FUNCTION clashlens_guard_generation_inputs()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.membership_captured_at IS NOT NULL
       AND (
           NEW.boundary_at IS DISTINCT FROM OLD.boundary_at
           OR NEW.generation IS DISTINCT FROM OLD.generation
           OR NEW.sweep_id IS DISTINCT FROM OLD.sweep_id
           OR NEW.ordering_rule_version IS DISTINCT FROM OLD.ordering_rule_version
           OR NEW.freshness_rule_version IS DISTINCT FROM OLD.freshness_rule_version
           OR NEW.expected_population_count IS DISTINCT FROM OLD.expected_population_count
           OR NEW.expected_population_hash IS DISTINCT FROM OLD.expected_population_hash
           OR NEW.membership_rule_version IS DISTINCT FROM OLD.membership_rule_version
           OR NEW.membership_captured_at IS DISTINCT FROM OLD.membership_captured_at
           OR NEW.snapshot_rule_version IS DISTINCT FROM OLD.snapshot_rule_version
           OR NEW.army_rule_version IS DISTINCT FROM OLD.army_rule_version
           OR NEW.target_rule IS DISTINCT FROM OLD.target_rule
           OR NEW.target_at IS DISTINCT FROM OLD.target_at
           OR NEW.supersedes_id IS DISTINCT FROM OLD.supersedes_id
           OR NEW.source_generation_id IS DISTINCT FROM OLD.source_generation_id
           OR (
               OLD.snapshot_manifest_id IS NOT NULL
               AND NEW.snapshot_manifest_id IS DISTINCT FROM OLD.snapshot_manifest_id
           )
           OR (
               OLD.army_manifest_id IS NOT NULL
               AND NEW.army_manifest_id IS DISTINCT FROM OLD.army_manifest_id
           )
           OR (
               OLD.snapshot_analytics_publication_id IS NOT NULL
               AND NEW.snapshot_analytics_publication_id IS DISTINCT FROM OLD.snapshot_analytics_publication_id
           )
           OR (
               OLD.army_publication_id IS NOT NULL
               AND NEW.army_publication_id IS DISTINCT FROM OLD.army_publication_id
           )
       ) THEN
        RAISE EXCEPTION 'boundary generation inputs are immutable after capture';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS boundary_generation_inputs_immutable
    ON boundary_publication_generations;
CREATE TRIGGER boundary_generation_inputs_immutable
BEFORE UPDATE ON boundary_publication_generations
FOR EACH ROW EXECUTE FUNCTION clashlens_guard_generation_inputs();

ALTER TABLE collector_boundary_admission
    ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'regular_open',
    ADD COLUMN IF NOT EXISTS reset_generation integer,
    ADD COLUMN IF NOT EXISTS handoff_at timestamptz,
    ADD COLUMN IF NOT EXISTS regular_nonterminal_count integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS reset_nonterminal_count integer NOT NULL DEFAULT 0;
ALTER TABLE collector_boundary_admission
    DROP CONSTRAINT IF EXISTS collector_boundary_admission_state_check;
ALTER TABLE collector_boundary_admission
    ADD CONSTRAINT collector_boundary_admission_state_check
    CHECK (state IN ('regular_open','regular_draining','reset_running','reset_draining','safe_handoff'));

ALTER TABLE boundary_publication_generations
    DROP CONSTRAINT IF EXISTS boundary_publication_generations_correction_state_check;
ALTER TABLE boundary_publication_generations
    ADD CONSTRAINT boundary_publication_generations_correction_state_check
    CHECK (correction_state IN ('none','active','queued','activation','pending_inputs','inheritance','finalized','terminal'));

CREATE TABLE IF NOT EXISTS boundary_publication_artifact_identities (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    generation_id bigint NOT NULL REFERENCES boundary_publication_generations(id) ON DELETE RESTRICT,
    artifact_kind text NOT NULL CHECK (artifact_kind IN ('analytics','army')),
    manifest_id bigint NOT NULL REFERENCES boundary_publication_manifests(id) ON DELETE RESTRICT,
    input_hash text NOT NULL CHECK (input_hash ~ '^[0-9a-f]{64}$'),
    source_identity jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (generation_id, artifact_kind, manifest_id)
);

CREATE OR REPLACE FUNCTION clashlens_boundary_publication_identity_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'boundary publication artifact identities are immutable';
END $$;
DROP TRIGGER IF EXISTS boundary_publication_artifact_identities_immutable
    ON boundary_publication_artifact_identities;
CREATE TRIGGER boundary_publication_artifact_identities_immutable
BEFORE UPDATE OR DELETE ON boundary_publication_artifact_identities
FOR EACH ROW EXECUTE FUNCTION clashlens_boundary_publication_identity_immutable();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'boundary_generations_snapshot_identity_fk'
    ) THEN
        ALTER TABLE boundary_publication_generations
            ADD CONSTRAINT boundary_generations_snapshot_identity_fk
            FOREIGN KEY (snapshot_analytics_publication_id)
            REFERENCES boundary_publication_artifact_identities(id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'boundary_generations_army_identity_fk'
    ) THEN
        ALTER TABLE boundary_publication_generations
            ADD CONSTRAINT boundary_generations_army_identity_fk
            FOREIGN KEY (army_publication_id)
            REFERENCES boundary_publication_artifact_identities(id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS boundary_publication_corrections (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    boundary_at timestamptz NOT NULL,
    source_generation_id bigint NOT NULL REFERENCES boundary_publication_generations(id),
    generation_id bigint REFERENCES boundary_publication_generations(id),
    affected_artifacts text[] NOT NULL DEFAULT ARRAY[]::text[],
    pending_inputs jsonb NOT NULL DEFAULT '[]'::jsonb,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued','activation','pending_inputs','inheritance','active','finalized','terminal')),
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    started_at timestamptz,
    finalized_at timestamptz
);
ALTER TABLE boundary_publication_corrections
    DROP CONSTRAINT IF EXISTS boundary_publication_corrections_state_check;
ALTER TABLE boundary_publication_corrections
    ADD CONSTRAINT boundary_publication_corrections_state_check
    CHECK (state IN ('queued','activation','pending_inputs','inheritance','active','finalized','terminal'));
CREATE UNIQUE INDEX IF NOT EXISTS boundary_publication_one_active_correction
    ON boundary_publication_corrections (boundary_at)
    WHERE state = 'active';
CREATE INDEX IF NOT EXISTS boundary_publication_corrections_queue
    ON boundary_publication_corrections (boundary_at, state, requested_at);

ALTER TABLE boundary_publication_events
    ADD COLUMN IF NOT EXISTS snapshot_analytics_publication_id bigint,
    ADD COLUMN IF NOT EXISTS army_publication_id bigint,
    ADD COLUMN IF NOT EXISTS superseded_generation integer,
    ADD COLUMN IF NOT EXISTS manifest_ids jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS rule_versions jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'boundary_events_snapshot_identity_fk'
    ) THEN
        ALTER TABLE boundary_publication_events
            ADD CONSTRAINT boundary_events_snapshot_identity_fk
            FOREIGN KEY (snapshot_analytics_publication_id)
            REFERENCES boundary_publication_artifact_identities(id);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'boundary_events_army_identity_fk'
    ) THEN
        ALTER TABLE boundary_publication_events
            ADD CONSTRAINT boundary_events_army_identity_fk
            FOREIGN KEY (army_publication_id)
            REFERENCES boundary_publication_artifact_identities(id);
    END IF;
END $$;

CREATE OR REPLACE FUNCTION clashlens_boundary_publication_event_immutable()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'boundary publication events are insert-only';
END $$;
DROP TRIGGER IF EXISTS boundary_publication_events_immutable
    ON boundary_publication_events;
CREATE TRIGGER boundary_publication_events_immutable
BEFORE UPDATE OR DELETE ON boundary_publication_events
FOR EACH ROW EXECUTE FUNCTION clashlens_boundary_publication_event_immutable();

CREATE TABLE IF NOT EXISTS boundary_publication_legacy_job_migrations (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id bigint NOT NULL REFERENCES python_processing_jobs(id),
    work_type text NOT NULL,
    previous_state text NOT NULL,
    reason text NOT NULL,
    migrated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (job_id)
);

-- Stop-migrate-restart handles every active legacy population-wide job state.
-- Member and source jobs remain readable and claimable; only the obsolete
-- global publication path is cancelled with an explicit durable reason.
INSERT INTO boundary_publication_legacy_job_migrations
    (job_id, work_type, previous_state, reason)
SELECT id, work_type, status, 'coordinator_contract_required'
FROM python_processing_jobs
WHERE work_type IN ('build_snapshot','build_analytics','build_army_analytics')
  AND status IN ('pending','leased','waiting_retry','waiting_dependency')
  AND NOT (
      (work_type = 'build_snapshot' AND input_json ? 'generation' AND input_json ? 'manifest_id' AND input_json ? 'manifest_digest')
      OR (work_type = 'build_analytics' AND input_json ? 'boundary_at' AND input_json ? 'generation' AND input_json ? 'manifest_id' AND input_json ? 'manifest_digest')
      OR (work_type = 'build_army_analytics' AND input_json ? 'boundary_at' AND input_json ? 'generation' AND input_json ? 'manifest_id' AND input_json ? 'manifest_digest')
  )
ON CONFLICT (job_id) DO NOTHING;
UPDATE python_processing_jobs AS job
SET status = 'cancelled',
    outcome = 'cancelled',
    failure_category = 'coordinator_contract_required',
    failure_detail = 'legacy population-wide publication job retired during coordinator contract migration',
    lease_owner = NULL,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = clock_timestamp(),
    completed_at = clock_timestamp()
WHERE job.id IN (SELECT job_id FROM boundary_publication_legacy_job_migrations)
  AND job.status IN ('pending','leased','waiting_retry','waiting_dependency');
CREATE OR REPLACE FUNCTION clashlens_freeze_boundary_manifest_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows'
       AND TG_OP IN ('UPDATE', 'DELETE')
       AND EXISTS (
           SELECT 1 FROM boundary_publication_manifests AS manifest
           WHERE manifest.id = (to_jsonb(OLD)->>'manifest_id')::bigint
             AND manifest.rows_sealed
       ) THEN
        RAISE EXCEPTION 'boundary publication manifest rows are immutable after sealing';
    END IF;
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows'
       AND TG_OP IN ('INSERT', 'UPDATE')
       AND EXISTS (
           SELECT 1 FROM boundary_publication_manifests AS manifest
           WHERE manifest.id = (to_jsonb(NEW)->>'manifest_id')::bigint
             AND manifest.rows_sealed
       ) THEN
        RAISE EXCEPTION 'boundary publication manifest rows are immutable after sealing';
    END IF;
    IF TG_TABLE_NAME = 'boundary_publication_manifest_rows' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE'
       AND TG_TABLE_NAME = 'boundary_publication_manifests'
       AND COALESCE((to_jsonb(OLD)->>'rows_sealed')::boolean, false) = false
       AND COALESCE((to_jsonb(NEW)->>'rows_sealed')::boolean, false)
       AND to_jsonb(NEW)->'generation_id' = to_jsonb(OLD)->'generation_id'
       AND to_jsonb(NEW)->'artifact_kind' = to_jsonb(OLD)->'artifact_kind'
       AND to_jsonb(NEW)->'rule_versions' = to_jsonb(OLD)->'rule_versions'
       AND to_jsonb(NEW)->'digest' = to_jsonb(OLD)->'digest'
       AND to_jsonb(NEW)->'created_at' = to_jsonb(OLD)->'created_at' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'boundary publication manifests are immutable after rows are sealed';
END $$;
DROP TRIGGER IF EXISTS boundary_publication_manifests_immutable
    ON boundary_publication_manifests;
CREATE TRIGGER boundary_publication_manifests_immutable
BEFORE UPDATE OR DELETE ON boundary_publication_manifests
FOR EACH ROW EXECUTE FUNCTION clashlens_freeze_boundary_manifest_guard();
DROP TRIGGER IF EXISTS boundary_publication_manifest_rows_immutable
    ON boundary_publication_manifest_rows;
CREATE TRIGGER boundary_publication_manifest_rows_immutable
BEFORE INSERT OR UPDATE OR DELETE ON boundary_publication_manifest_rows
FOR EACH ROW EXECUTE FUNCTION clashlens_freeze_boundary_manifest_guard();

-- Require the persisted manifest identity on every new coordinator job while
-- retaining legacy fixture/history shapes for reads and explicit migration.
ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v4_check;
ALTER TABLE python_processing_jobs
    DROP CONSTRAINT IF EXISTS python_processing_jobs_input_v5_check;
ALTER TABLE python_processing_jobs
    ADD CONSTRAINT python_processing_jobs_input_v5_check CHECK (
        jsonb_typeof(input_json) = 'object'
        AND COALESCE(CASE work_type
            WHEN 'process_observation' THEN input_json = '{}'::jsonb
            WHEN 'replay_observation' THEN
                jsonb_typeof(input_json -> 'replay_request_id') = 'number'
                AND (input_json ->> 'replay_request_id')::bigint > 0
            WHEN 'reconcile_ranked_day' THEN
                jsonb_typeof(input_json -> 'player_id') = 'number'
                AND (input_json ->> 'player_id')::bigint > 0
                AND input_json ->> 'ranked_day_start' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T05:00:00Z$'
            WHEN 'build_snapshot' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'build_analytics' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'build_export' THEN
                jsonb_typeof(input_json -> 'export_request_id') = 'number'
                AND (input_json ->> 'export_request_id')::bigint > 0
            WHEN 'build_army_analytics' THEN
                status IN ('complete','failed','cancelled')
                OR (
                    jsonb_typeof(input_json -> 'generation') = 'number'
                    AND (input_json ->> 'generation')::integer > 0
                    AND jsonb_typeof(input_json -> 'manifest_id') = 'number'
                    AND (input_json ->> 'manifest_id')::bigint > 0
                    AND input_json ->> 'manifest_digest' ~ '^[0-9a-f]{64}$'
                )
            WHEN 'redecode_army' THEN
                (jsonb_typeof(input_json -> 'battle_id') = 'number'
                 AND (input_json ->> 'battle_id')::bigint > 0)
                OR (jsonb_typeof(input_json -> 'battle_ids') = 'array'
                    AND jsonb_array_length(input_json -> 'battle_ids') BETWEEN 1 AND 100)
            ELSE false
        END, false)
    ) NOT VALID;

ALTER TABLE python_processing_jobs
    VALIDATE CONSTRAINT python_processing_jobs_input_v5_check;

-- Existing active legacy rows are retired below before this check is validated.
-- New rows are checked immediately even while the populated upgrade is open.

-- A pre-contract worker can finish member work, but cannot create a new
-- population-wide job once PostgreSQL owns coordinator admission. Historical
-- terminal rows remain readable; every new active population-wide row needs a
-- complete generation/manifest identity regardless of its deduplication key.
CREATE OR REPLACE FUNCTION clashlens_fence_legacy_boundary_publication_write()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE current_version integer;
BEGIN
    SELECT version INTO current_version FROM clash_lens_contract WHERE singleton;
    IF current_version >= 4
       AND NEW.work_type IN ('build_snapshot','build_analytics','build_army_analytics')
       AND NEW.status IN ('pending','leased','waiting_retry','waiting_dependency')
       AND NOT (
           NEW.input_json ? 'generation'
           AND NEW.input_json ? 'manifest_id'
           AND NEW.input_json ? 'manifest_digest'
       ) THEN
        RAISE EXCEPTION 'population-wide publication write requires coordinator manifest contract 4';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS fence_legacy_boundary_publication_write
    ON python_processing_jobs;
CREATE TRIGGER fence_legacy_boundary_publication_write
BEFORE INSERT OR UPDATE OF status, work_type, input_json, deduplication_key
ON python_processing_jobs
FOR EACH ROW EXECUTE FUNCTION clashlens_fence_legacy_boundary_publication_write();

-- The v4 image is the only claimant for rows classified after this migration.
-- The old trigger remains the source of the detailed classification; this
-- ordered fence makes the old image's (1,2,3) claim predicate miss the row.
CREATE OR REPLACE FUNCTION clashlens_raise_python_claim_compatibility_v4()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.input_json ? 'generation' AND NEW.input_json ? 'manifest_id'
       AND NEW.input_json ? 'manifest_digest' THEN
        NEW.claim_compatibility_version := 4;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS zz_python_processing_jobs_claim_compatibility_v4
    ON python_processing_jobs;
CREATE TRIGGER zz_python_processing_jobs_claim_compatibility_v4
BEFORE INSERT OR UPDATE OF observation_id, replay_observation_id, work_type,
    parser_version, processing_version, domain_rule_version,
    analytics_rule_version, input_json, claim_compatibility_version
ON python_processing_jobs
FOR EACH ROW EXECUTE FUNCTION clashlens_raise_python_claim_compatibility_v4();
UPDATE python_processing_jobs
SET claim_compatibility_version = 4
WHERE input_json ? 'generation'
  AND input_json ? 'manifest_id'
  AND input_json ? 'manifest_digest';

DROP INDEX IF EXISTS python_processing_jobs_pending_claim_v2;
CREATE INDEX python_processing_jobs_pending_claim_v2
    ON python_processing_jobs (priority, due_at, created_at, id)
    WHERE status IN ('pending','waiting_retry','waiting_dependency')
      AND claim_compatibility_version IN (1,2,3,4)
      AND attempt_count < max_attempts;
DROP INDEX IF EXISTS python_processing_jobs_expired_leases_v2;
CREATE INDEX python_processing_jobs_expired_leases_v2
    ON python_processing_jobs (lease_expires_at, due_at, created_at, id, priority)
    WHERE status = 'leased' AND claim_compatibility_version IN (1,2,3,4)
      AND attempt_count < max_attempts;

-- Contract 4 is the shared Go/Python coordinator contract. Existing raw
-- evidence, replay, correction and publication history remains untouched.
UPDATE clash_lens_contract SET version = 4 WHERE singleton;
INSERT INTO clash_lens_schema_migrations(version) VALUES (11)
ON CONFLICT (version) DO NOTHING;

GRANT SELECT, INSERT, UPDATE ON boundary_publication_manifests,
    boundary_publication_manifest_rows, boundary_publication_artifact_identities,
    boundary_publication_corrections,
    boundary_publication_legacy_job_migrations
    TO clashlens_python_worker;
GRANT USAGE, SELECT ON SEQUENCE boundary_publication_manifests_id_seq,
    boundary_publication_artifact_identities_id_seq,
    boundary_publication_corrections_id_seq,
    boundary_publication_legacy_job_migrations_id_seq
    TO clashlens_python_worker;
GRANT SELECT, UPDATE ON boundary_publication_generations,
    boundary_publication_events TO clashlens_python_worker;
GRANT SELECT ON collector_boundary_admission TO clashlens_python_worker;
-- The collector creates generation 1 in the same transaction as the reset
-- sweep. Keep this grant limited to immutable capture rows and their identity
-- sequences; publication mutation remains a Python-worker responsibility.
GRANT SELECT ON collector_reset_sweep_members TO clashlens_collector;
REVOKE ALL PRIVILEGES ON boundary_publication_generations,
    boundary_publication_generation_members FROM clashlens_collector;
GRANT SELECT (id, boundary_at, generation, membership_captured_at)
    ON boundary_publication_generations TO clashlens_collector;
GRANT INSERT (
    boundary_at, generation, sweep_id, ordering_rule_version,
    freshness_rule_version, expected_population_count, expected_population_hash,
    membership_rule_version, snapshot_rule_version, army_rule_version,
    target_rule, target_at
) ON boundary_publication_generations TO clashlens_collector;
GRANT UPDATE (membership_captured_at)
    ON boundary_publication_generations TO clashlens_collector;
GRANT SELECT (generation_id, player_id)
    ON boundary_publication_generation_members TO clashlens_collector;
GRANT INSERT (generation_id, player_id)
    ON boundary_publication_generation_members TO clashlens_collector;
GRANT USAGE, SELECT ON SEQUENCE boundary_publication_generations_id_seq
    TO clashlens_collector;
GRANT SELECT ON boundary_publication_manifests,
    boundary_publication_manifest_rows, boundary_publication_artifact_identities,
    boundary_publication_corrections,
    boundary_publication_events TO clashlens_python_api;

COMMIT;
