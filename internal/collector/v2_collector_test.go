package collector

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/ClashLens/internal/testsupport"
)

type failResetBattleLogArchive struct {
	mu       sync.Mutex
	failed   bool
	delegate memoryArchive
}

func (a *failResetBattleLogArchive) store(ctx context.Context, hash string, body []byte) (string, error) {
	a.mu.Lock()
	if !a.failed && string(body) == `{"items":[]}` {
		a.failed = true
		a.mu.Unlock()
		return "", errors.New("injected reset battle-log archive failure")
	}
	a.mu.Unlock()
	return a.delegate.store(ctx, hash, body)
}

func TestVersionTwoResetSweepCreatesPairedPerPlayerBaselines(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, clock_timestamp()), ('#2PQ', true, clock_timestamp())
	`); err != nil {
		t.Fatalf("insert reset players: %v", err)
	}

	boundary := time.Date(2026, time.August, 4, 5, 0, 0, 0, time.UTC)
	sweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("schedule version-two reset sweep: %v", err)
	}
	if !created {
		t.Fatal("version-two reset sweep was not created")
	}

	var members, baselines, jobs, invalid int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM collector_reset_baseline_sweeps
			 WHERE reset_sweep_id = $1 AND evidence_kind = 'paired_v2'),
			(SELECT count(*) FROM collector_jobs
			 WHERE sweep_id = $1 AND work_type = 'reset_baseline'),
			(SELECT count(*)
			 FROM collector_jobs AS job
			 JOIN collector_reset_baseline_sweeps AS baseline
			   ON baseline.id = job.reset_baseline_sweep_id
			 WHERE job.sweep_id = $1
			   AND (job.player_id <> baseline.player_id
			        OR job.required_endpoint IS NOT NULL
			        OR job.scope <> 'player'))
	`, sweepID).Scan(&members, &baselines, &jobs, &invalid); err != nil {
		t.Fatalf("read paired reset outputs: %v", err)
	}
	if members != 2 || baselines != 2 || jobs != 2 || invalid != 0 {
		t.Fatalf("paired reset outputs = %d members, %d baselines, %d jobs, %d invalid", members, baselines, jobs, invalid)
	}

	claimAt := time.Now().UTC()
	job, err := store.claimNext(ctx, "reset-test", normalPool, claimAt, time.Minute, "reset-token")
	if err != nil {
		t.Fatalf("claim reset baseline: %v", err)
	}
	if job == nil || job.workType != "reset_baseline" || !job.resetBaselineSweepID.Valid {
		t.Fatalf("claimed reset job = %+v", job)
	}
	_, endpoints, err := store.prepareAttempt(ctx, job, claimAt.Add(time.Second))
	if err != nil {
		t.Fatalf("prepare paired reset attempt: %v", err)
	}
	if len(endpoints) != 2 || endpoints[0] != battleLogEndpoint || endpoints[1] != profileEndpoint {
		t.Fatalf("paired reset endpoints = %v, want battle log and profile", endpoints)
	}
}

func TestVersionTwoResetBaselineCompletesOnlyAfterBothEndpoints(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, clock_timestamp())
	`); err != nil {
		t.Fatalf("insert reset player: %v", err)
	}
	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	sweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("schedule reset sweep: %v", err)
	}
	if !created {
		t.Fatal("reset sweep was not created")
	}

	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		if request.URL.Path == "/v1/players/#2PP/battlelog" {
			_, _ = io.WriteString(response, `{"items":[]}`)
			return
		}
		_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
	}))
	t.Cleanup(api.Close)
	official := newTestOfficialAPIClient(t, api.URL, 1<<20)
	keys, err := newKeyPool([]APIKey{{Label: "normal-1", Secret: "normal-secret", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("create normal key pool: %v", err)
	}
	worker := newWorker(store, &failResetBattleLogArchive{}, official, keys, workerConfig{
		owner:            "reset-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "collector-v2",
		maximumRetries:   1,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})

	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("first reset worker run: %v", err)
	}
	if !claimed {
		t.Fatal("first reset worker run did not claim reset work")
	}
	var observations int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count first reset observations: %v", err)
	}
	if observations != 1 {
		t.Fatalf("first reset observations = %d, want 1", observations)
	}
	var firstObserved, firstIncomplete int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE endpoint = 'profile' AND outcome = 'observed'),
			count(*) FILTER (WHERE endpoint = 'battle_log' AND outcome <> 'observed')
		FROM collector_endpoint_results
		WHERE attempt_id = (
			SELECT result_attempt_id
			FROM collector_jobs
			WHERE sweep_id = $1 AND work_type = 'reset_baseline'
		)
	`, sweepID).Scan(&firstObserved, &firstIncomplete); err != nil {
		t.Fatalf("read first reset endpoint outcomes: %v", err)
	}
	if firstObserved != 1 || firstIncomplete != 1 {
		t.Fatalf("first reset endpoint outcomes = %d observed and %d incomplete, want 1 and 1", firstObserved, firstIncomplete)
	}
	var baselineState string
	if err := store.pool.QueryRow(ctx, `
		SELECT state
		FROM collector_reset_baseline_sweeps
		WHERE reset_sweep_id = $1
	`, sweepID).Scan(&baselineState); err != nil {
		t.Fatalf("read incomplete reset baseline: %v", err)
	}
	if baselineState != "incomplete" {
		t.Fatalf("reset baseline state after one archived endpoint = %q, want incomplete", baselineState)
	}

	claimed, err = worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("retry reset worker run: %v", err)
	}
	if !claimed {
		t.Fatal("retry reset worker run did not claim battle-log retry")
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count final reset observations: %v", err)
	}
	if observations != 2 {
		t.Fatalf("final reset observations = %d, want 2", observations)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT state
		FROM collector_reset_baseline_sweeps
		WHERE reset_sweep_id = $1
	`, sweepID).Scan(&baselineState); err != nil {
		t.Fatalf("read completed reset baseline: %v", err)
	}
	if baselineState != "complete" {
		t.Fatalf("reset baseline state after both archived endpoints = %q, want complete", baselineState)
	}
	var endpointCount, observedEndpoints, completedJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_endpoint_results WHERE attempt_id = (
				SELECT id FROM collector_attempts WHERE job_id = (
					SELECT id FROM collector_jobs WHERE sweep_id = $1 AND work_type = 'reset_baseline'
				)
			)),
			(SELECT count(*) FROM collector_endpoint_results
			 WHERE attempt_id = (
				 SELECT id FROM collector_attempts WHERE job_id = (
					 SELECT id FROM collector_jobs WHERE sweep_id = $1 AND work_type = 'reset_baseline'
				 )
			 ) AND outcome = 'observed'),
			(SELECT count(*) FROM collector_jobs WHERE sweep_id = $1 AND status = 'complete')
	`, sweepID).Scan(&endpointCount, &observedEndpoints, &completedJobs); err != nil {
		t.Fatalf("read completed reset endpoints and jobs: %v", err)
	}
	if endpointCount != 2 || observedEndpoints != 2 || completedJobs != 2 {
		t.Fatalf("completed reset outputs = %d endpoints, %d observed, and %d jobs, want 2, 2, and 2", endpointCount, observedEndpoints, completedJobs)
	}
}

func TestVersionTwoGlobalRankingsScheduleIsFiveMinuteAndBoundaryPrioritized(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	boundary := time.Date(2026, time.August, 4, 5, 0, 0, 0, time.UTC)

	created, err := store.scheduleGlobalRankings(ctx, boundary, 5*time.Minute)
	if err != nil {
		t.Fatalf("schedule boundary global rankings: %v", err)
	}
	if !created {
		t.Fatal("boundary global rankings job was not created")
	}
	created, err = store.scheduleGlobalRankings(ctx, boundary.Add(4*time.Minute), 5*time.Minute)
	if err != nil {
		t.Fatalf("reschedule same global rankings cycle: %v", err)
	}
	if created {
		t.Fatal("global rankings schedule duplicated one five-minute cycle")
	}
	created, err = store.scheduleGlobalRankings(ctx, boundary.Add(5*time.Minute), 5*time.Minute)
	if err != nil {
		t.Fatalf("schedule next global rankings cycle: %v", err)
	}
	if !created {
		t.Fatal("next global rankings cycle was not created")
	}

	rows, err := store.pool.Query(ctx, `
		SELECT priority, scope, player_id, normalized_tag, required_endpoint
		FROM collector_jobs
		WHERE work_type = 'global_player_rankings'
		ORDER BY due_at
	`)
	if err != nil {
		t.Fatalf("read global rankings jobs: %v", err)
	}
	defer rows.Close()
	var priorities []int
	for rows.Next() {
		var priority int
		var scope, endpoint string
		var playerID *int64
		var tag *string
		if err := rows.Scan(&priority, &scope, &playerID, &tag, &endpoint); err != nil {
			t.Fatalf("scan global rankings job: %v", err)
		}
		if scope != "global" || playerID != nil || tag != nil || endpoint != "global_player_rankings" {
			t.Fatalf("invalid global rankings identity: scope=%q player=%v tag=%v endpoint=%q", scope, playerID, tag, endpoint)
		}
		priorities = append(priorities, priority)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate global rankings jobs: %v", err)
	}
	if len(priorities) != 2 || priorities[0] != 400 || priorities[1] != 300 {
		t.Fatalf("global rankings priorities = %v, want [400 300]", priorities)
	}
}

func TestVersionTwoWorkerArchivesGlobalRankingsWithValidationHandoffProvenance(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	if _, err := store.scheduleGlobalRankings(ctx, now, 5*time.Minute); err != nil {
		t.Fatalf("schedule global rankings: %v", err)
	}

	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Method != http.MethodGet || request.URL.Path != "/v1/locations/global/rankings/players" || request.URL.RawQuery != "limit=200" {
			t.Errorf("global rankings request = %s %s?%s", request.Method, request.URL.Path, request.URL.RawQuery)
		}
		response.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(response, `{"items":[],"paging":{}}`)
	}))
	t.Cleanup(api.Close)
	official := newTestOfficialAPIClient(t, api.URL, 1<<20)
	keys, err := newKeyPool([]APIKey{{Label: "normal-1", Secret: "normal-secret", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("create normal key pool: %v", err)
	}
	metrics := newCollectorMetrics()
	var logOutput bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logOutput, nil))
	worker := newWorker(store, &memoryArchive{}, official, keys, workerConfig{
		owner:            "global-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "collector-v2",
		maximumRetries:   0,
		metrics:          metrics,
		logger:           logger,
	})
	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("collect global rankings: %v", err)
	}
	if !claimed {
		t.Fatal("global rankings worker did not claim scheduled work")
	}
	metrics.mu.Lock()
	requests := metrics.apiRequests["global_player_rankings\x00normal"]
	outcomes := metrics.apiOutcomes["global_player_rankings\x002xx"]
	durations := metrics.apiDurationCount["global_player_rankings\x00normal"]
	metrics.mu.Unlock()
	if requests != 1 || outcomes != 1 || durations != 1 {
		t.Fatalf("global rankings endpoint metrics = requests/outcomes/durations %d/%d/%d, want 1/1/1", requests, outcomes, durations)
	}
	if !strings.Contains(logOutput.String(), `"endpoint":"global_player_rankings"`) {
		t.Fatalf("global rankings structured logs do not identify endpoint: %s", logOutput.String())
	}

	var scope, endpoint, method, path, query, paging, adapter string
	var playerID *int64
	var tag *string
	if err := store.pool.QueryRow(ctx, `
		SELECT scope, player_id, normalized_tag, endpoint, request_method,
		       request_path, request_query, paging_envelope_state,
		       source_adapter_version
		FROM collector_observations
	`).Scan(&scope, &playerID, &tag, &endpoint, &method, &path, &query, &paging, &adapter); err != nil {
		t.Fatalf("read global rankings observation: %v", err)
	}
	if scope != "global" || playerID != nil || tag != nil || endpoint != "global_player_rankings" ||
		method != "GET" || path != "/v1/locations/global/rankings/players" || query != "limit=200" ||
		paging != "not_present" || adapter != "global-player-rankings-v1" {
		t.Fatalf("global rankings provenance = scope=%q player=%v tag=%v endpoint=%q %s %s?%s paging=%q adapter=%q",
			scope, playerID, tag, endpoint, method, path, query, paging, adapter)
	}
	var jobs int
	var parserVersion string
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*), min(parser_version) FROM python_processing_jobs
	`).Scan(&jobs, &parserVersion); err != nil {
		t.Fatalf("read global rankings validation handoff: %v", err)
	}
	if jobs != 1 || parserVersion != "supercell-source-parser-v2" {
		t.Fatalf("global rankings validation handoff = %d jobs with parser %q", jobs, parserVersion)
	}
}

func TestParserVersionForEndpointUsesPythonSourceContract(t *testing.T) {
	for _, endpoint := range []endpointName{
		profileEndpoint,
		battleLogEndpoint,
		globalPlayerRankingsEndpoint,
	} {
		if got := parserVersionForEndpoint(endpoint); got != "supercell-source-parser-v2" {
			t.Fatalf("parser version for %q = %q", endpoint, got)
		}
	}
}

func startVersionTwoStore(t *testing.T, ctx context.Context) *store {
	t.Helper()
	// The embedded PostgreSQL bootstrap can take longer under race
	// instrumentation than the test-body context permits. Bootstrap with a
	// fresh context so the caller's context bounds the test body only.
	bootstrapContext, cancelBootstrap := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancelBootstrap()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(bootstrapContext, databaseURL)
	if err != nil {
		t.Fatalf("connect to version-two PostgreSQL: %v", err)
	}
	applySQLFile(t, bootstrapContext, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, bootstrapContext, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	if err := connection.Close(bootstrapContext); err != nil {
		t.Fatalf("close migration connection: %v", err)
	}
	opened, err := openStore(bootstrapContext, databaseURL, 2)
	if err != nil {
		t.Fatalf("open version-two store: %v", err)
	}
	t.Cleanup(opened.close)
	return opened
}
