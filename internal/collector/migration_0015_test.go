package collector

import (
	"context"
	"errors"
	"fmt"
	"path/filepath"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

func TestMigration0015OwnsPythonJobSourceContractSecurity(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)

	for _, testCase := range []struct {
		name  string
		setup func(*testing.T, context.Context, *pgx.Conn) int64
	}{
		{
			name: "fresh",
			setup: func(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
				applySourceContractMigrations(t, ctx, connection, 1, 8)
				observationID := seedSourceContractObservation(t, ctx, connection)
				applySourceContractMigrations(t, ctx, connection, 9, 15)
				return observationID
			},
		},
		{
			name: "upgrade_from_0014",
			setup: func(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
				applySourceContractMigrations(t, ctx, connection, 1, 8)
				observationID := seedSourceContractObservation(t, ctx, connection)
				applySourceContractMigrations(t, ctx, connection, 9, 14)
				simulatePre0015SourceContractFunction(t, ctx, connection)
				assertSourceContractFunctionInsecure(t, ctx, connection)
				restrictWorkerBaseTable(t, ctx, connection)
				assertWorkerInsertDenied(t, ctx, connection, observationID)
				applySourceContractMigrations(t, ctx, connection, 15, 15)
				return observationID
			},
		},
		{
			name: "0015_before_late_0009",
			setup: func(t *testing.T, ctx context.Context, connection *pgx.Conn) int64 {
				applySourceContractMigrations(t, ctx, connection, 1, 8)
				observationID := seedSourceContractObservation(t, ctx, connection)
				applySourceContractMigrations(t, ctx, connection, 15, 15)
				var functionExists, migrationRecorded bool
				if err := connection.QueryRow(ctx, `
					SELECT to_regprocedure(
					           format('%I.clashlens_set_python_job_source_contract()', current_schema())
					       ) IS NOT NULL,
					       EXISTS (
					           SELECT 1 FROM clash_lens_schema_migrations WHERE version = 15
					       )
				`).Scan(&functionExists, &migrationRecorded); err != nil {
					t.Fatalf("inspect migration 0015 before migration 0009: %v", err)
				}
				if functionExists || !migrationRecorded {
					t.Fatalf(
						"pre-0009 migration 0015 state = function %v, recorded %v",
						functionExists,
						migrationRecorded,
					)
				}
				applySourceContractMigrations(t, ctx, connection, 9, 9)
				return observationID
			},
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			connection, err := pgx.Connect(ctx, databaseURL)
			if err != nil {
				t.Fatalf("connect to test PostgreSQL: %v", err)
			}
			defer connection.Close(context.Background())

			schema := "source_contract_" + testCase.name
			quotedSchema := pgx.Identifier{schema}.Sanitize()
			if _, err := connection.Exec(ctx, "CREATE SCHEMA "+quotedSchema); err != nil {
				t.Fatalf("create migration schema: %v", err)
			}
			if _, err := connection.Exec(ctx, "SET search_path TO "+quotedSchema); err != nil {
				t.Fatalf("select migration schema: %v", err)
			}

			observationID := testCase.setup(t, ctx, connection)
			assertSourceContractFunctionSecure(t, ctx, connection, schema)
			assertLeastPrivilegeSourceContractTrigger(
				t,
				ctx,
				connection,
				observationID,
				testCase.name,
			)
		})
	}
}

func applySourceContractMigrations(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
	first int,
	last int,
) {
	t.Helper()
	for version := first; version <= last; version++ {
		matches, err := filepath.Glob(filepath.Join(
			"..",
			"..",
			"deploy",
			"migrations",
			fmt.Sprintf("%04d_*.sql", version),
		))
		if err != nil || len(matches) != 1 {
			t.Fatalf("resolve migration %04d: matches=%v err=%v", version, matches, err)
		}
		applySQLFile(t, ctx, connection, matches[0])
	}
}

func seedSourceContractObservation(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
) int64 {
	t.Helper()
	var playerID, jobID, attemptID, observationID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PM', true, clock_timestamp())
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert source-contract player: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES (
			'regular_poll', $1, '#2PM', 'normal', 100,
			clock_timestamp(), 'migration-0015-source-contract', 'complete'
		)
		RETURNING id
	`, playerID).Scan(&jobID); err != nil {
		t.Fatalf("insert source-contract collector job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', clock_timestamp(), clock_timestamp())
		RETURNING id
	`, jobID).Scan(&attemptID); err != nil {
		t.Fatalf("insert source-contract collector attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id,
			normalized_tag, endpoint, request_started_at, response_completed_at,
			http_status, response_hash, archive_reference, collector_version,
			key_label, evidence_headers
		) VALUES (
			'migration-0015-source-contract', $1, $2, $3,
			'#2PM', 'profile', clock_timestamp(), clock_timestamp(),
			200, repeat('a', 64), 's3://legacy/profile', 'collector-v2',
			'key', '{}'::jsonb
		)
		RETURNING id
	`, jobID, attemptID, playerID).Scan(&observationID); err != nil {
		t.Fatalf("insert source-contract observation: %v", err)
	}
	return observationID
}

func assertSourceContractFunctionInsecure(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
) {
	t.Helper()
	var securityDefiner, publicExecute bool
	if err := connection.QueryRow(ctx, `
		SELECT p.prosecdef,
		       has_function_privilege('public', p.oid, 'EXECUTE')
		FROM pg_proc AS p
		JOIN pg_namespace AS n ON n.oid = p.pronamespace
		WHERE n.nspname = current_schema()
		  AND p.proname = 'clashlens_set_python_job_source_contract'
		  AND p.pronargs = 0
	`).Scan(&securityDefiner, &publicExecute); err != nil {
		t.Fatalf("inspect pre-0015 source-contract function: %v", err)
	}
	if securityDefiner || !publicExecute {
		t.Fatalf(
			"pre-0015 source-contract function = security definer %v, PUBLIC execute %v",
			securityDefiner,
			publicExecute,
		)
	}
}

func assertSourceContractFunctionSecure(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
	schema string,
) {
	t.Helper()
	var securityDefiner, publicExecute, apiExecute, workerExecute, collectorExecute bool
	var settings string
	if err := connection.QueryRow(ctx, `
		SELECT p.prosecdef,
		       COALESCE(array_to_string(p.proconfig, ','), ''),
		       has_function_privilege('public', p.oid, 'EXECUTE'),
		       has_function_privilege('clashlens_python_api', p.oid, 'EXECUTE'),
		       has_function_privilege('clashlens_python_worker', p.oid, 'EXECUTE'),
		       has_function_privilege('clashlens_collector', p.oid, 'EXECUTE')
		FROM pg_proc AS p
		JOIN pg_namespace AS n ON n.oid = p.pronamespace
		WHERE n.nspname = current_schema()
		  AND p.proname = 'clashlens_set_python_job_source_contract'
		  AND p.pronargs = 0
	`).Scan(
		&securityDefiner,
		&settings,
		&publicExecute,
		&apiExecute,
		&workerExecute,
		&collectorExecute,
	); err != nil {
		t.Fatalf("inspect hardened source-contract function: %v", err)
	}
	if !securityDefiner || settings != "search_path=pg_catalog, "+schema {
		t.Fatalf(
			"source-contract function security = definer %v, settings %q",
			securityDefiner,
			settings,
		)
	}
	if publicExecute || apiExecute || workerExecute || collectorExecute {
		t.Fatalf(
			"source-contract function execute ACL = PUBLIC %v, API %v, worker %v, collector %v",
			publicExecute,
			apiExecute,
			workerExecute,
			collectorExecute,
		)
	}
}

func simulatePre0015SourceContractFunction(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
) {
	t.Helper()
	if _, err := connection.Exec(ctx, `
		ALTER FUNCTION clashlens_set_python_job_source_contract() SECURITY INVOKER;
		ALTER FUNCTION clashlens_set_python_job_source_contract() RESET search_path;
		GRANT EXECUTE ON FUNCTION clashlens_set_python_job_source_contract() TO PUBLIC;
	`); err != nil {
		t.Fatalf("simulate pre-0015 source-contract function: %v", err)
	}
}

func restrictWorkerBaseTable(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
) {
	t.Helper()
	if _, err := connection.Exec(ctx, `
		REVOKE ALL PRIVILEGES ON TABLE python_processing_jobs
		    FROM clashlens_python_worker;
		GRANT SELECT (id, lease_generation) ON TABLE python_processing_jobs
		    TO clashlens_python_worker;
	`); err != nil {
		t.Fatalf("restrict worker base-table privileges: %v", err)
	}
}

func assertWorkerInsertDenied(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
	observationID int64,
) {
	t.Helper()
	if _, err := connection.Exec(ctx, `SET ROLE clashlens_python_worker`); err != nil {
		t.Fatalf("set worker role before migration 0015: %v", err)
	}
	_, insertErr := connection.Exec(ctx, `
		INSERT INTO python_processing_jobs_worker (observation_id, deduplication_key)
		VALUES ($1, 'migration-0015-before-hardening')
	`, observationID)
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role before migration 0015: %v", err)
	}
	assertInsufficientPrivilege(t, insertErr, "pre-0015 worker trigger insert")
}

func assertLeastPrivilegeSourceContractTrigger(
	t *testing.T,
	ctx context.Context,
	connection *pgx.Conn,
	observationID int64,
	deduplicationSuffix string,
) {
	t.Helper()
	restrictWorkerBaseTable(t, ctx, connection)
	var workerUpdate, workerViewInsert bool
	if err := connection.QueryRow(ctx, `
		SELECT has_table_privilege(
		           'clashlens_python_worker',
		           format('%I.python_processing_jobs', current_schema()),
		           'UPDATE'
		       ),
		       has_table_privilege(
		           'clashlens_python_worker',
		           format('%I.python_processing_jobs_worker', current_schema()),
		           'INSERT'
		       )
	`).Scan(&workerUpdate, &workerViewInsert); err != nil {
		t.Fatalf("inspect worker job-table privileges: %v", err)
	}
	if workerUpdate || !workerViewInsert {
		t.Fatalf(
			"worker privileges = base UPDATE %v, worker-view INSERT %v",
			workerUpdate,
			workerViewInsert,
		)
	}

	if _, err := connection.Exec(ctx, `SET ROLE clashlens_python_worker`); err != nil {
		t.Fatalf("set worker role for source-contract trigger: %v", err)
	}
	var jobID int64
	insertErr := connection.QueryRow(ctx, `
		INSERT INTO python_processing_jobs_worker (observation_id, deduplication_key)
		VALUES ($1, $2)
		RETURNING id
	`, observationID, "migration-0015-after-hardening:"+deduplicationSuffix).Scan(&jobID)
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role after source-contract trigger: %v", err)
	}
	if insertErr != nil {
		t.Fatalf("least-privilege worker trigger insert: %v", insertErr)
	}

	var endpoint, endpointVersion, schemaVersion string
	if err := connection.QueryRow(ctx, `
		SELECT endpoint, endpoint_version, schema_version
		FROM python_processing_jobs
		WHERE id = $1
	`, jobID).Scan(&endpoint, &endpointVersion, &schemaVersion); err != nil {
		t.Fatalf("read trigger-populated source contract: %v", err)
	}
	if endpoint != "profile" || endpointVersion != "profile-v1" || schemaVersion != "profile-schema-v1" {
		t.Fatalf(
			"trigger-populated source contract = %q, %q, %q",
			endpoint,
			endpointVersion,
			schemaVersion,
		)
	}

	if _, err := connection.Exec(ctx, `SET ROLE clashlens_python_worker`); err != nil {
		t.Fatalf("set worker role for direct update probe: %v", err)
	}
	_, updateErr := connection.Exec(ctx, `
		UPDATE python_processing_jobs SET endpoint = 'battle_log' WHERE id = $1
	`, jobID)
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role after direct update probe: %v", err)
	}
	assertInsufficientPrivilege(t, updateErr, "direct worker source-contract update")
}

func assertInsufficientPrivilege(t *testing.T, err error, operation string) {
	t.Helper()
	var postgresError *pgconn.PgError
	if !errors.As(err, &postgresError) || postgresError.Code != "42501" {
		t.Fatalf("%s error = %v, want insufficient_privilege", operation, err)
	}
}
