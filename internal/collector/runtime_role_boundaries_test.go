package collector

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// TestProductionMigrationsEnforceRuntimeRoleBoundaries proves the explicit
// least-privilege grant layer that the current migrations install for the three
// runtime roles: the Go collector, the Python worker, and the Python API.
// Each role is NOLOGIN with no elevated attributes and no password; positive
// probes run the role's sanctioned statements and negative probes assert
// SQLSTATE 42501 across the documented boundary matrix.
func TestProductionMigrationsEnforceRuntimeRoleBoundaries(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)

	// Reapplication must be stable: roles stay unique with fixed attributes
	// and every grant/revoke can be repeated.
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))

	const (
		collectorRole = "clashlens_collector"
		workerRole    = "clashlens_python_worker"
		apiRole       = "clashlens_python_api"
	)
	for _, role := range []string{collectorRole, workerRole, apiRole} {
		var canLogin, inherits, superuser, createdb, createrole, replication, bypassrls bool
		var password *string
		if err := connection.QueryRow(ctx, `
			SELECT r.rolcanlogin, r.rolinherit, r.rolsuper, r.rolcreatedb,
			       r.rolcreaterole, r.rolreplication, r.rolbypassrls, a.rolpassword
			FROM pg_roles AS r
			LEFT JOIN pg_authid AS a ON a.rolname = r.rolname
			WHERE r.rolname = $1
		`, role).Scan(&canLogin, &inherits, &superuser, &createdb, &createrole, &replication, &bypassrls, &password); err != nil {
			t.Fatalf("inspect role %s: %v", role, err)
		}
		if canLogin || inherits || superuser || createdb || createrole || replication || bypassrls {
			t.Fatalf("role %s has elevated attributes: login=%v inherit=%v super=%v createdb=%v createrole=%v replication=%v bypassrls=%v",
				role, canLogin, inherits, superuser, createdb, createrole, replication, bypassrls)
		}
		if password != nil {
			t.Fatalf("role %s has a tracked password", role)
		}
		var schemaUsage bool
		if err := connection.QueryRow(ctx, `
			SELECT has_schema_privilege($1, current_schema(), 'USAGE')
		`, role).Scan(&schemaUsage); err != nil {
			t.Fatalf("inspect schema usage for %s: %v", role, err)
		}
		if !schemaUsage {
			t.Fatalf("role %s lacks USAGE on the application schema", role)
		}
	}

	// Seed fixture rows as the admin owner so role probes run against real data.
	var playerID, jobID, observationID, pythonJobID, dailyLogID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag) VALUES ('#9PL') RETURNING id`).Scan(&playerID); err != nil {
		t.Fatalf("seed role-boundary player: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('initial_collection', $1, '#9PL', 'interactive', 300, clock_timestamp(), 'role-boundary', 'complete')
		RETURNING id
	`, playerID).Scan(&jobID); err != nil {
		t.Fatalf("seed role-boundary job: %v", err)
	}
	var attemptID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, jobID).Scan(&attemptID); err != nil {
		t.Fatalf("seed role-boundary attempt: %v", err)
	}
	var accountID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO clash_lens_accounts (public_id, username, normalized_username, display_name)
		VALUES ('00000000-0000-0000-0000-0000000000a1'::uuid, 'boundary-user', 'boundary_user', 'Boundary User')
		RETURNING id
	`).Scan(&accountID); err != nil {
		t.Fatalf("seed role-boundary account: %v", err)
	}
	if accountID < 1 {
		t.Fatal("seeded role-boundary account has no id")
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'role-boundary:1', $1, $2, $3, '#9PL', 'profile', 'GET', '/v1/players/%239PL', '',
			clock_timestamp(), clock_timestamp(), 200, repeat('d', 64), 's3://evidence/d',
			'not_applicable', 'collector-v2', 'player-profile-v1', 'interactive-1', '{}'::jsonb
		) RETURNING id
	`, jobID, attemptID, playerID).Scan(&observationID); err != nil {
		t.Fatalf("seed role-boundary observation: %v", err)
	}
	if err := connection.QueryRow(ctx, `INSERT INTO python_processing_jobs (observation_id) VALUES ($1) RETURNING id`, observationID).Scan(&pythonJobID); err != nil {
		t.Fatalf("seed role-boundary python job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO api_player_daily_logs (player_id, ranked_day_start, version, state, coverage)
		VALUES ($1, '2026-08-01 05:00:00+00', 1, 'Live', 'complete')
		RETURNING id
	`, playerID).Scan(&dailyLogID); err != nil {
		t.Fatalf("seed role-boundary daily log: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO shared_api_credentials (credential_fingerprint)
		VALUES (repeat('c', 64))
	`); err != nil {
		t.Fatalf("seed role-boundary credential: %v", err)
	}

	// Collector: evidence and job rows, contract reads, permit gate, canonical
	// interactive enqueue, and identity sequences; no account,
	// canonical-profile, support, or replay use.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin collector probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+collectorRole); err != nil {
		t.Fatalf("set collector role: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`); err != nil {
		t.Fatalf("collector read contract: %v", err)
	}
	var collectorJobID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('live_refresh', $1, '#9PL', 'interactive', 300, clock_timestamp(), 'role-boundary-2', 'pending')
		RETURNING id
	`, playerID).Scan(&collectorJobID); err != nil {
		t.Fatalf("collector insert job: %v", err)
	}
	if _, err := connection.Exec(ctx, `UPDATE collector_jobs SET status = 'pending' WHERE id = $1`, collectorJobID); err != nil {
		t.Fatalf("collector update job: %v", err)
	}
	var collectorAttemptID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, collectorJobID).Scan(&collectorAttemptID); err != nil {
		t.Fatalf("collector insert attempt: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query, request_started_at,
			response_completed_at, http_status, response_hash, archive_reference,
			paging_envelope_state, collector_version, source_adapter_version,
			key_label, evidence_headers
		) VALUES (
			'role-boundary:2', $1, $2, $3, '#9PL', 'profile', 'GET', '/v1/players/%239PL', '',
			clock_timestamp(), clock_timestamp(), 200, repeat('f', 64), 's3://evidence/f',
			'not_applicable', 'collector-v2', 'player-profile-v1', 'interactive-2', '{}'::jsonb
		)
	`, collectorJobID, collectorAttemptID, playerID); err != nil {
		t.Fatalf("collector insert observation: %v", err)
	}
	var permitGranted bool
	if err := connection.QueryRow(ctx, `
		SELECT granted FROM clashlens_acquire_shared_api_permit(repeat('c', 64), 'go')
	`).Scan(&permitGranted); err != nil {
		t.Fatalf("collector acquire shared permit: %v", err)
	}
	if !permitGranted {
		t.Fatal("collector permit acquisition was not granted")
	}
	if _, err := connection.Exec(ctx, `SELECT clashlens_cleanup_shared_api_permits(100)`); err != nil {
		t.Fatalf("collector cleanup shared permits: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT nextval('collector_jobs_id_seq')`); err != nil {
		t.Fatalf("collector identity sequence usage: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `SELECT count(*) FROM clash_lens_accounts`, "collector reading accounts")
	for _, table := range []string{"account_saved_players", "account_groups", "account_group_players"} {
		expectInsufficientPrivilege(t, ctx, connection, `DELETE FROM `+table+` WHERE false`, "collector deleting from "+table)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		INSERT INTO player_profile_versions (
			player_id, observation_id, normalized_tag, endpoint_version, schema_version,
			parser_version, observed_at, source_http_status, name, trophies,
			league_tier_id, league_tier_name, eligibility_state, profile_json
		) VALUES ($1, $2, '#9PL', 'profile-v1', 'profile-schema-v1', 'parser-v1',
			clock_timestamp(), 200, 'Boundary', 4000, 1, 'Legend', 'eligible', '{}'::jsonb)
	`, "collector writing canonical profiles", playerID, observationID)
	expectInsufficientPrivilege(t, ctx, connection, `
		UPDATE players SET current_profile_version_id = 1 WHERE id = $1
	`, "collector mutating Python-owned current profile", playerID)
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_support_transfer(
			'00000000-0000-0000-0000-0000000000a2'::uuid, '#9PL',
			'00000000-0000-0000-0000-0000000000a3'::uuid,
			'00000000-0000-0000-0000-0000000000a4'::uuid, 'operator:x', 'reason text')
	`, "collector executing support transfer")
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_request_python_replay_v2(
			$1, 'operator:x', 'reason text', 'parser-v1', 'proc-v1', 'rules-v1', 'analytics-v1')
	`, "collector executing replay request", observationID)
	if _, err := connection.Exec(ctx, `
		SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#9PL', 300, true)
	`); err != nil {
		t.Fatalf("collector execute shared interactive enqueue: %v", err)
	}
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset collector role: %v", err)
	}
	if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
		t.Fatalf("commit collector probe transaction: %v", err)
	}

	// Worker: Python queue claim and completion, domain parse/profile writes,
	// identity sequences; read-only collector evidence; no account, credential,
	// support, replay, or operator-reset use.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin worker probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+workerRole); err != nil {
		t.Fatalf("set worker role: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs_worker
		SET state = 'leased', lease_owner = 'worker:test', lease_token = 'role-boundary-token',
		    lease_expires_at = clock_timestamp() + interval '5 minutes'
		WHERE id = $1
	`, pythonJobID); err != nil {
		t.Fatalf("worker claim python job: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE python_processing_jobs_worker
		SET state = 'complete', completed_at = clock_timestamp(),
		    lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
		WHERE id = $1
	`, pythonJobID); err != nil {
		t.Fatalf("worker complete python job: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO parsed_source_payloads (endpoint, response_hash, parser_version, schema_version, parse_outcome, parsed_json)
		VALUES ('profile', repeat('e', 64), 'parser-v1', 'schema-v1', 'valid', '{}'::jsonb)
	`); err != nil {
		t.Fatalf("worker insert parsed payload: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO player_profile_versions (
			player_id, observation_id, normalized_tag, endpoint_version, schema_version,
			parser_version, observed_at, source_http_status, name, trophies,
			league_tier_id, league_tier_name, eligibility_state, profile_json
		) VALUES ($1, $2, '#9PL', 'profile-v1', 'profile-schema-v1', 'parser-v1',
			clock_timestamp(), 200, 'Boundary', 4000, 1, 'Legend', 'eligible', '{}'::jsonb)
	`, playerID, observationID); err != nil {
		t.Fatalf("worker insert profile version: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE players
		SET active = true,
		    eligibility_state = 'eligible',
		    current_profile_version_id = (
		        SELECT max(id) FROM player_profile_versions WHERE player_id = $1
		    ),
		    current_observed_at = clock_timestamp(),
		    updated_at = clock_timestamp()
		WHERE id = $1
	`, playerID); err != nil {
		t.Fatalf("worker publish current profile: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO api_player_daily_logs (
			player_id, ranked_day_start, version, state, coverage
		) VALUES ($1, '2026-08-01 05:00:00+00', 2, 'Complete', 'complete')
	`, playerID); err != nil {
		t.Fatalf("worker publish daily-log read model: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT nextval('python_processing_jobs_id_seq')`); err != nil {
		t.Fatalf("worker identity sequence usage: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `SELECT count(*) FROM clash_lens_accounts`, "worker reading accounts")
	for _, table := range []string{"account_saved_players", "account_groups", "account_group_players"} {
		expectInsufficientPrivilege(t, ctx, connection, `DELETE FROM `+table+` WHERE false`, "worker deleting from "+table)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		UPDATE shared_api_credentials SET state = 'retired' WHERE credential_fingerprint = repeat('c', 64)
	`, "worker mutating shared credentials")
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_support_transfer(
			'00000000-0000-0000-0000-0000000000a2'::uuid, '#9PL',
			'00000000-0000-0000-0000-0000000000a3'::uuid,
			'00000000-0000-0000-0000-0000000000a4'::uuid, 'operator:x', 'reason text')
	`, "worker executing support transfer")
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_request_python_replay_v2(
			$1, 'operator:x', 'reason text', 'parser-v1', 'proc-v1', 'rules-v1', 'analytics-v1')
	`, "worker executing replay request", observationID)
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT clashlens_operator_reset_python_job($1, 'operator:x', 'reason text')
	`, "worker executing operator reset", pythonJobID)
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role: %v", err)
	}
	if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
		t.Fatalf("commit worker probe transaction: %v", err)
	}

	// API: derived/public reads, permit gate and interactive enqueue, account
	// rows and identity sequences; no canonical, evidence, lease, support,
	// replay, or cleanup use.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin api probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+apiRole); err != nil {
		t.Fatalf("set api role: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT state, ranked_day_end, attack_count, defense_loss FROM api_player_daily_logs WHERE id = $1`, dailyLogID); err != nil {
		t.Fatalf("api read derived row: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		SELECT granted FROM clashlens_acquire_shared_api_permit(repeat('c', 64), 'python')
	`).Scan(&permitGranted); err != nil {
		t.Fatalf("api acquire shared permit: %v", err)
	}
	if !permitGranted {
		t.Fatal("api permit acquisition was not granted")
	}
	if _, err := connection.Exec(ctx, `SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#9PL', 300)`); err != nil {
		t.Fatalf("api interactive enqueue: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO clash_lens_accounts (public_id, username, normalized_username, display_name)
		VALUES ('00000000-0000-0000-0000-0000000000a5'::uuid, 'boundary-api', 'boundary_api', 'Boundary Api')
	`); err != nil {
		t.Fatalf("api insert account: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT nextval('clash_lens_accounts_id_seq')`); err != nil {
		t.Fatalf("api identity sequence usage: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#API', false)
		ON CONFLICT (normalized_tag) DO UPDATE
		    SET normalized_tag = EXCLUDED.normalized_tag
	`); err != nil {
		t.Fatalf("api ensure submitted player: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs (
			observation_id, work_type, deduplication_key, input_json,
			priority, parser_version, max_attempts
		) VALUES (
			NULL, 'build_export', 'role-boundary-export',
			'{"export_request_id": 1}'::jsonb, 100, 'export-scaffold-v1', 3
		)
	`); err != nil {
		t.Fatalf("api enqueue export job: %v", err)
	}
	for _, table := range []string{
		"account_saved_players",
		"account_groups",
		"account_group_players",
	} {
		if _, err := connection.Exec(ctx, `DELETE FROM `+table+` WHERE false`); err != nil {
			t.Fatalf("api delete from %s: %v", table, err)
		}
	}
	expectInsufficientPrivilege(t, ctx, connection, `SELECT count(*) FROM ranked_day_versions`, "api reading ranked-day rows")
	expectInsufficientPrivilege(t, ctx, connection, `UPDATE ranked_day_versions SET state = 'Malformed' WHERE id = 1`, "api mutating ranked-day rows")
	expectInsufficientPrivilege(t, ctx, connection, `UPDATE player_profile_versions SET trophies = 0 WHERE id = 1`, "api mutating profile rows")
	expectInsufficientPrivilege(t, ctx, connection, `UPDATE players SET current_profile_version_id = NULL WHERE id = $1`, "api mutating current profile", playerID)
	expectInsufficientPrivilege(t, ctx, connection, `UPDATE collector_observations SET http_status = 200 WHERE id = $1`, "api mutating collector observations", observationID)
	expectInsufficientPrivilege(t, ctx, connection, `UPDATE python_processing_jobs SET lease_owner = 'x' WHERE id = $1`, "api mutating worker lease fields", pythonJobID)
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_support_transfer(
			'00000000-0000-0000-0000-0000000000a2'::uuid, '#9PL',
			'00000000-0000-0000-0000-0000000000a3'::uuid,
			'00000000-0000-0000-0000-0000000000a4'::uuid, 'operator:x', 'reason text')
	`, "api executing support transfer")
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_request_python_replay_v2(
			$1, 'operator:x', 'reason text', 'parser-v1', 'proc-v1', 'rules-v1', 'analytics-v1')
	`, "api executing replay request", observationID)
	expectInsufficientPrivilege(t, ctx, connection, `SELECT clashlens_cleanup_shared_api_permits(100)`, "api executing permit cleanup")
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset api role: %v", err)
	}
	if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
		t.Fatalf("commit api probe transaction: %v", err)
	}
}

// TestResetBaselineWorkerPrivilegeContract proves the hardened reset-baseline
// worker contract that migration 0003 installs: the Python worker alone can
// execute the job-lineage helper and the narrow sweep-lock seam; PUBLIC, the
// collector, and the API cannot; the lineage helper stays SECURITY INVOKER
// under the worker's read grants on collector evidence; the lock seam is
// SECURITY DEFINER with a fixed search_path and actually holds the row lock.
func TestResetBaselineWorkerPrivilegeContract(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)

	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))

	const (
		collectorRole = "clashlens_collector"
		workerRole    = "clashlens_python_worker"
		apiRole       = "clashlens_python_api"
	)

	// Seed a two-level job lineage and its baseline sweep as the owner so the
	// role probes run against real rows.
	var playerID, parentJobID, parentAttemptID, childJobID, baselineID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag) VALUES ('#RBL') RETURNING id`).Scan(&playerID); err != nil {
		t.Fatalf("seed reset-baseline player: %v", err)
	}
	var sweepID int64
	if err := connection.QueryRow(ctx, `INSERT INTO collector_reset_sweeps (boundary_at) VALUES ('2026-08-05 05:00:00+00') RETURNING id`).Scan(&sweepID); err != nil {
		t.Fatalf("seed reset sweep: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_reset_baseline_sweeps (
			reset_sweep_id, player_id, boundary_at, evidence_kind, state
		) VALUES ($1, $2, '2026-08-05 05:00:00+00', 'paired_v2', 'pending')
		RETURNING id
	`, sweepID, playerID).Scan(&baselineID); err != nil {
		t.Fatalf("seed baseline sweep: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, reset_baseline_sweep_id, sweep_id
		) VALUES ('reset_baseline', $1, '#RBL', 'normal', 400,
			'2026-08-05 05:00:00+00', 'reset-baseline-role', 'complete', $2, $3)
		RETURNING id
	`, playerID, baselineID, sweepID).Scan(&parentJobID); err != nil {
		t.Fatalf("seed root reset-baseline job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', '2026-08-05 05:00:00+00', '2026-08-05 05:01:00+00')
		RETURNING id
	`, parentJobID).Scan(&parentAttemptID); err != nil {
		t.Fatalf("seed root reset-baseline attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, parent_attempt_id
		) VALUES ('live_refresh', $1, '#RBL', 'normal', 300,
			'2026-08-05 05:01:00+00', 'reset-baseline-child', 'complete', $2)
		RETURNING id
	`, playerID, parentAttemptID).Scan(&childJobID); err != nil {
		t.Fatalf("seed child lineage job: %v", err)
	}

	// The lock seam is SECURITY DEFINER owned by the migration owner with a
	// fixed search_path; the lineage helper stays SECURITY INVOKER.
	for _, function := range []struct {
		name      string
		definer   bool
		fixedPath bool
	}{
		{name: "clashlens_lock_reset_baseline_v2", definer: true, fixedPath: true},
		{name: "clashlens_reset_job_lineage_v2", definer: false, fixedPath: false},
	} {
		var prosecdef, fixedSearchPath, ownedByMigrator bool
		if err := connection.QueryRow(ctx, `
			SELECT p.prosecdef,
			       EXISTS (
			           SELECT 1
			           FROM unnest(COALESCE(p.proconfig, ARRAY[]::text[])) AS entry
			           WHERE entry LIKE 'search_path=pg_catalog,%'
			       ),
			       p.proowner::regrole::text = current_user::text
			FROM pg_proc AS p
			JOIN pg_namespace AS n ON n.oid = p.pronamespace
			WHERE n.nspname = current_schema() AND p.proname = $1
		`, function.name).Scan(&prosecdef, &fixedSearchPath, &ownedByMigrator); err != nil {
			t.Fatalf("inspect %s metadata: %v", function.name, err)
		}
		if prosecdef != function.definer {
			t.Fatalf("%s prosecdef = %v, want %v", function.name, prosecdef, function.definer)
		}
		if fixedSearchPath != function.fixedPath {
			t.Fatalf("%s fixed search_path = %v, want %v", function.name, fixedSearchPath, function.fixedPath)
		}
		if !ownedByMigrator {
			t.Fatalf("%s is not owned by the migration owner", function.name)
		}
	}

	// Worker: executes the lineage helper and the lock seam, and the lock
	// actually holds the sweep row. The previous image's direct FOR UPDATE
	// remains compatible for this release through UPDATE(id), but the worker
	// still cannot mutate sweep state.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin worker reset-baseline probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+workerRole); err != nil {
		t.Fatalf("set worker role: %v", err)
	}
	var lineageFound bool
	if err := connection.QueryRow(ctx, `SELECT clashlens_reset_job_lineage_v2($1, $2)`, childJobID, parentJobID).Scan(&lineageFound); err != nil {
		t.Fatalf("worker execute lineage helper: %v", err)
	}
	if !lineageFound {
		t.Fatal("worker lineage lookup did not find the root job")
	}
	if err := connection.QueryRow(ctx, `SELECT clashlens_reset_job_lineage_v2($1, 999999999)`, childJobID).Scan(&lineageFound); err != nil {
		t.Fatalf("worker execute lineage helper for missing root: %v", err)
	}
	if lineageFound {
		t.Fatal("worker lineage lookup found an unknown root job")
	}
	if err := connection.QueryRow(ctx, `SELECT clashlens_reset_job_lineage_v2(999999999, $1)`, parentJobID).Scan(&lineageFound); err != nil {
		t.Fatalf("worker execute lineage helper for missing observed job: %v", err)
	}
	if lineageFound {
		t.Fatal("worker lineage lookup found an unknown observed job")
	}
	var locked bool
	if err := connection.QueryRow(ctx, `SELECT clashlens_lock_reset_baseline_v2($1)`, baselineID).Scan(&locked); err != nil {
		t.Fatalf("worker lock existing baseline sweep: %v", err)
	}
	if !locked {
		t.Fatal("worker lock of an existing baseline sweep returned false")
	}
	if err := connection.QueryRow(ctx, `SELECT clashlens_lock_reset_baseline_v2(999999999)`).Scan(&locked); err != nil {
		t.Fatalf("worker lock missing baseline sweep: %v", err)
	}
	if locked {
		t.Fatal("worker lock of a missing baseline sweep returned true")
	}
	var directlyLockedID int64
	if err := connection.QueryRow(ctx, `
		SELECT id FROM collector_reset_baseline_sweeps WHERE id = $1 FOR UPDATE
	`, baselineID).Scan(&directlyLockedID); err != nil || directlyLockedID != baselineID {
		t.Fatalf("previous worker image cannot lock the sweep row during its compatibility window: id=%d err=%v", directlyLockedID, err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		UPDATE collector_reset_baseline_sweeps SET state = 'complete' WHERE id = $1
	`, "worker updating the sweep table directly", baselineID)

	// The worker transaction still holds the sweep lock: a second session
	// must block and time out instead of updating the locked row.
	blockedConnection, err := pgx.ConnectConfig(ctx, connection.Config())
	if err != nil {
		t.Fatalf("open lock-blocking connection: %v", err)
	}
	t.Cleanup(func() { _ = blockedConnection.Close(context.Background()) })
	if _, err := blockedConnection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin lock-blocking transaction: %v", err)
	}
	if _, err := blockedConnection.Exec(ctx, `SET LOCAL lock_timeout = '500ms'`); err != nil {
		t.Fatalf("set lock timeout: %v", err)
	}
	_, err = blockedConnection.Exec(ctx, `
		UPDATE collector_reset_baseline_sweeps SET state = 'complete' WHERE id = $1
	`, baselineID)
	if err == nil {
		_, _ = blockedConnection.Exec(ctx, `ROLLBACK`)
		t.Fatal("database accepted an update of the worker-locked sweep")
	}
	var pgError *pgconn.PgError
	if !errors.As(err, &pgError) || pgError.Code != "55P03" {
		t.Fatalf("update of worker-locked sweep failed with %v, want SQLSTATE 55P03", err)
	}
	if _, err := blockedConnection.Exec(ctx, `ROLLBACK`); err != nil {
		t.Fatalf("roll back lock-blocking transaction: %v", err)
	}

	// Release the worker's lock and prove the same update now succeeds.
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role: %v", err)
	}
	if _, err := connection.Exec(ctx, `ROLLBACK`); err != nil {
		t.Fatalf("roll back worker probe transaction: %v", err)
	}
	if _, err := blockedConnection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin post-release transaction: %v", err)
	}
	if _, err := blockedConnection.Exec(ctx, `
		UPDATE collector_reset_baseline_sweeps SET state = 'complete' WHERE id = $1
	`, baselineID); err != nil {
		t.Fatalf("update of released sweep failed: %v", err)
	}
	if _, err := blockedConnection.Exec(ctx, `ROLLBACK`); err != nil {
		t.Fatalf("roll back post-release transaction: %v", err)
	}

	// The collector, the API, and a neutral PUBLIC-only role cannot execute
	// either seam. The neutral role gets schema USAGE so the denial comes
	// specifically from function EXECUTE, not from schema reach.
	if _, err := connection.Exec(ctx, `CREATE ROLE clashlens_public_probe NOLOGIN`); err != nil {
		t.Fatalf("create PUBLIC probe role: %v", err)
	}
	t.Cleanup(func() { _, _ = connection.Exec(context.Background(), `DROP ROLE IF EXISTS clashlens_public_probe`) })
	if _, err := connection.Exec(ctx, `GRANT USAGE ON SCHEMA `+schemaName(t, ctx, connection)+` TO clashlens_public_probe`); err != nil {
		t.Fatalf("grant schema usage to PUBLIC probe role: %v", err)
	}
	for _, role := range []string{collectorRole, apiRole, "clashlens_public_probe"} {
		if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
			t.Fatalf("begin %s reset-baseline probe transaction: %v", role, err)
		}
		if _, err := connection.Exec(ctx, `SET ROLE `+role); err != nil {
			t.Fatalf("set %s role: %v", role, err)
		}
		expectInsufficientPrivilege(t, ctx, connection, `
			SELECT clashlens_lock_reset_baseline_v2($1)
		`, role+" executing the sweep-lock seam", baselineID)
		expectInsufficientPrivilege(t, ctx, connection, `
			SELECT clashlens_reset_job_lineage_v2($1, $2)
		`, role+" executing the lineage helper", childJobID, parentJobID)
		if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
			t.Fatalf("reset %s role: %v", role, err)
		}
		if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
			t.Fatalf("commit %s reset-baseline probe transaction: %v", role, err)
		}
	}
}

func schemaName(t *testing.T, ctx context.Context, connection *pgx.Conn) string {
	t.Helper()
	var name string
	if err := connection.QueryRow(ctx, `SELECT current_schema()`).Scan(&name); err != nil {
		t.Fatalf("read current schema: %v", err)
	}
	return name
}

// expectInsufficientPrivilege asserts that statement fails with SQLSTATE
// 42501. A denied statement aborts the current transaction, so each probe
// runs inside a savepoint that is rolled back after the failure.
func expectInsufficientPrivilege(t *testing.T, ctx context.Context, connection *pgx.Conn, statement, description string, arguments ...any) {
	t.Helper()
	if _, err := connection.Exec(ctx, `SAVEPOINT privilege_probe`); err != nil {
		t.Fatalf("open privilege probe savepoint: %v", err)
	}
	_, err := connection.Exec(ctx, statement, arguments...)
	if err == nil {
		_, _ = connection.Exec(ctx, `RELEASE SAVEPOINT privilege_probe`)
		t.Fatalf("database accepted %s", description)
	}
	var pgError *pgconn.PgError
	if !errors.As(err, &pgError) || pgError.Code != "42501" {
		t.Fatalf("database rejected %s with %v, want SQLSTATE 42501", description, err)
	}
	if _, err := connection.Exec(ctx, `ROLLBACK TO SAVEPOINT privilege_probe`); err != nil {
		t.Fatalf("roll back privilege probe savepoint: %v", err)
	}
	if _, err := connection.Exec(ctx, `RELEASE SAVEPOINT privilege_probe`); err != nil {
		t.Fatalf("release privilege probe savepoint: %v", err)
	}
}

var _ = time.Now
