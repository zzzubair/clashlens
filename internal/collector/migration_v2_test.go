package collector

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

func TestProductionMigrationTwoUpgradesPopulatedVersionOneRepeatably(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })

	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))

	var playerID, jobID, attemptID, observationID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', false, NULL)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert version-one player: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, status
		) VALUES ('initial_collection', $1, '#2PP', 'interactive', 300, $2, 'migration-v2', 'complete')
		RETURNING id
	`, playerID, time.Date(2026, time.August, 3, 19, 35, 0, 0, time.UTC)).Scan(&jobID); err != nil {
		t.Fatalf("insert version-one collector job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', $2, $2)
		RETURNING id
	`, jobID, time.Date(2026, time.August, 3, 19, 35, 0, 0, time.UTC)).Scan(&attemptID); err != nil {
		t.Fatalf("insert version-one collector attempt: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, player_id,
			normalized_tag, endpoint, request_started_at, response_completed_at,
			http_status, response_hash, archive_reference, collector_version,
			key_label, evidence_headers
		) VALUES (
			'migration-v2:profile', $1, $2, $3, '#2PP', 'profile', $4, $4,
			200, repeat('a', 64), 'archive/profile.json', 'collector-v1',
			'normal-a', '{}'::jsonb
		)
		RETURNING id
	`, jobID, attemptID, playerID, time.Date(2026, time.August, 3, 19, 35, 1, 0, time.UTC)).Scan(&observationID); err != nil {
		t.Fatalf("insert version-one observation: %v", err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO python_processing_jobs (observation_id) VALUES ($1)`, observationID); err != nil {
		t.Fatalf("insert version-one Python job: %v", err)
	}

	migrationPath := filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql")
	applySQLFile(t, ctx, connection, migrationPath)
	applySQLFile(t, ctx, connection, migrationPath)

	var contractVersion, migrations int
	if err := connection.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&contractVersion); err != nil {
		t.Fatalf("read upgraded contract version: %v", err)
	}
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM clash_lens_schema_migrations WHERE version IN (1, 2)`).Scan(&migrations); err != nil {
		t.Fatalf("count applied schema migrations: %v", err)
	}
	if contractVersion != 2 || migrations != 2 {
		t.Fatalf("contract version = %d and migration count = %d, want 2 and 2", contractVersion, migrations)
	}

	var workType, deduplicationKey, status, endpointVersion, schemaVersion string
	if err := connection.QueryRow(ctx, `
		SELECT work_type, deduplication_key, status
		FROM python_processing_jobs
		WHERE observation_id = $1
	`, observationID).Scan(&workType, &deduplicationKey, &status); err != nil {
		t.Fatalf("read upgraded Python job: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		SELECT endpoint_version, schema_version
		FROM collector_observations
		WHERE id = $1
	`, observationID).Scan(&endpointVersion, &schemaVersion); err != nil {
		t.Fatalf("read upgraded observation versions: %v", err)
	}
	if workType != "process_observation" || deduplicationKey != "process-observation:"+int64String(observationID) || status != "pending" {
		t.Fatalf("upgraded job = %q, %q, %q", workType, deduplicationKey, status)
	}
	if endpointVersion != "profile-v1" || schemaVersion != "profile-schema-v1" {
		t.Fatalf("upgraded observation versions = %q and %q", endpointVersion, schemaVersion)
	}

	var attemptsTable, profileVersionsTable, profileEffectsTable *string
	if err := connection.QueryRow(ctx, `
		SELECT to_regclass('python_processing_attempts')::text,
		       to_regclass('player_profile_versions')::text,
		       to_regclass('player_profile_effects')::text
	`).Scan(&attemptsTable, &profileVersionsTable, &profileEffectsTable); err != nil {
		t.Fatalf("read version-two table registry: %v", err)
	}
	if attemptsTable == nil || profileVersionsTable == nil || profileEffectsTable == nil {
		t.Fatalf("version-two tables are missing: attempts=%v profiles=%v effects=%v", attemptsTable, profileVersionsTable, profileEffectsTable)
	}

	var bridgeJobID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO python_processing_jobs (observation_id)
		VALUES ($1)
		ON CONFLICT (observation_id) DO UPDATE SET observation_id = EXCLUDED.observation_id
		RETURNING id
	`, observationID).Scan(&bridgeJobID); err != nil {
		t.Fatalf("version-one-shaped bridge insert failed after migration: %v", err)
	}
	if bridgeJobID < 1 {
		t.Fatalf("bridge job id = %d, want a positive id", bridgeJobID)
	}

	opened, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("open version-two bridge store: %v", err)
	}
	defer opened.close()
	now := time.Now().UTC()
	if _, err := connection.Exec(ctx, `
		UPDATE players
		SET active = true, eligibility_state = 'eligible', next_due_at = $1
		WHERE normalized_tag = '#2PP'
	`, now.Add(-time.Minute)); err != nil {
		t.Fatalf("activate migrated player: %v", err)
	}
	created, err := opened.scheduleDueRegular(ctx, now, 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule activated migrated player: %v", err)
	}
	if created != 1 {
		t.Fatalf("scheduled regular jobs = %d, want 1", created)
	}
	var regularJobs int
	var nextDue time.Time
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FILTER (
			WHERE collector_jobs.work_type = 'regular_poll'
			  AND collector_jobs.status = 'pending'
		), max(players.next_due_at)
		FROM collector_jobs
		CROSS JOIN players
		WHERE players.normalized_tag = '#2PP'
	`).Scan(&regularJobs, &nextDue); err != nil {
		t.Fatalf("read scheduled activation handoff: %v", err)
	}
	if regularJobs != 1 || !nextDue.After(now) {
		t.Fatalf("activation handoff = jobs %d, next due %s", regularJobs, nextDue)
	}
}

func applySQLFile(t *testing.T, ctx context.Context, connection *pgx.Conn, path string) {
	t.Helper()
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read SQL file %s: %v", path, err)
	}
	if _, err := connection.Exec(ctx, string(content)); err != nil {
		t.Fatalf("apply SQL file %s: %v", path, err)
	}
}

func int64String(value int64) string {
	return fmt.Sprintf("%d", value)
}
