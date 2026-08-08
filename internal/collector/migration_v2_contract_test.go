package collector

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/ClashLens/internal/testsupport"
)

func TestProductionMigrationTwoCreatesCompleteContractOnEmptyVersionOneDatabase(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	migrationPath := filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql")
	applySQLFile(t, ctx, connection, migrationPath)

	tables := []string{
		"collector_reset_baseline_sweeps",
		"python_processing_attempts",
		"python_processing_job_events",
		"python_replay_requests",
		"shared_api_credentials",
		"shared_api_permits",
		"shared_api_credential_events",
		"source_response_parses",
		"processed_observation_versions",
		"player_discovery_events",
		"player_profile_versions",
		"player_profile_effects",
		"observation_processing_outcomes",
		"parsed_source_payloads",
		"season_anchor_evidence",
		"legend_season_anchors",
		"known_player_discoveries",
		"battle_log_observations",
		"battle_source_rows",
		"legend_battles",
		"battle_evidence",
		"battle_perspectives",
		"reset_baseline_evidence",
		"ranked_day_versions",
		"ranked_day_adjustments",
		"official_top200_attempts",
		"official_top200_versions",
		"official_top200_entries",
		"leaderboard_snapshots",
		"leaderboard_snapshot_entries",
		"analytics_summaries",
		"analytics_breakdowns",
		"clash_lens_accounts",
		"account_provider_identities",
		"account_saved_players",
		"account_groups",
		"account_group_players",
		"private_api_requests",
		"api_refresh_requests",
		"verified_player_links",
		"player_link_verification_audits",
		"support_player_link_transfer_candidates",
		"support_player_link_transfer_audits",
		"account_export_requests",
		"api_player_daily_logs",
		"api_frozen_leaderboards",
		"api_frozen_leaderboard_entries",
	}
	for _, table := range tables {
		var registered *string
		if err := connection.QueryRow(ctx, `SELECT to_regclass($1)::text`, table).Scan(&registered); err != nil {
			t.Fatalf("look up table %s: %v", table, err)
		}
		if registered == nil {
			t.Fatalf("migration 0002 did not create %s", table)
		}
	}
	for _, currentColumn := range []struct {
		table  string
		column string
	}{
		{table: "official_top200_versions", column: "published_at"},
		{table: "leaderboard_snapshots", column: "state"},
		{table: "ranked_day_versions", column: "version"},
	} {
		var exists bool
		if err := connection.QueryRow(ctx, `
			SELECT EXISTS (
				SELECT 1 FROM information_schema.columns
				WHERE table_schema = current_schema()
				  AND table_name = $1
				  AND column_name = $2
			)
		`, currentColumn.table, currentColumn.column).Scan(&exists); err != nil {
			t.Fatalf("inspect current-version column for %s: %v", currentColumn.table, err)
		}
		if !exists {
			t.Fatalf(
				"migration 0002 did not add current-version column %s to %s",
				currentColumn.column,
				currentColumn.table,
			)
		}
	}

	var contractVersion int
	if err := connection.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&contractVersion); err != nil {
		t.Fatalf("read contract version: %v", err)
	}
	if contractVersion != 2 {
		t.Fatalf("contract version = %d, want 2", contractVersion)
	}
}

func TestProductionMigrationTwoEnforcesGlobalAndPlayerCollectorScope(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)

	var globalJobID, globalAttemptID, globalObservationID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, required_endpoint, status
		) VALUES (
			'global_player_rankings', 'global', NULL, NULL, 'normal',
			300, clock_timestamp(), 'global:1', 'global_player_rankings', 'complete'
		) RETURNING id
	`).Scan(&globalJobID); err != nil {
		t.Fatalf("insert global rankings job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, globalJobID).Scan(&globalAttemptID); err != nil {
		t.Fatalf("insert global rankings attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, player_id,
			normalized_tag, endpoint, request_method, request_path, request_query,
			request_started_at, response_completed_at, http_status, response_hash,
			archive_reference, paging_envelope_state, collector_version,
			source_adapter_version, key_label, evidence_headers
		) VALUES (
			'global:1:response', $1, $2, 'global', NULL,
			NULL, 'global_player_rankings', 'GET', '/v1/locations/global/rankings/players', 'limit=200',
			clock_timestamp(), clock_timestamp(), 200, repeat('a', 64),
			's3://evidence/sha256/aa/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
			'not_present', 'collector-v2', 'global-player-rankings-v1', 'normal-1', '{}'::jsonb
		) RETURNING id
	`, globalJobID, globalAttemptID).Scan(&globalObservationID); err != nil {
		t.Fatalf("insert global rankings observation: %v", err)
	}
	if globalObservationID < 1 {
		t.Fatalf("global observation ID = %d, want positive", globalObservationID)
	}

	assertSQLRejected(t, ctx, connection, `
		INSERT INTO collector_jobs (
			work_type, scope, normalized_tag, capacity_pool, priority, due_at,
			coalescing_key, required_endpoint, status
		) VALUES (
			'global_player_rankings', 'global', '#2PP', 'normal', 300,
			clock_timestamp(), 'invalid-global-tag', 'global_player_rankings', 'pending'
		)
	`, "global work with a player tag")
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'invalid-playerless-profile', $1, $2, 'player', NULL,
			'profile', 'GET', '/v1/players/%232PP', '', clock_timestamp(),
			clock_timestamp(), 200, repeat('b', 64), 's3://evidence/b',
			'not_applicable', 'collector-v2', 'player-profile-v1', 'normal-1', '{}'::jsonb
		)
	`, "player observation without identity", globalJobID, globalAttemptID)
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'invalid-player-id-profile', $1, $2, 'player', '#2PP',
			'profile', 'GET', '/v1/players/%232PP', '', clock_timestamp(),
			clock_timestamp(), 200, repeat('b', 64), 's3://evidence/b',
			'not_applicable', 'collector-v2', 'player-profile-v1', 'normal-1', '{}'::jsonb
		)
	`, "player observation without relational player identity", globalJobID, globalAttemptID)
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO collector_transport_failures (
			collection_job_id, attempt_id, scope, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query,
			paging_envelope_state, source_adapter_version, request_started_at,
			failed_at, failure_category, retry_state, key_label
		) VALUES (
			$1, $2, 'player', NULL, '#2PP', 'profile', 'GET',
			'/v1/players/%232PP', '', 'unknown_no_response', 'player-profile-v1',
			clock_timestamp(), clock_timestamp(), 'network', 'retryable', 'normal-1'
		)
	`, "player transport failure without relational player identity", globalJobID, globalAttemptID)
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, endpoint,
			request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'invalid-global-query', $1, $2, 'global', 'global_player_rankings',
			'GET', '/v1/locations/global/rankings/players', 'limit=199', clock_timestamp(),
			clock_timestamp(), 200, repeat('c', 64), 's3://evidence/c',
			'not_present', 'collector-v2', 'global-player-rankings-v1', 'normal-1', '{}'::jsonb
		)
	`, "global observation with wrong request query", globalJobID, globalAttemptID)
}

func TestProductionMigrationTwoEnforcesPythonWorkInputsAndObservationDeduplication(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	observationID := insertVersionTwoProfileObservation(t, ctx, connection)

	var processJobID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO python_processing_jobs (observation_id)
		VALUES ($1)
		RETURNING id
	`, observationID).Scan(&processJobID); err != nil {
		t.Fatalf("version-one observation handoff insert failed: %v", err)
	}
	var workType, deduplicationKey string
	if err := connection.QueryRow(ctx, `
		SELECT work_type, deduplication_key
		FROM python_processing_jobs
		WHERE id = $1
	`, processJobID).Scan(&workType, &deduplicationKey); err != nil {
		t.Fatalf("read version-one handoff defaults: %v", err)
	}
	if workType != "process_observation" || deduplicationKey != "process-observation:"+int64String(observationID) {
		t.Fatalf("version-one handoff defaults = %q and %q", workType, deduplicationKey)
	}
	assertSQLRejected(t, ctx, connection, `INSERT INTO python_processing_jobs (observation_id) VALUES ($1)`, "duplicate initial observation handoff", observationID)

	var replayRequestID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO python_replay_requests (
			observation_id, operator_identity, reason,
			target_parser_version, target_domain_rule_version
		) VALUES ($1, 'sudo:operator', 'adapter correction', 'profile-parser-v2', 'domain-v2')
		RETURNING id
	`, observationID).Scan(&replayRequestID); err != nil {
		t.Fatalf("insert replay audit: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			replay_observation_id, work_type, deduplication_key, input_json,
			parser_version, domain_rule_version
		) VALUES (
			$1, 'replay_observation', $2,
			jsonb_build_object('replay_request_id', $3::bigint),
			'profile-parser-v2', 'domain-v2'
		)
	`, observationID, "replay-observation:"+int64String(observationID)+":profile-parser-v2:domain-v2", replayRequestID); err != nil {
		t.Fatalf("insert replay observation work: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json, domain_rule_version
		) VALUES (
			'reconcile_ranked_day', 'ranked-day:1:2026-08-03',
			'{"player_id":1,"ranked_day_start":"2026-08-03T05:00:00Z"}'::jsonb,
			'domain-v1'
		)
	`); err != nil {
		t.Fatalf("insert ranked-day work: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json, snapshot_rule_version
		) VALUES (
			'build_snapshot', 'snapshot:2026-08-03T05:00:00Z:v1',
			'{"boundary_at":"2026-08-03T05:00:00Z"}'::jsonb,
			'snapshot-v1'
		)
	`); err != nil {
		t.Fatalf("insert snapshot work: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json, analytics_rule_version
		) VALUES (
			'build_analytics', 'analytics:snapshot:1:v1',
			'{"snapshot_id":1}'::jsonb, 'analytics-v1'
		)
	`); err != nil {
		t.Fatalf("insert analytics work: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json, export_schema_version
		) VALUES (
			'build_export', 'export:1:v1', '{"export_request_id":1}'::jsonb, 'export-v1'
		)
	`); err != nil {
		t.Fatalf("insert export work: %v", err)
	}

	assertSQLRejected(t, ctx, connection, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json
		) VALUES ('process_observation', 'missing-observation', '{}'::jsonb)
	`, "process work without an observation")
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json
		) VALUES ('reconcile_ranked_day', 'bad-ranked-day', '{}'::jsonb)
	`, "ranked-day work without typed input")
	assertSQLRejected(t, ctx, connection, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json, status,
			lease_owner, lease_token, lease_expires_at
		) VALUES (
			'build_snapshot', 'bad-lease', '{"boundary_at":"2026-08-03T05:00:00Z"}'::jsonb,
			'leased', NULL, NULL, NULL
		)
	`, "leased work without fencing fields")
}

func TestProductionMigrationTwoRetainsLegacyResetEvidenceAndSupportsPairedBaselines(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))

	var playerID, sweepID, legacyJobID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true) RETURNING id`).Scan(&playerID); err != nil {
		t.Fatalf("insert legacy reset player: %v", err)
	}
	boundary := time.Date(2026, time.August, 3, 5, 0, 0, 0, time.UTC)
	if err := connection.QueryRow(ctx, `INSERT INTO collector_reset_sweeps (boundary_at) VALUES ($1) RETURNING id`, boundary).Scan(&sweepID); err != nil {
		t.Fatalf("insert legacy reset sweep: %v", err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES ($1, $2)`, sweepID, playerID); err != nil {
		t.Fatalf("insert legacy reset member: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, sweep_id, status
		) VALUES ('reset_profile', $1, '#2PP', 'normal', 400, $2, 'legacy-reset', $3, 'pending')
		RETURNING id
	`, playerID, boundary, sweepID).Scan(&legacyJobID); err != nil {
		t.Fatalf("insert legacy reset job: %v", err)
	}

	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))

	var workType, evidenceKind string
	var baselineID int64
	if err := connection.QueryRow(ctx, `
		SELECT job.work_type, job.reset_baseline_sweep_id, baseline.evidence_kind
		FROM collector_jobs AS job
		JOIN collector_reset_baseline_sweeps AS baseline ON baseline.id = job.reset_baseline_sweep_id
		WHERE job.id = $1
	`, legacyJobID).Scan(&workType, &baselineID, &evidenceKind); err != nil {
		t.Fatalf("read retained legacy reset evidence: %v", err)
	}
	if workType != "legacy_reset_profile" || baselineID < 1 || evidenceKind != "legacy_profile_only_v1" {
		t.Fatalf("legacy reset evidence = work %q baseline %d kind %q", workType, baselineID, evidenceKind)
	}

	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, sweep_id, reset_baseline_sweep_id, status
		) VALUES (
			'reset_baseline', 'player', $1, '#2PP', 'normal', 400,
			$2, 'paired-reset', $3, $4, 'pending'
		)
	`, playerID, boundary.Add(24*time.Hour), sweepID, baselineID); err == nil {
		t.Fatal("paired reset accepted a legacy profile-only baseline identity")
	}
}

func TestProductionMigrationTwoFencesLeasesAndAuditsOperatorReset(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	var jobID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO python_processing_jobs (
			work_type, deduplication_key, input_json
		) VALUES (
			'reconcile_ranked_day', 'fence:reconcile:1',
			'{"player_id":1,"ranked_day_start":"2026-08-04T05:00:00Z"}'::jsonb
		)
		RETURNING id
	`).Scan(&jobID); err != nil {
		t.Fatalf("insert fenced Python job: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'leased', lease_owner = 'worker-a', lease_token = 'token-a',
			lease_expires_at = clock_timestamp() + interval '1 minute'
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("lease Python job: %v", err)
	}
	var generation int64
	if err := connection.QueryRow(ctx, `
		SELECT lease_generation FROM python_processing_jobs WHERE id = $1
	`, jobID).Scan(&generation); err != nil {
		t.Fatalf("read lease generation: %v", err)
	}
	if generation != 1 {
		t.Fatalf("first lease generation = %d, want 1", generation)
	}
	var attemptGeneration int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO python_processing_attempts (
			job_id, attempt_number, lease_owner, lease_token, started_at,
			lease_expires_at, state
		) VALUES (
			$1, 1, 'worker-a', 'token-a', clock_timestamp(),
			clock_timestamp() + interval '1 minute', 'running'
		)
		RETURNING lease_generation
	`, jobID).Scan(&attemptGeneration); err != nil {
		t.Fatalf("insert fenced Python attempt: %v", err)
	}
	if attemptGeneration != generation {
		t.Fatalf("attempt generation = %d, want %d", attemptGeneration, generation)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'failed', lease_owner = NULL, lease_token = NULL,
			lease_expires_at = NULL, outcome = 'failed',
			failure_category = 'fixture', failure_detail = 'safe fixture failure',
			completed_at = clock_timestamp()
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("fail Python job: %v", err)
	}
	var reset bool
	if err := connection.QueryRow(ctx, `
		SELECT clashlens_operator_reset_python_job($1, 'operator:test', 'approved replay')
	`, jobID).Scan(&reset); err != nil {
		t.Fatalf("operator reset Python job: %v", err)
	}
	if !reset {
		t.Fatal("operator reset returned false")
	}
	var state string
	var events int
	if err := connection.QueryRow(ctx, `
		SELECT job.status,
			(SELECT count(*) FROM python_processing_job_events AS event
			 WHERE event.job_id = job.id AND event.event_type = 'operator_reset')
		FROM python_processing_jobs AS job
		WHERE job.id = $1
	`, jobID).Scan(&state, &events); err != nil {
		t.Fatalf("read audited operator reset: %v", err)
	}
	if state != "pending" || events != 1 {
		t.Fatalf("operator reset state = %q with %d events", state, events)
	}
}

func TestProductionMigrationTwoReplayRequestSeamBoundaries(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)

	var roleExists, roleNotInherit bool
	if err := connection.QueryRow(ctx, `
		SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'clashlens_replay_request'),
		       COALESCE((SELECT NOT rolinherit FROM pg_roles WHERE rolname = 'clashlens_replay_request'), false)
	`).Scan(&roleExists, &roleNotInherit); err != nil {
		t.Fatalf("inspect replay request role: %v", err)
	}
	if !roleExists || !roleNotInherit {
		t.Fatalf("replay request role exists = %v, no inherit = %v", roleExists, roleNotInherit)
	}

	var noTrackedPassword bool
	if err := connection.QueryRow(ctx, `
		SELECT rolpassword IS NULL
		FROM pg_authid
		WHERE rolname = 'clashlens_replay_request'
	`).Scan(&noTrackedPassword); err != nil {
		t.Fatalf("inspect replay request role password: %v", err)
	}
	if !noTrackedPassword {
		t.Fatal("replay request role has a tracked password")
	}

	replayFunction := "clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text)"
	var replayExecute, publicExecute, supportExecute bool
	if err := connection.QueryRow(ctx, `
		SELECT COALESCE(has_function_privilege('clashlens_replay_request', $1, 'EXECUTE'), false),
		       COALESCE(has_function_privilege('public', $1, 'EXECUTE'), false),
		       COALESCE(has_function_privilege('clashlens_support_transfer', $1, 'EXECUTE'), false)
	`, replayFunction).Scan(&replayExecute, &publicExecute, &supportExecute); err != nil {
		t.Fatalf("inspect replay function privileges: %v", err)
	}
	if !replayExecute || publicExecute || supportExecute {
		t.Fatalf(
			"replay function privileges = replay %v, public %v, support %v",
			replayExecute,
			publicExecute,
			supportExecute,
		)
	}

	var mirrorPublicExecute, cleanupPublicExecute bool
	if err := connection.QueryRow(ctx, `
		SELECT COALESCE(has_function_privilege('public', 'clashlens_mirror_replay_request_from_job_v2()', 'EXECUTE'), false),
		       COALESCE(has_function_privilege('public', 'clashlens_cleanup_shared_api_permits(integer)', 'EXECUTE'), false)
	`).Scan(&mirrorPublicExecute, &cleanupPublicExecute); err != nil {
		t.Fatalf("inspect trigger and cleanup function privileges: %v", err)
	}
	if mirrorPublicExecute || cleanupPublicExecute {
		t.Fatalf(
			"trigger PUBLIC execute = %v, permit cleanup PUBLIC execute = %v",
			mirrorPublicExecute,
			cleanupPublicExecute,
		)
	}

	for _, functionName := range []string{
		"clashlens_request_python_replay_v2",
		"clashlens_mirror_replay_request_from_job_v2",
		"clashlens_cleanup_shared_api_permits",
	} {
		var searchPath string
		if err := connection.QueryRow(ctx, `
			SELECT COALESCE((SELECT proconfig::text FROM pg_proc WHERE proname = $1), '')
		`, functionName).Scan(&searchPath); err != nil {
			t.Fatalf("inspect search path for %s: %v", functionName, err)
		}
		if !strings.Contains(searchPath, "search_path") {
			t.Fatalf("function %s has no fixed search path: %s", functionName, searchPath)
		}
	}

	if _, err := connection.Exec(ctx, `
		DO $$
		DECLARE
			replay_role_name text;
		BEGIN
			FOR replay_role_name IN SELECT unnest(ARRAY[
				'clashlens_support_transfer',
				'clashlens_collector',
				'clashlens_worker',
				'clashlens_api'
			])
			LOOP
				IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = replay_role_name)
				   AND has_function_privilege(replay_role_name,
				       'clashlens_request_python_replay_v2(bigint, text, text, text, text, text, text)',
				       'EXECUTE') THEN
					RAISE EXCEPTION 'replay function executable by %', replay_role_name;
				END IF;
			END LOOP;
		END
		$$;
	`); err != nil {
		t.Fatalf("runtime roles must not execute the replay function: %v", err)
	}

	// Migration 0002 must reapply cleanly after the replay seam is present.
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	var stillReplayExecute bool
	if err := connection.QueryRow(ctx, `
		SELECT COALESCE(has_function_privilege('clashlens_replay_request', $1, 'EXECUTE'), false)
	`, replayFunction).Scan(&stillReplayExecute); err != nil {
		t.Fatalf("inspect replay function after reapplication: %v", err)
	}
	if !stillReplayExecute {
		t.Fatal("replay function privileges were lost when migration 0002 reapplied")
	}

	if _, err := connection.Exec(ctx, `SET ROLE clashlens_support_transfer`); err != nil {
		t.Fatalf("set support transfer role: %v", err)
	}
	defer func() { _, _ = connection.Exec(context.Background(), `RESET ROLE`) }()
	if _, err := connection.Exec(ctx, `
		SELECT clashlens_request_python_replay_v2(
			$1, 'operator:test', 'approved replay',
			'supercell-source-parser-v1', 'clashlens-domain-processing-v1',
			'clashlens-domain-rules-v1', 'legend-analytics-v1'
		)
	`, int64(1)); err == nil {
		t.Fatal("support role executed the replay request function")
	}
}

func TestProductionMigrationTwoReplayRequestCreatesOneRequestAndOneDeduplicatedJob(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	observationID := insertVersionTwoProfileObservation(t, ctx, connection)

	requestID, jobID, status, err := requestPythonReplayCall(
		t, ctx, connection, observationID, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	)
	if err != nil {
		t.Fatalf("request python replay: %v", err)
	}
	if requestID < 1 || jobID < 1 || status != "enqueued" {
		t.Fatalf("replay request = id %d job %d status %q", requestID, jobID, status)
	}

	var requestCount, jobCount int
	if err := connection.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM python_replay_requests WHERE observation_id = $1),
			(SELECT count(*) FROM python_processing_jobs
			 WHERE work_type = 'replay_observation' AND replay_observation_id = $1)
	`, observationID).Scan(&requestCount, &jobCount); err != nil {
		t.Fatalf("count replay rows: %v", err)
	}
	if requestCount != 1 || jobCount != 1 {
		t.Fatalf("replay rows = %d requests and %d jobs, want one each", requestCount, jobCount)
	}

	var workType, dedupKey, replayedRequestID, parserVersion, processingVersion, domainRuleVersion, analyticsRuleVersion string
	var jobObservationID *int64
	if err := connection.QueryRow(ctx, `
		SELECT work_type, deduplication_key, input_json ->> 'replay_request_id',
		       parser_version, processing_version, domain_rule_version,
		       analytics_rule_version, observation_id
		FROM python_processing_jobs
		WHERE id = $1
	`, jobID).Scan(
		&workType, &dedupKey, &replayedRequestID, &parserVersion,
		&processingVersion, &domainRuleVersion, &analyticsRuleVersion, &jobObservationID,
	); err != nil {
		t.Fatalf("read replay job: %v", err)
	}
	if workType != "replay_observation" || jobObservationID != nil {
		t.Fatalf("replay job work = %q with observation %v", workType, jobObservationID)
	}
	if dedupKey != "replay-observation:"+int64String(observationID)+":supercell-source-parser-v1:clashlens-domain-rules-v1" {
		t.Fatalf("replay deduplication key = %q", dedupKey)
	}
	if replayedRequestID != int64String(requestID) ||
		parserVersion != "supercell-source-parser-v1" ||
		processingVersion != "clashlens-domain-processing-v1" ||
		domainRuleVersion != "clashlens-domain-rules-v1" ||
		analyticsRuleVersion != "legend-analytics-v1" {
		t.Fatalf(
			"replay job = request %q parser %q processing %q domain %q analytics %q",
			replayedRequestID, parserVersion, processingVersion, domainRuleVersion, analyticsRuleVersion,
		)
	}

	replayedRequestIDValue, replayedJobID, replayedStatus, err := requestPythonReplayCall(
		t, ctx, connection, observationID, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	)
	if err != nil {
		t.Fatalf("replay exact same request: %v", err)
	}
	if replayedRequestIDValue != requestID || replayedJobID != jobID || replayedStatus != "enqueued" {
		t.Fatalf(
			"idempotent replay = request %d job %d status %q, want %d %d enqueued",
			replayedRequestIDValue, replayedJobID, replayedStatus, requestID, jobID,
		)
	}

	if _, _, _, err := requestPythonReplayCall(
		t, ctx, connection, observationID, "sudo:other:1001", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	); err == nil {
		t.Fatal("replay with a different operator reused the existing request")
	}
	if _, _, _, err := requestPythonReplayCall(
		t, ctx, connection, observationID, "sudo:operator:1000", "different bounded reason",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	); err == nil {
		t.Fatal("replay with a different reason reused the existing request")
	}

	if _, _, _, err := requestPythonReplayCall(
		t, ctx, connection, 99999999, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	); err == nil {
		t.Fatal("replay accepted a missing collector observation")
	}

	for name, versions := range map[string][4]string{
		"parser":     {"profile-parser-v2", replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion},
		"processing": {replayParserVersion, "python-processing-prototype-v1", replayDomainRuleVersion, replayAnalyticsVersion},
		"domain":     {replayParserVersion, replayProcessingVersion, "domain-v2", replayAnalyticsVersion},
		"analytics":  {replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, "analytics-v2"},
	} {
		if _, _, _, err := requestPythonReplayCall(
			t, ctx, connection, observationID, "sudo:operator:1000", "adapter correction replay",
			versions[0], versions[1], versions[2], versions[3],
		); err == nil {
			t.Fatalf("replay accepted unsupported %s target version", name)
		}
	}

	battleLogObservationID := insertVersionTwoBattleLogObservation(t, ctx, connection)
	battleRequestID, battleJobID, battleStatus, err := requestPythonReplayCall(
		t, ctx, connection, battleLogObservationID, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	)
	if err != nil {
		t.Fatalf("request battle-log replay: %v", err)
	}
	if battleRequestID == requestID || battleRequestID < 1 || battleJobID < 1 || battleStatus != "enqueued" {
		t.Fatalf(
			"battle-log replay = request %d job %d status %q, first request %d",
			battleRequestID, battleJobID, battleStatus, requestID,
		)
	}

	globalObservationID := insertVersionTwoGlobalObservation(t, ctx, connection)
	if _, _, _, err := requestPythonReplayCall(
		t, ctx, connection, globalObservationID, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	); err == nil {
		t.Fatal("replay accepted a global rankings observation")
	}
}

func TestProductionMigrationTwoReplayRequestMirrorFollowsLinkedJobStates(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	observationID := insertVersionTwoProfileObservation(t, ctx, connection)
	requestID, jobID, _, err := requestPythonReplayCall(
		t, ctx, connection, observationID, "sudo:operator:1000", "adapter correction replay",
		replayParserVersion, replayProcessingVersion, replayDomainRuleVersion, replayAnalyticsVersion,
	)
	if err != nil {
		t.Fatalf("request python replay: %v", err)
	}

	readRequest := func() (string, *time.Time) {
		t.Helper()
		var status string
		var completedAt *time.Time
		if err := connection.QueryRow(ctx, `
			SELECT status, completed_at
			FROM python_replay_requests
			WHERE id = $1
		`, requestID).Scan(&status, &completedAt); err != nil {
			t.Fatalf("read replay request status: %v", err)
		}
		return status, completedAt
	}

	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'complete', completed_at = clock_timestamp()
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("complete linked replay job: %v", err)
	}
	if status, completedAt := readRequest(); status != "complete" || completedAt == nil {
		t.Fatalf("mirrored complete state = %q completed %v", status, completedAt)
	}

	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'failed', completed_at = clock_timestamp(),
		    failure_category = 'fixture', failure_detail = 'safe fixture failure'
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("fail linked replay job: %v", err)
	}
	if status, completedAt := readRequest(); status != "failed" || completedAt == nil {
		t.Fatalf("mirrored failed state = %q completed %v", status, completedAt)
	}

	var reset bool
	if err := connection.QueryRow(ctx, `
		SELECT clashlens_operator_reset_python_job($1, 'operator:test', 'approved replay')
	`, jobID).Scan(&reset); err != nil {
		t.Fatalf("operator reset linked replay job: %v", err)
	}
	if !reset {
		t.Fatal("operator reset returned false")
	}
	if status, completedAt := readRequest(); status != "enqueued" || completedAt != nil {
		t.Fatalf("mirrored reset state = %q completed %v", status, completedAt)
	}

	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'cancelled', completed_at = clock_timestamp()
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("cancel linked replay job: %v", err)
	}
	if status, _ := readRequest(); status != "cancelled" {
		t.Fatalf("mirrored cancelled state = %q", status)
	}

	if _, err := connection.Exec(ctx, `CREATE ROLE clashlens_replay_probe_worker NOINHERIT`); err != nil {
		t.Fatalf("create replay probe worker role: %v", err)
	}
	t.Cleanup(func() {
		_, _ = connection.Exec(context.Background(), `DROP ROLE IF EXISTS clashlens_replay_probe_worker`)
	})
	if _, err := connection.Exec(ctx, `GRANT USAGE ON SCHEMA public TO clashlens_replay_probe_worker`); err != nil {
		t.Fatalf("grant probe schema usage: %v", err)
	}
	if _, err := connection.Exec(ctx, `GRANT SELECT, UPDATE ON python_processing_jobs TO clashlens_replay_probe_worker`); err != nil {
		t.Fatalf("grant probe job update: %v", err)
	}
	var probeCanUpdateRequest bool
	if err := connection.QueryRow(ctx, `
		SELECT COALESCE(has_table_privilege(
			'clashlens_replay_probe_worker', 'python_replay_requests', 'UPDATE'), false)
	`).Scan(&probeCanUpdateRequest); err != nil {
		t.Fatalf("inspect probe request update privilege: %v", err)
	}
	if probeCanUpdateRequest {
		t.Fatal("probe worker role can update python_replay_requests directly")
	}

	if _, err := connection.Exec(ctx, `SET ROLE clashlens_replay_probe_worker`); err != nil {
		t.Fatalf("set probe worker role: %v", err)
	}
	defer func() { _, _ = connection.Exec(context.Background(), `RESET ROLE`) }()
	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'pending'
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("probe worker requeue linked replay job: %v", err)
	}
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset probe worker role: %v", err)
	}
	if status, _ := readRequest(); status != "enqueued" {
		t.Fatalf("probe worker requeue mirrored state = %q", status)
	}
}

func migratedVersionTwoConnection(t *testing.T, ctx context.Context) *pgx.Conn {
	t.Helper()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	return connection
}

func insertVersionTwoProfileObservation(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
	t.Helper()
	var playerID, jobID, attemptID, observationID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag) VALUES ('#2PP') RETURNING id`).Scan(&playerID); err != nil {
		t.Fatalf("insert profile player: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('initial_collection', $1, '#2PP', 'interactive', 250, clock_timestamp(), 'profile-v2', 'complete')
		RETURNING id
	`, playerID).Scan(&jobID); err != nil {
		t.Fatalf("insert profile job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, jobID).Scan(&attemptID); err != nil {
		t.Fatalf("insert profile attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'profile-v2:1', $1, $2, $3, '#2PP', 'profile', 'GET', '/v1/players/%232PP', '',
			clock_timestamp(), clock_timestamp(), 200, repeat('d', 64), 's3://evidence/d',
			'not_applicable', 'collector-v2', 'player-profile-v1', 'interactive-1', '{}'::jsonb
		) RETURNING id
	`, jobID, attemptID, playerID).Scan(&observationID); err != nil {
		t.Fatalf("insert profile observation: %v", err)
	}
	return observationID
}

func assertSQLRejected(t *testing.T, ctx context.Context, connection *pgx.Conn, sql, description string, arguments ...any) {
	t.Helper()
	_, err := connection.Exec(ctx, sql, arguments...)
	if err == nil {
		t.Fatalf("database accepted %s", description)
	}
	if errors.Is(err, context.DeadlineExceeded) || strings.Contains(err.Error(), "connection is closed") {
		t.Fatalf("database check for %s failed for an unrelated reason: %v", description, err)
	}
}

const (
	replayParserVersion     = "supercell-source-parser-v1"
	replayProcessingVersion = "clashlens-domain-processing-v1"
	replayDomainRuleVersion = "clashlens-domain-rules-v1"
	replayAnalyticsVersion  = "legend-analytics-v1"
)

func requestPythonReplayCall(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
	observationID int64,
	operator string,
	reason string,
	parserVersion string,
	processingVersion string,
	domainRuleVersion string,
	analyticsRuleVersion string,
) (int64, int64, string, error) {
	t.Helper()
	if _, err := connection.Exec(ctx, `SET SESSION AUTHORIZATION clashlens_replay_request`); err != nil {
		t.Fatalf("set replay request session: %v", err)
	}
	defer func() { _, _ = connection.Exec(context.Background(), `RESET SESSION AUTHORIZATION`) }()
	var requestID, jobID int64
	var status string
	err := connection.QueryRow(ctx, `
		SELECT request_id, job_id, request_status
		FROM clashlens_request_python_replay_v2($1, $2, $3, $4, $5, $6, $7)
	`, observationID, operator, reason, parserVersion, processingVersion, domainRuleVersion, analyticsRuleVersion).
		Scan(&requestID, &jobID, &status)
	return requestID, jobID, status, err
}

func insertVersionTwoBattleLogObservation(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
	t.Helper()
	var playerID, jobID, attemptID, observationID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag) VALUES ('#2PY') RETURNING id`).Scan(&playerID); err != nil {
		t.Fatalf("insert battle-log player: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('initial_collection', $1, '#2PY', 'interactive', 250, clock_timestamp(), 'battle-log-v2', 'complete')
		RETURNING id
	`, playerID).Scan(&jobID); err != nil {
		t.Fatalf("insert battle-log job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, jobID).Scan(&attemptID); err != nil {
		t.Fatalf("insert battle-log attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'battle-log-v2:1', $1, $2, $3, '#2PY', 'battle_log', 'GET',
			'/v1/players/%232PY/battlelog', '', clock_timestamp(),
			clock_timestamp(), 200, repeat('e', 64), 's3://evidence/e',
			'not_applicable', 'collector-v2', 'battle-log-v1', 'interactive-1', '{}'::jsonb
		) RETURNING id
	`, jobID, attemptID, playerID).Scan(&observationID); err != nil {
		t.Fatalf("insert battle-log observation: %v", err)
	}
	return observationID
}

func insertVersionTwoGlobalObservation(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
	t.Helper()
	var globalJobID, globalAttemptID, globalObservationID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, required_endpoint, status
		) VALUES (
			'global_player_rankings', 'global', NULL, NULL, 'normal',
			300, clock_timestamp(), 'global:replay-probe', 'global_player_rankings', 'complete'
		) RETURNING id
	`).Scan(&globalJobID); err != nil {
		t.Fatalf("insert global rankings job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, globalJobID).Scan(&globalAttemptID); err != nil {
		t.Fatalf("insert global rankings attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, player_id,
			normalized_tag, endpoint, request_method, request_path, request_query,
			request_started_at, response_completed_at, http_status, response_hash,
			archive_reference, paging_envelope_state, collector_version,
			source_adapter_version, key_label, evidence_headers
		) VALUES (
			'global:replay-probe:response', $1, $2, 'global', NULL,
			NULL, 'global_player_rankings', 'GET', '/v1/locations/global/rankings/players', 'limit=200',
			clock_timestamp(), clock_timestamp(), 200, repeat('f', 64),
			's3://evidence/f', 'not_present', 'collector-v2',
			'global-player-rankings-v1', 'normal-1', '{}'::jsonb
		) RETURNING id
	`, globalJobID, globalAttemptID).Scan(&globalObservationID); err != nil {
		t.Fatalf("insert global rankings observation: %v", err)
	}
	return globalObservationID
}
