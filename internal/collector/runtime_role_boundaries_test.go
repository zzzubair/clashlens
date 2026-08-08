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

// TestProductionMigrationTwoEnforcesRuntimeRoleBoundaries proves the explicit
// least-privilege grant layer that migration 0002 installs for the three
// runtime roles: the Go collector, the Python worker, and the Python API.
// Each role is NOLOGIN with no elevated attributes and no password; positive
// probes run the role's sanctioned statements and negative probes assert
// SQLSTATE 42501 across the documented boundary matrix.
func TestProductionMigrationTwoEnforcesRuntimeRoleBoundaries(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)

	// Reapplication must be stable: roles stay unique with fixed attributes
	// and every grant/revoke can be repeated.
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))

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

	// Collector: evidence and job rows, contract reads, permit gate, identity
	// sequences; no account, canonical-profile, support, replay, or enqueue use.
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
	expectInsufficientPrivilege(t, ctx, connection, `
		SELECT * FROM clashlens_enqueue_interactive('live_refresh', '#9PL', 300)
	`, "collector executing interactive enqueue")
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
	if _, err := connection.Exec(ctx, `SELECT state FROM api_player_daily_logs WHERE id = $1`, dailyLogID); err != nil {
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
