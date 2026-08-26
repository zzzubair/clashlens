package collector

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

func startBoundaryAdmissionDatabase(t *testing.T) string {
	t.Helper()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(context.Background(), databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	defer connection.Close(context.Background())
	paths, err := filepath.Glob(filepath.Join("..", "..", "deploy", "migrations", "*.sql"))
	if err != nil {
		t.Fatalf("find production migrations: %v", err)
	}
	sort.Strings(paths)
	for _, path := range paths {
		migration, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read migration %s: %v", path, err)
		}
		if _, err := connection.Exec(context.Background(), string(migration)); err != nil {
			t.Fatalf("apply migration %s: %v", path, err)
		}
	}
	return databaseURL
}

func TestBoundaryAdmissionExcludesRegularWorkUntilSafeHandoff(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 3)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	defer store.close()

	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	beforeReset := boundary.Add(-3 * time.Minute)
	if _, err := store.pool.Exec(ctx, `
        INSERT INTO players (normalized_tag, active, next_due_at)
        VALUES ('#GATE', true, $1)
    `, boundary.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
        INSERT INTO collector_jobs (
            work_type, player_id, normalized_tag, capacity_pool, priority,
            due_at, coalescing_key, status, created_at, updated_at
        ) VALUES (
            'regular_poll', (SELECT id FROM players WHERE normalized_tag = '#GATE'),
            '#GATE', 'normal', 100, $1, 'gate-seed', 'pending', $2, $2
        )
    `, beforeReset, boundary.Add(-time.Minute)); err != nil {
		t.Fatalf("seed prior regular job: %v", err)
	}

	created, err := store.scheduleDueRegular(ctx, beforeReset, 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule before reset: %v", err)
	}
	if created != 0 {
		t.Fatalf("regular work admitted in reset window: %d", created)
	}

	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs SET status = 'waiting_dependency', updated_at = clock_timestamp()
        WHERE coalescing_key = 'gate-seed'
    `); err != nil {
		t.Fatalf("move prior regular job to dependency wait: %v", err)
	}
	waitingSweepID, waitingCreated, err := store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil {
		t.Fatalf("prepare boundary with waiting regular work: %v", err)
	}
	if waitingCreated || waitingSweepID != 0 {
		t.Fatalf("waiting regular work was treated as drained: sweep=%d created=%v", waitingSweepID, waitingCreated)
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
        WHERE coalescing_key = 'gate-seed'
    `); err != nil {
		t.Fatalf("drain prior regular job: %v", err)
	}
	sweepID, createdSweep, err := store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil {
		t.Fatalf("prepare reset boundary: %v", err)
	}
	if !createdSweep || sweepID == 0 {
		t.Fatalf("reset sweep = %d created=%v, want new sweep", sweepID, createdSweep)
	}
	created, err = store.scheduleDueRegular(ctx, boundary, 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule while reset runs: %v", err)
	}
	if created != 0 {
		t.Fatalf("regular work overlapped reset baseline: %d", created)
	}

	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs
        SET status = 'waiting_dependency', updated_at = clock_timestamp()
        WHERE id = (
            SELECT id FROM collector_jobs
            WHERE sweep_id = $1 AND work_type = 'reset_baseline'
            ORDER BY id LIMIT 1
        )
    `, sweepID); err != nil {
		t.Fatalf("move reset job to dependency wait: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(time.Minute)); err != nil {
		t.Fatalf("prepare boundary with waiting reset work: %v", err)
	}
	allowed, err := store.regularAdmissionAllowed(ctx, boundary.Add(time.Minute))
	if err != nil {
		t.Fatalf("check admission with waiting reset work: %v", err)
	}
	if allowed {
		t.Fatal("waiting reset work was treated as drained")
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs
        SET status = 'complete', updated_at = clock_timestamp()
        WHERE sweep_id = $1 AND work_type = 'reset_baseline'
    `, sweepID); err != nil {
		t.Fatalf("drain reset jobs: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(10*time.Minute)); err != nil {
		t.Fatalf("record reset safe handoff: %v", err)
	}
	created, err = store.scheduleDueRegular(ctx, boundary.Add(10*time.Minute), 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule after safe handoff: %v", err)
	}
	if created != 1 {
		t.Fatalf("regular work after safe handoff = %d, want 1", created)
	}
}
