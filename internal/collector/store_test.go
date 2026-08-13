package collector

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
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

func TestStoreBridgeAcceptsContractVersionOneWhenConfiguredForVersionTwo(t *testing.T) {
	databaseURL := startContractDatabase(t)

	store, err := openStore(context.Background(), databaseURL, 2)
	if err != nil {
		t.Fatalf("openStore returned an error for supported contract version one: %v", err)
	}
	t.Cleanup(store.close)
	if store.contractVersion != 1 {
		t.Fatalf("store contract version = %d, want actual version 1", store.contractVersion)
	}
}

func TestStoreBridgeRemainsReadyAcrossContractUpgradeToVersionTwo(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	if _, err := store.pool.Exec(ctx, `UPDATE clash_lens_contract SET version = 2 WHERE singleton`); err != nil {
		t.Fatalf("upgrade contract version: %v", err)
	}
	if err := store.ready(ctx); err != nil {
		t.Fatalf("bridge readiness failed after supported live upgrade: %v", err)
	}
}

func TestStoreBridgeRejectsContractVersionThree(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	if _, err := connection.Exec(ctx, `UPDATE clash_lens_contract SET version = 3 WHERE singleton`); err != nil {
		_ = connection.Close(ctx)
		t.Fatalf("set incompatible contract version: %v", err)
	}
	if err := connection.Close(ctx); err != nil {
		t.Fatalf("close test PostgreSQL connection: %v", err)
	}

	store, err := openStore(ctx, databaseURL, 2)
	if !errors.Is(err, errIncompatibleContract) {
		t.Fatalf("openStore error = %v, want errIncompatibleContract", err)
	}
	if store != nil {
		t.Fatal("openStore returned a store for contract version three")
	}
}

func TestClaimRecordsDatabasePoolAcquireDuration(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	store.metrics = newCollectorMetrics()

	job, err := store.claimNext(
		ctx, "metrics-worker", normalPool, time.Now().UTC(), time.Minute, "metrics-token",
	)
	if err != nil {
		t.Fatalf("claimNext returned an error: %v", err)
	}
	if job != nil {
		t.Fatal("claimNext unexpectedly returned a job")
	}

	store.metrics.mu.Lock()
	histogram := store.metrics.stageDurations["claim_pool_acquire"]
	store.metrics.mu.Unlock()
	if histogram.count != 1 {
		t.Fatalf("claim pool-acquire metric count = %d, want 1", histogram.count)
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

func TestScheduleDueRegularEnqueuesTargetPopulationWithoutPerPlayerRoundTrips(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC().Truncate(time.Second)
	const playerCount = 12_500

	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		SELECT '#SCHEDULE' || player_number, true, $1::timestamptz - interval '10 minutes'
		FROM generate_series(1, $2) AS player_number
	`, now, playerCount); err != nil {
		t.Fatalf("seed target scheduling population: %v", err)
	}

	startedAt := time.Now()
	created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, playerCount)
	elapsed := time.Since(startedAt)
	if err != nil {
		t.Fatalf("schedule target population: %v", err)
	}
	if created != playerCount {
		t.Fatalf("scheduled %d players, want %d", created, playerCount)
	}
	if elapsed > 2*time.Second {
		t.Fatalf("target population scheduling took %v, want <= 2s", elapsed)
	}
}

func TestRequiredTrafficGateRejectsVersionOneContract(t *testing.T) {
	databaseURL := startContractDatabase(t)
	store, err := openStore(context.Background(), databaseURL, 2)
	if err != nil {
		t.Fatalf("open bridge store: %v", err)
	}
	t.Cleanup(store.close)

	err = store.validateTrafficGateMode(context.Background(), requiredTrafficGateMode)
	if err == nil || !strings.Contains(err.Error(), "required") {
		t.Fatalf("required traffic-gate validation error = %v, want version-one rejection", err)
	}
}

func TestBridgeTrafficGateAllowsVersionOneContract(t *testing.T) {
	databaseURL := startContractDatabase(t)
	store, err := openStore(context.Background(), databaseURL, 2)
	if err != nil {
		t.Fatalf("open bridge store: %v", err)
	}
	t.Cleanup(store.close)

	if err := store.validateTrafficGateMode(context.Background(), bridgeTrafficGateMode); err != nil {
		t.Fatalf("bridge traffic-gate validation error = %v, want version-one acceptance", err)
	}
}
