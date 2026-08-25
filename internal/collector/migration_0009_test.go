package collector

import (
	"context"
	"fmt"
	"path/filepath"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

func TestMigration0009ExecutesAndRetainsLegacyObservations(t *testing.T) {
	ctx := context.Background()
	connection, err := pgx.Connect(ctx, testsupport.StartPostgres(t))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close(ctx)
	for version := 1; version <= 8; version++ {
		applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", formatMigration(version)))
	}
	var playerID, jobID, attemptID int64
	if err := connection.QueryRow(ctx, `INSERT INTO players (normalized_tag, active, next_due_at) VALUES ('#2PM', true, clock_timestamp()) RETURNING id`).Scan(&playerID); err != nil {
		t.Fatal(err)
	}
	if err := connection.QueryRow(ctx, `INSERT INTO collector_jobs (work_type, player_id, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status) VALUES ('regular_poll', $1, '#2PM', 'normal', 100, clock_timestamp(), 'migration-0009-legacy', 'complete') RETURNING id`, playerID).Scan(&jobID); err != nil {
		t.Fatal(err)
	}
	if err := connection.QueryRow(ctx, `INSERT INTO collector_attempts (job_id, status, started_at, completed_at) VALUES ($1, 'complete', clock_timestamp(), clock_timestamp()) RETURNING id`, jobID).Scan(&attemptID); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO collector_observations (occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag, endpoint, request_started_at, response_completed_at, http_status, response_hash, archive_reference, collector_version, key_label, evidence_headers) VALUES ('migration-0009-legacy', $1, $2, $3, '#2PM', 'profile', clock_timestamp(), clock_timestamp(), 200, repeat('a', 64), 's3://legacy/profile', 'collector-v2', 'key', '{}'::jsonb)`, jobID, attemptID, playerID); err != nil {
		t.Fatal(err)
	}
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0009_raw_evidence.sql"))
	var contract int
	if err := connection.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&contract); err != nil {
		t.Fatal(err)
	}
	if contract != 3 {
		t.Fatalf("contract version = %d, want 3", contract)
	}
	var tables int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM pg_class WHERE relname IN ('archive_instances', 'archive_catalogue')`).Scan(&tables); err != nil {
		t.Fatal(err)
	}
	if tables != 2 {
		t.Fatalf("raw-evidence tables = %d, want 2", tables)
	}
	var legacyMarker *string
	if err := connection.QueryRow(ctx, `SELECT archive_catalogue_hash FROM collector_observations WHERE occurrence_key = 'migration-0009-legacy'`).Scan(&legacyMarker); err != nil {
		t.Fatal(err)
	}
	if legacyMarker != nil {
		t.Fatalf("legacy observation marker = %q, want NULL", *legacyMarker)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO archive_instances (instance_id, endpoint, region, bucket, marker_key, marker_hash, marker_payload_version) VALUES ('instance', 'archive.example:443', 'region', 'bucket', 'marker', repeat('b', 64), 'v1')`); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO archive_catalogue (response_hash, archive_reference, byte_size, archive_instance_id) VALUES (repeat('c', 64), 's3://bucket/object', 4, 'instance')`); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO collector_observations (occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag, endpoint, request_started_at, response_completed_at, http_status, response_hash, archive_reference, archive_catalogue_hash, collector_version, key_label, evidence_headers) VALUES ('migration-0009-catalogued', $1, $2, $3, '#2PM', 'profile', clock_timestamp(), clock_timestamp(), 200, repeat('c', 64), 's3://bucket/object', repeat('c', 64), 'collector-v3', 'key', '{}'::jsonb)`, jobID, attemptID, playerID); err != nil {
		t.Fatal(err)
	}
	if _, err := connection.Exec(ctx, `INSERT INTO collector_observations (occurrence_key, collection_job_id, attempt_id, player_id, normalized_tag, endpoint, request_started_at, response_completed_at, http_status, response_hash, archive_reference, collector_version, key_label, evidence_headers) VALUES ('migration-0009-uncatalogued', $1, $2, $3, '#2PM', 'profile', clock_timestamp(), clock_timestamp(), 200, repeat('d', 64), 's3://bucket/uncatalogued', 'collector-v3', 'key', '{}'::jsonb)`, jobID, attemptID, playerID); err == nil {
		t.Fatal("new uncatalogued observation was accepted")
	}
}

func formatMigration(version int) string {
	return fmt.Sprintf("%04d_%s.sql", version, migrationNames[version])
}

var migrationNames = map[int]string{
	1: "collector", 2: "python_layer", 3: "regular_poll_dedup", 4: "source_parser_v2",
	5: "army_decoding", 6: "provider_identities", 7: "player_discovery", 8: "public_army_analytics",
}
