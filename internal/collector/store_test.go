package collector

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/ClashLens/internal/testsupport"
)

func startContractDatabase(t *testing.T) string {
	t.Helper()

	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(context.Background(), databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()

	schemaPath := filepath.Join("..", "..", "testdata", "contract.sql")
	schema, err := os.ReadFile(schemaPath)
	if err != nil {
		t.Fatalf("read contract fixture: %v", err)
	}
	if _, err := connection.Exec(context.Background(), string(schema)); err != nil {
		t.Fatalf("apply contract fixture: %v", err)
	}
	return databaseURL
}

func TestStoreRejectsIncompatibleContractVersion(t *testing.T) {
	databaseURL := startContractDatabase(t)

	store, err := openStore(context.Background(), databaseURL, 2)
	if !errors.Is(err, errIncompatibleContract) {
		t.Fatalf("openStore error = %v, want errIncompatibleContract", err)
	}
	if store != nil {
		t.Fatal("openStore returned a store for an incompatible contract")
	}
}

func TestScheduleDueRegularCreatesOneCurrentPollAndAdvancesDueTime(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)
	var playerID int64
	err = store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
		RETURNING id
	`, now.Add(-time.Hour)).Scan(&playerID)
	if err != nil {
		t.Fatalf("insert active player: %v", err)
	}

	created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("scheduleDueRegular returned an error: %v", err)
	}
	if created != 1 {
		t.Fatalf("scheduleDueRegular created %d jobs, want 1", created)
	}
	created, err = store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("second scheduleDueRegular returned an error: %v", err)
	}
	if created != 0 {
		t.Fatalf("second scheduleDueRegular created %d jobs, want 0", created)
	}

	var jobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs`).Scan(&jobs); err != nil {
		t.Fatalf("count collector jobs: %v", err)
	}
	if jobs != 1 {
		t.Fatalf("collector job count = %d, want 1", jobs)
	}

	var nextDue time.Time
	if err := store.pool.QueryRow(ctx, `SELECT next_due_at FROM players WHERE id = $1`, playerID).Scan(&nextDue); err != nil {
		t.Fatalf("read next due time: %v", err)
	}
	cycleStart := now.Truncate(5 * time.Minute).Add(5 * time.Minute)
	if nextDue.Before(cycleStart) || !nextDue.Before(cycleStart.Add(5*time.Minute)) {
		t.Fatalf("next due time %s is outside next cycle [%s, %s)", nextDue, cycleStart, cycleStart.Add(5*time.Minute))
	}
}
