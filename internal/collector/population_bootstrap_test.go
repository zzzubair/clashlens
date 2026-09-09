package collector

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

// startPopulationDatabase applies the collector, Python-layer, discovery,
// and population-bootstrap migrations to embedded PostgreSQL and returns
// its URL.
func startPopulationDatabase(t *testing.T, ctx context.Context) string {
	t.Helper()
	bootstrapContext, cancelBootstrap := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancelBootstrap()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(bootstrapContext, databaseURL)
	if err != nil {
		t.Fatalf("connect to population PostgreSQL: %v", err)
	}
	for _, migration := range []string{
		"0001_collector.sql",
		"0002_python_layer.sql",
		"0007_player_discovery.sql",
		"0022_step9_regular_admission_evidence.sql",
		"0023_population_bootstrap.sql",
	} {
		applySQLFile(t, bootstrapContext, connection, filepath.Join("..", "..", "deploy", "migrations", migration))
	}
	if err := connection.Close(bootstrapContext); err != nil {
		t.Fatalf("close migration connection: %v", err)
	}
	return databaseURL
}

// startPopulationStore applies the population migrations and opens a
// version-two store against embedded PostgreSQL.
func startPopulationStore(t *testing.T, ctx context.Context) *store {
	t.Helper()
	databaseURL := startPopulationDatabase(t, ctx)
	bootstrapContext, cancelBootstrap := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancelBootstrap()
	opened, err := openStore(bootstrapContext, databaseURL, 2)
	if err != nil {
		t.Fatalf("open population store: %v", err)
	}
	t.Cleanup(opened.close)
	return opened
}

func testBudgetConfig(caps map[endpointName]int, deadline time.Time) endpointBudgetConfig {
	return endpointBudgetConfig{
		enabled:  true,
		runID:    "b2-test-run",
		caps:     caps,
		deadline: deadline.UTC().Truncate(time.Microsecond),
	}
}

func TestGlobalRankingsEndpointSemantics(t *testing.T) {
	provenance, requestPath, err := officialRequest(globalPlayerRankingsEndpoint, "")
	if err != nil {
		t.Fatalf("officialRequest returned an error: %v", err)
	}
	// The bootstrap ranking allowance is exactly one request: a single
	// Top-200 page with no pagination. Report this shape, not a page count.
	if provenance.method != http.MethodGet {
		t.Fatalf("ranking method = %q, want GET", provenance.method)
	}
	if provenance.path != "/v1/locations/global/rankings/players" || requestPath != "/v1/locations/global/rankings/players" {
		t.Fatalf("ranking path = %q, want the single global Top-200 path", provenance.path)
	}
	if provenance.query != "limit=200" {
		t.Fatalf("ranking query = %q, want one limit=200 page", provenance.query)
	}
	if provenance.sourceAdapterVersion != "global-player-rankings-v1" {
		t.Fatalf("ranking adapter = %q, want global-player-rankings-v1", provenance.sourceAdapterVersion)
	}
}

func TestEnqueueGlobalRankingsCreatesOneAlignedCycle(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	cycle := time.Date(2026, 9, 8, 12, 5, 0, 0, time.UTC)
	created, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if err != nil {
		t.Fatalf("enqueue cycle returned an error: %v", err)
	}
	if !created {
		t.Fatal("enqueue cycle reported no creation on first call")
	}
	var workType, scope, pool, requiredEndpoint, coalescingKey, status string
	var playerID *int64
	var normalizedTag *string
	var priority int
	if err := store.pool.QueryRow(ctx, `
		SELECT work_type, scope, capacity_pool, required_endpoint,
		       coalescing_key, player_id, normalized_tag, priority, status
		FROM collector_jobs
	`).Scan(&workType, &scope, &pool, &requiredEndpoint, &coalescingKey, &playerID, &normalizedTag, &priority, &status); err != nil {
		t.Fatalf("read ranking root: %v", err)
	}
	if workType != "global_player_rankings" || scope != "global" || pool != "normal" ||
		requiredEndpoint != "global_player_rankings" || playerID != nil || normalizedTag != nil {
		t.Fatalf("ranking root identity = %q/%q/%q/%q player=%v tag=%v",
			workType, scope, pool, requiredEndpoint, playerID, normalizedTag)
	}
	if coalescingKey != "global-player-rankings:2026-09-08T12:05:00Z" {
		t.Fatalf("ranking coalescing key = %q", coalescingKey)
	}
	if priority != 300 || status != "pending" {
		t.Fatalf("ranking root priority/status = %d/%q, want 300/pending", priority, status)
	}
	var intents int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM global_rankings_intents`).Scan(&intents); err != nil {
		t.Fatalf("count ranking intents: %v", err)
	}
	if intents != 1 {
		t.Fatalf("ranking intents = %d, want 1", intents)
	}

	resetCycle := time.Date(2026, 9, 8, 5, 0, 0, 0, time.UTC)
	if _, err := store.enqueueGlobalRankingsCycle(ctx, resetCycle); err != nil {
		t.Fatalf("enqueue reset-boundary cycle returned an error: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT priority FROM collector_jobs
		WHERE coalescing_key = 'global-player-rankings:2026-09-08T05:00:00Z'
	`).Scan(&priority); err != nil {
		t.Fatalf("read reset-boundary priority: %v", err)
	}
	if priority != 400 {
		t.Fatalf("reset-boundary priority = %d, want 400", priority)
	}
}

func TestEnqueueGlobalRankingsCollisionIsIdempotentWhenIdentical(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	cycle := time.Date(2026, 9, 8, 12, 10, 0, 0, time.UTC)
	if _, err := store.enqueueGlobalRankingsCycle(ctx, cycle); err != nil {
		t.Fatalf("first enqueue returned an error: %v", err)
	}
	created, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if err != nil {
		t.Fatalf("second enqueue returned an error: %v", err)
	}
	if created {
		t.Fatal("second enqueue reported creation for an identical cycle")
	}
	var jobs, intents int
	if err := store.pool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM collector_jobs),
		       (SELECT count(*) FROM global_rankings_intents)
	`).Scan(&jobs, &intents); err != nil {
		t.Fatalf("count ranking rows: %v", err)
	}
	if jobs != 1 || intents != 1 {
		t.Fatalf("ranking rows = %d jobs and %d intents, want 1 and 1", jobs, intents)
	}
}

func TestEnqueueGlobalRankingsCollisionFailsClosed(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	cycle := time.Date(2026, 9, 8, 12, 15, 0, 0, time.UTC)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, required_endpoint, status
		) VALUES ('endpoint_retry', 'global', NULL, NULL, 'normal',
			300, $1, $2, 'profile', 'pending')
	`, cycle, globalRankingsCoalescingKey(cycle)); err != nil {
		t.Fatalf("seed conflicting job: %v", err)
	}
	_, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if !errors.Is(err, errGlobalRankingsCollision) {
		t.Fatalf("enqueue error = %v, want errGlobalRankingsCollision", err)
	}
	var jobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs`).Scan(&jobs); err != nil {
		t.Fatalf("count jobs after collision: %v", err)
	}
	if jobs != 1 {
		t.Fatalf("jobs after collision = %d, want the single conflicting row", jobs)
	}
}

func TestEnqueueGlobalRankingsRepairsMissingRoot(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	cycle := time.Date(2026, 9, 8, 12, 20, 0, 0, time.UTC)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO global_rankings_intents (cycle_at) VALUES ($1)
	`, cycle); err != nil {
		t.Fatalf("seed lone intent: %v", err)
	}
	created, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if err != nil {
		t.Fatalf("enqueue with lone intent returned an error: %v", err)
	}
	if !created {
		t.Fatal("enqueue with lone intent reported no creation")
	}
	// A second call proves the repaired root verifies: identical replay
	// is an idempotent no-op.
	again, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if err != nil {
		t.Fatalf("replay after repair returned an error: %v", err)
	}
	if again {
		t.Fatal("replay after repair reported creation")
	}
}

func TestEnqueueGlobalRankingsTerminalReplayIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	cycle := time.Date(2026, 9, 8, 12, 25, 0, 0, time.UTC)
	if _, err := store.enqueueGlobalRankingsCycle(ctx, cycle); err != nil {
		t.Fatalf("first enqueue returned an error: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete'
		WHERE coalescing_key = $1
	`, globalRankingsCoalescingKey(cycle)); err != nil {
		t.Fatalf("complete ranking root: %v", err)
	}
	created, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
	if err != nil {
		t.Fatalf("terminal replay returned an error: %v", err)
	}
	if created {
		t.Fatal("terminal replay reported creation; it must not re-arm a terminal root")
	}
	var jobs, intents int
	if err := store.pool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM collector_jobs),
		       (SELECT count(*) FROM global_rankings_intents)
	`).Scan(&jobs, &intents); err != nil {
		t.Fatalf("count ranking rows: %v", err)
	}
	if jobs != 1 || intents != 1 {
		t.Fatalf("ranking rows = %d jobs and %d intents, want 1 and 1", jobs, intents)
	}
}

func TestPopulationBootstrapRoleBoundaries(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	databaseURL := startPopulationDatabase(t, ctx)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect for role probes: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })

	const (
		workerRole    = "clashlens_python_worker"
		collectorRole = "clashlens_collector"
	)
	// Worker: owns the bootstrap run record and reads only the ranking
	// cycle identity; it cannot touch the collector's budget ledger or
	// insert ranking intents.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin worker probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+workerRole); err != nil {
		t.Fatalf("set worker role: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO population_bootstrap_runs (
			run_id, manifest_sha256, manifest_count,
			normalized_set_sha256, status, batch_size
		) VALUES ('role-probe', repeat('a', 64), 1, repeat('b', 64), 'started', 500)
	`); err != nil {
		t.Fatalf("worker insert bootstrap run: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE population_bootstrap_runs
		SET status = 'complete', completed_at = clock_timestamp()
		WHERE run_id = 'role-probe'
	`); err != nil {
		t.Fatalf("worker update bootstrap run: %v", err)
	}
	var runStatus string
	if err := connection.QueryRow(ctx, `
		SELECT status FROM population_bootstrap_runs WHERE run_id = 'role-probe'
	`).Scan(&runStatus); err != nil || runStatus != "complete" {
		t.Fatalf("worker read bootstrap run: status=%q err=%v", runStatus, err)
	}
	var intentCycles int
	if err := connection.QueryRow(ctx, `SELECT count(cycle_at) FROM global_rankings_intents`).Scan(&intentCycles); err != nil {
		t.Fatalf("worker read ranking cycle identity: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		INSERT INTO global_rankings_intents (cycle_at) VALUES ('2026-09-08T12:05:00+00')
	`, "worker inserting ranking intents")
	// The worker reads budget aggregates for monitoring but cannot mint or
	// consume budget units.
	var budgetRows int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM collector_endpoint_budgets`).Scan(&budgetRows); err != nil {
		t.Fatalf("worker read endpoint budget aggregates: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		INSERT INTO collector_endpoint_budgets (run_id, endpoint, cap, consumed, deadline_at)
		VALUES ('role-probe', 'profile', 1, 0, clock_timestamp())
	`, "worker writing endpoint budgets")
	expectInsufficientPrivilege(t, ctx, connection, `
		UPDATE collector_endpoint_budgets SET consumed = 1 WHERE run_id = 'role-probe'
	`, "worker consuming endpoint budgets")
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset worker role: %v", err)
	}
	if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
		t.Fatalf("commit worker probe transaction: %v", err)
	}

	// Collector: owns the budget ledger and the ranking intent insert path;
	// it cannot touch the worker's bootstrap run record.
	if _, err := connection.Exec(ctx, `BEGIN`); err != nil {
		t.Fatalf("begin collector probe transaction: %v", err)
	}
	if _, err := connection.Exec(ctx, `SET ROLE `+collectorRole); err != nil {
		t.Fatalf("set collector role: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_endpoint_budgets (run_id, endpoint, cap, consumed, deadline_at)
		VALUES ('role-probe', 'profile', 1, 0, clock_timestamp())
	`); err != nil {
		t.Fatalf("collector insert endpoint budget: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE collector_endpoint_budgets SET consumed = 1 WHERE run_id = 'role-probe' AND endpoint = 'profile'
	`); err != nil {
		t.Fatalf("collector consume endpoint budget: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO global_rankings_intents (cycle_at) VALUES ('2026-09-08T12:05:00+00')
	`); err != nil {
		t.Fatalf("collector insert ranking intent: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `
		INSERT INTO population_bootstrap_runs (
			run_id, manifest_sha256, manifest_count,
			normalized_set_sha256, status, batch_size
		) VALUES ('collector-probe', repeat('c', 64), 1, repeat('d', 64), 'started', 500)
	`, "collector writing bootstrap runs")
	if _, err := connection.Exec(ctx, `RESET ROLE`); err != nil {
		t.Fatalf("reset collector role: %v", err)
	}
	if _, err := connection.Exec(ctx, `COMMIT`); err != nil {
		t.Fatalf("commit collector probe transaction: %v", err)
	}
}

func TestEndpointBudgetReserveConsumeExhaust(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              2,
		globalPlayerRankingsEndpoint: 1,
		battleLogEndpoint:            0,
	}, time.Now().UTC().Add(time.Hour)))

	if err := store.reserveEndpointBudget(ctx, profileEndpoint); err != nil {
		t.Fatalf("first profile reservation returned an error: %v", err)
	}
	if err := store.reserveEndpointBudget(ctx, profileEndpoint); err != nil {
		t.Fatalf("second profile reservation returned an error: %v", err)
	}
	if err := store.reserveEndpointBudget(ctx, profileEndpoint); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("third profile reservation error = %v, want errEndpointBudgetExhausted", err)
	}
	if err := store.reserveEndpointBudget(ctx, battleLogEndpoint); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("battle-log reservation error = %v, want errEndpointBudgetExhausted", err)
	}
	if err := store.reserveEndpointBudget(ctx, globalPlayerRankingsEndpoint); err != nil {
		t.Fatalf("ranking reservation returned an error: %v", err)
	}
	if err := store.reserveEndpointBudget(ctx, globalPlayerRankingsEndpoint); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("second ranking reservation error = %v, want errEndpointBudgetExhausted", err)
	}
	var profileConsumed, battleConsumed int
	if err := store.pool.QueryRow(ctx, `
		SELECT consumed FROM collector_endpoint_budgets WHERE run_id = 'b2-test-run' AND endpoint = 'profile'
	`).Scan(&profileConsumed); err != nil {
		t.Fatalf("read profile budget: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT consumed FROM collector_endpoint_budgets WHERE run_id = 'b2-test-run' AND endpoint = 'battle_log'
	`).Scan(&battleConsumed); err != nil {
		t.Fatalf("read battle-log budget: %v", err)
	}
	if profileConsumed != 2 || battleConsumed != 0 {
		t.Fatalf("consumed = profile %d, battle-log %d; want 2 and 0", profileConsumed, battleConsumed)
	}
}

func TestEndpointBudgetConcurrentReservationsNeverExceedCap(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              5,
		globalPlayerRankingsEndpoint: 0,
		battleLogEndpoint:            0,
	}, time.Now().UTC().Add(time.Hour)))

	const attempts = 20
	var wait sync.WaitGroup
	results := make(chan error, attempts)
	for range attempts {
		wait.Add(1)
		go func() {
			defer wait.Done()
			results <- store.reserveEndpointBudget(ctx, profileEndpoint)
		}()
	}
	wait.Wait()
	close(results)
	var granted, exhausted int
	for err := range results {
		if err == nil {
			granted++
		} else if errors.Is(err, errEndpointBudgetExhausted) {
			exhausted++
		} else {
			t.Fatalf("concurrent reservation error = %v", err)
		}
	}
	if granted != 5 || exhausted != attempts-5 {
		t.Fatalf("concurrent reservations granted %d and exhausted %d, want 5 and %d", granted, exhausted, attempts-5)
	}
}

func TestEndpointBudgetPersistsAcrossRestartAndConflictsOnCapChange(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	databaseURL := startPopulationDatabase(t, ctx)
	bootstrapContext, cancelBootstrap := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancelBootstrap()
	store, err := openStore(bootstrapContext, databaseURL, 2)
	if err != nil {
		t.Fatalf("open population store: %v", err)
	}
	t.Cleanup(store.close)

	caps := map[endpointName]int{
		profileEndpoint:              1,
		globalPlayerRankingsEndpoint: 0,
		battleLogEndpoint:            0,
	}
	deadline := time.Now().UTC().Add(time.Hour)
	store.setEndpointBudget(testBudgetConfig(caps, deadline))
	if err := store.reserveEndpointBudget(ctx, profileEndpoint); err != nil {
		t.Fatalf("first reservation returned an error: %v", err)
	}
	store.close()

	// A fresh store object against the same database inherits the consumed
	// budget: restart persistence without any refund.
	restarted, err := openStore(bootstrapContext, databaseURL, 2)
	if err != nil {
		t.Fatalf("reopen population store: %v", err)
	}
	t.Cleanup(restarted.close)
	restarted.setEndpointBudget(testBudgetConfig(caps, deadline))
	if err := restarted.reserveEndpointBudget(ctx, profileEndpoint); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("post-restart reservation error = %v, want errEndpointBudgetExhausted", err)
	}
	// Reconfiguring the cap against durable state fails closed instead of
	// silently adopting the new ceiling.
	restarted.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              2,
		globalPlayerRankingsEndpoint: 0,
		battleLogEndpoint:            0,
	}, deadline))
	if err := restarted.reserveEndpointBudget(ctx, profileEndpoint); !errors.Is(err, errEndpointBudgetConflict) {
		t.Fatalf("cap-change reservation error = %v, want errEndpointBudgetConflict", err)
	}
}

func TestEndpointBudgetDeadlineFailsClosed(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              100,
		globalPlayerRankingsEndpoint: 100,
		battleLogEndpoint:            100,
	}, time.Now().UTC().Add(-time.Minute)))

	if err := store.reserveEndpointBudget(ctx, profileEndpoint); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("past-deadline reservation error = %v, want errEndpointBudgetExhausted", err)
	}
	var consumed int
	if err := store.pool.QueryRow(ctx, `
		SELECT consumed FROM collector_endpoint_budgets WHERE run_id = 'b2-test-run' AND endpoint = 'profile'
	`).Scan(&consumed); err != nil {
		t.Fatalf("read expired budget: %v", err)
	}
	if consumed != 0 {
		t.Fatalf("expired budget consumed = %d, want 0", consumed)
	}
}

func TestBeginEndpointRequestFailsClosedOnExhaustedBudget(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule regular work: %v", err)
	}
	job, err := store.claimNext(ctx, "budget-owner", normalPool, now, time.Minute, "budget-token")
	if err != nil {
		t.Fatalf("claim job: %v", err)
	}
	if job == nil {
		t.Fatal("claim returned no job")
	}
	attemptID, _, err := store.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepare attempt: %v", err)
	}
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              0,
		globalPlayerRankingsEndpoint: 0,
		battleLogEndpoint:            0,
	}, now.Add(time.Hour)))
	if _, err := store.beginEndpointRequest(ctx, job, attemptID, profileEndpoint, now); !errors.Is(err, errEndpointBudgetExhausted) {
		t.Fatalf("beginEndpointRequest error = %v, want errEndpointBudgetExhausted", err)
	}
	// Deterministic bookkeeping runs before the reservation, so the
	// attempt counter may advance; the denial itself must consume
	// nothing and admit no dispatch (proven end-to-end by
	// TestBudgetDenialNeverCallsHTTP).
	var consumed int
	if err := store.pool.QueryRow(ctx, `
		SELECT consumed FROM collector_endpoint_budgets
		WHERE run_id = 'b2-test-run' AND endpoint = 'profile'
	`).Scan(&consumed); err != nil {
		t.Fatalf("read denied budget: %v", err)
	}
	if consumed != 0 {
		t.Fatalf("denied reservation consumed %d units, want 0", consumed)
	}
}

func TestBudgetedClientDeniesRedirects(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/target" {
			response.Header().Set("Content-Type", "application/json")
			_, _ = response.Write([]byte(`{}`))
			return
		}
		http.Redirect(response, request, "/target", http.StatusFound)
	}))
	t.Cleanup(server.Close)

	base := officialAPIConfig{
		origin:                server.URL,
		allowInsecureTestHTTP: true,
		connectionTimeout:     time.Second,
		responseHeaderTimeout: time.Second,
		totalTimeout:          5 * time.Second,
		maximumResponseBytes:  1 << 20,
	}
	budgeted, err := newOfficialAPIClient(officialAPIConfig{
		origin:                base.origin,
		allowInsecureTestHTTP: true,
		disableRedirects:       true,
		connectionTimeout:     base.connectionTimeout,
		responseHeaderTimeout: base.responseHeaderTimeout,
		totalTimeout:          base.totalTimeout,
		maximumResponseBytes:  base.maximumResponseBytes,
	})
	if err != nil {
		t.Fatalf("create budgeted client: %v", err)
	}
	if _, err := budgeted.fetch(context.Background(), profileEndpoint, "#2PP", "secret"); err == nil ||
		!strings.Contains(err.Error(), "redirects are disabled") {
		t.Fatalf("budgeted fetch error = %v, want redirect denial", err)
	}
	ordinary, err := newOfficialAPIClient(base)
	if err != nil {
		t.Fatalf("create ordinary client: %v", err)
	}
	response, err := ordinary.fetch(context.Background(), profileEndpoint, "#2PP", "secret")
	if err != nil {
		t.Fatalf("ordinary fetch returned an error: %v", err)
	}
	if response.statusCode != http.StatusOK {
		t.Fatalf("ordinary fetch status = %d, want 200", response.statusCode)
	}
}

func TestLoadEndpointBudgetConfig(t *testing.T) {
	deadline := time.Now().UTC().Add(time.Hour).Format(time.RFC3339)
	settings := map[string]string{
		"CLASHLENS_ENDPOINT_BUDGET_ENABLED":          "true",
		"CLASHLENS_ENDPOINT_BUDGET_RUN_ID":           "issue92",
		"CLASHLENS_ENDPOINT_BUDGET_PROFILE":          "13500",
		"CLASHLENS_ENDPOINT_BUDGET_GLOBAL_RANKINGS":  "1",
		"CLASHLENS_ENDPOINT_BUDGET_BATTLE_LOG":       "0",
		"CLASHLENS_ENDPOINT_BUDGET_DEADLINE_AT":      deadline,
	}
	getenv := func(name string) string { return settings[name] }
	config, err := loadEndpointBudgetConfig(getenv)
	if err != nil {
		t.Fatalf("load budget config returned an error: %v", err)
	}
	if !config.enabled || config.runID != "issue92" {
		t.Fatalf("budget config = %+v, want enabled issue92", config)
	}
	if config.caps[profileEndpoint] != 13500 || config.caps[globalPlayerRankingsEndpoint] != 1 || config.caps[battleLogEndpoint] != 0 {
		t.Fatalf("budget caps = %v, want profile=13500 ranking=1 battlelog=0", config.caps)
	}

	disabled, err := loadEndpointBudgetConfig(func(string) string { return "" })
	if err != nil || disabled.enabled {
		t.Fatalf("empty environment config = %+v err=%v, want disabled", disabled, err)
	}
	for name, mutate := range map[string]func(map[string]string){
		"bad enabled":  func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_ENABLED"] = "yes" },
		"missing run":  func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_RUN_ID"] = "" },
		"bad run":      func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_RUN_ID"] = "has space" },
		"bad deadline": func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_DEADLINE_AT"] = "tomorrow" },
		"bad cap":      func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_PROFILE"] = "-1" },
		"missing cap":  func(s map[string]string) { s["CLASHLENS_ENDPOINT_BUDGET_BATTLE_LOG"] = "" },
	} {
		mutated := map[string]string{}
		for key, value := range settings {
			mutated[key] = value
		}
		mutate(mutated)
		if _, err := loadEndpointBudgetConfig(func(name string) string { return mutated[name] }); err == nil {
			t.Fatalf("budget config accepted %s", name)
		}
	}
}

func TestEnqueueGlobalRankingsRejectsMisalignedCycles(t *testing.T) {
	for _, cycleAt := range []string{
		"not-a-time",
		"2026-09-08T12:03:00Z",
		"2026-09-08T12:05:30Z",
		"2026-09-08",
	} {
		var stdout, stderr bytes.Buffer
		err := RunCLI(
			context.Background(),
			[]string{"enqueue-global-rankings", "--cycle-at", cycleAt},
			func(string) string { return "" },
			&stdout,
			&stderr,
		)
		if err == nil {
			t.Fatalf("enqueue-global-rankings accepted %q", cycleAt)
		}
	}
	var stdout, stderr bytes.Buffer
	if err := RunCLI(
		context.Background(),
		[]string{"enqueue-global-rankings"},
		func(string) string { return "" },
		&stdout,
		&stderr,
	); err == nil {
		t.Fatal("enqueue-global-rankings accepted a missing --cycle-at")
	}
}

func TestBeginEndpointRequestLeaseFailureDoesNotConsumeBudget(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule regular work: %v", err)
	}
	job, err := store.claimNext(ctx, "budget-lease-owner", normalPool, now, time.Minute, "budget-lease-token")
	if err != nil {
		t.Fatalf("claim job: %v", err)
	}
	if job == nil {
		t.Fatal("claim returned no job")
	}
	attemptID, _, err := store.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepare attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET lease_expires_at = clock_timestamp() - interval '1 second'
		WHERE id = $1
	`, job.id); err != nil {
		t.Fatalf("expire lease: %v", err)
	}
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              5,
		globalPlayerRankingsEndpoint: 5,
		battleLogEndpoint:            5,
	}, now.Add(time.Hour)))
	if _, err := store.beginEndpointRequest(ctx, job, attemptID, profileEndpoint, now); !errors.Is(err, errLeaseLost) {
		t.Fatalf("beginEndpointRequest error = %v, want errLeaseLost", err)
	}
	var budgetRows int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_endpoint_budgets`).Scan(&budgetRows); err != nil {
		t.Fatalf("count budget rows: %v", err)
	}
	if budgetRows != 0 {
		t.Fatalf("lease failure consumed budget: %d budget rows, want 0", budgetRows)
	}
}

func TestBudgetDenialNeverCallsHTTP(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule regular work: %v", err)
	}
	var hits atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		hits.Add(1)
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{}`))
	}))
	t.Cleanup(api.Close)
	keys, err := newKeyPool([]APIKey{{Label: "normal-1", Secret: "normal-secret", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("create normal key pool: %v", err)
	}
	worker := newWorker(store, &memoryArchive{}, newTestOfficialAPIClient(t, api.URL, 1<<20), keys, workerConfig{
		owner:            "budget-denial-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "collector-test",
		maximumRetries:   0,
		metrics:          newCollectorMetrics(),
	})
	store.setEndpointBudget(testBudgetConfig(map[endpointName]int{
		profileEndpoint:              0,
		globalPlayerRankingsEndpoint: 0,
		battleLogEndpoint:            0,
	}, now.Add(time.Hour)))
	_, err = worker.runOnce(ctx, normalPool)
	if err == nil || !strings.Contains(err.Error(), "exhausted") {
		t.Fatalf("worker run error = %v, want budget exhaustion", err)
	}
	if got := hits.Load(); got != 0 {
		t.Fatalf("denied dispatch reached HTTP %d times, want 0", got)
	}
}

func TestGlobalRankingsConcurrentAdmissionConvergesToOneRoot(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	store := startPopulationStore(t, ctx)

	// Scheduler and manual admission race the same fresh cycle: the shared
	// cycle lock must converge them onto exactly one intent and one root.
	cycle := time.Date(2026, 9, 8, 12, 30, 0, 0, time.UTC)
	const racers = 8
	var wait sync.WaitGroup
	errs := make(chan error, 2*racers)
	for range racers {
		wait.Add(2)
		go func() {
			defer wait.Done()
			_, err := store.scheduleGlobalRankings(ctx, cycle, 5*time.Minute)
			errs <- err
		}()
		go func() {
			defer wait.Done()
			_, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
			errs <- err
		}()
	}
	wait.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("concurrent admission error = %v", err)
		}
	}
	var jobs, intents int
	if err := store.pool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM collector_jobs),
		       (SELECT count(*) FROM global_rankings_intents)
	`).Scan(&jobs, &intents); err != nil {
		t.Fatalf("count ranking rows: %v", err)
	}
	if jobs != 1 || intents != 1 {
		t.Fatalf("concurrent admission left %d jobs and %d intents, want 1 and 1", jobs, intents)
	}

	// The same race against a terminal root must stay a no-op: exactly one
	// root, none rearmed.
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete'
		WHERE coalescing_key = $1
	`, globalRankingsCoalescingKey(cycle)); err != nil {
		t.Fatalf("complete ranking root: %v", err)
	}
	var terminalWait sync.WaitGroup
	terminalErrs := make(chan error, racers)
	created := make(chan bool, racers)
	for range racers {
		terminalWait.Add(1)
		go func() {
			defer terminalWait.Done()
			made, err := store.enqueueGlobalRankingsCycle(ctx, cycle)
			terminalErrs <- err
			created <- made
		}()
	}
	terminalWait.Wait()
	close(terminalErrs)
	close(created)
	for err := range terminalErrs {
		if err != nil {
			t.Fatalf("terminal-race admission error = %v", err)
		}
	}
	for made := range created {
		if made {
			t.Fatal("terminal-race admission re-armed the cycle")
		}
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs`).Scan(&jobs); err != nil {
		t.Fatalf("recount jobs: %v", err)
	}
	if jobs != 1 {
		t.Fatalf("terminal race left %d jobs, want 1", jobs)
	}
}
