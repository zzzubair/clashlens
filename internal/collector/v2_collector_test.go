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

func TestVersionTwoGlobalRankingsScheduleUsesImmutableCycleIntent(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	boundary := time.Date(2026, time.August, 4, 5, 0, 0, 0, time.UTC)
	mutations := []string{
		"status = 'complete'",
		"status = 'failed'",
		"status = 'cancelled'",
		"status = 'waiting_retry', due_at = due_at + interval '2 minutes'",
		"status = 'pending', due_at = due_at + interval '1 hour'",
	}
	for index, mutation := range mutations {
		cycle := boundary.Add(time.Duration(index) * 5 * time.Minute)
		created, err := store.scheduleGlobalRankings(ctx, cycle, 5*time.Minute)
		if err != nil || !created {
			t.Fatalf("schedule cycle %d: created=%v err=%v", index, created, err)
		}
		if _, err := store.pool.Exec(ctx, `UPDATE collector_jobs SET `+mutation+`
			WHERE coalescing_key = $1`, "global-player-rankings:"+cycle.Format(time.RFC3339)); err != nil {
			t.Fatalf("mutate cycle %d: %v", index, err)
		}
		created, err = store.scheduleGlobalRankings(ctx, cycle.Add(4*time.Minute), 5*time.Minute)
		if err != nil || created {
			t.Fatalf("reschedule consumed cycle %d: created=%v err=%v", index, created, err)
		}
	}
	nextCycle := boundary.Add(time.Duration(len(mutations)) * 5 * time.Minute)
	created, err := store.scheduleGlobalRankings(ctx, nextCycle, 5*time.Minute)
	if err != nil || !created {
		t.Fatalf("schedule next cycle: created=%v err=%v", created, err)
	}

	var jobs, intents, boundaryPriority int
	if err := store.pool.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM collector_jobs WHERE work_type = 'global_player_rankings'),
		       (SELECT count(*) FROM global_rankings_intents),
		       (SELECT priority FROM collector_jobs WHERE coalescing_key = $1)
	`, "global-player-rankings:"+boundary.Format(time.RFC3339)).Scan(&jobs, &intents, &boundaryPriority); err != nil {
		t.Fatalf("read global rankings schedule: %v", err)
	}
	if jobs != 6 || intents != 6 || boundaryPriority != 400 {
		t.Fatalf("global rankings jobs/intents/boundary priority = %d/%d/%d, want 6/6/400", jobs, intents, boundaryPriority)
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

func TestDiscoveryProfileUsesOnlyNormalProfileWork(t *testing.T) {
	ctx := context.Background()
	store, databaseURL := startVersionTwoStoreWithURL(t, ctx)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect for discovery migration: %v", err)
	}
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0007_player_discovery.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0007_player_discovery.sql"))
	defer connection.Close(ctx)
	var selectorIndex bool
	if err := connection.QueryRow(ctx, `
		SELECT to_regclass(current_schema() || '.ranked_day_versions_daily_selector') IS NOT NULL
	`).Scan(&selectorIndex); err != nil || !selectorIndex {
		t.Fatalf("daily selector index after migration reapply: exists=%v err=%v", selectorIndex, err)
	}
	var unknownID, eligibleID int64
	if err := store.pool.QueryRow(ctx, `
		WITH inserted AS (
			INSERT INTO players (normalized_tag, active, eligibility_state)
			VALUES ('#2PP', false, 'unknown'), ('#2PQ', true, 'eligible')
			RETURNING id, normalized_tag
		)
		SELECT max(id) FILTER (WHERE normalized_tag = '#2PP'),
		       max(id) FILTER (WHERE normalized_tag = '#2PQ') FROM inserted
	`).Scan(&unknownID, &eligibleID); err != nil {
		t.Fatalf("insert discovered players: %v", err)
	}
	var created, repeated int
	if err := store.pool.QueryRow(ctx,
		`SELECT clashlens_enqueue_discovery_profiles($1::bigint[])`,
		[]int64{unknownID, unknownID, eligibleID},
	).Scan(&created); err != nil {
		t.Fatalf("enqueue discovery profile work: %v", err)
	}
	if err := store.pool.QueryRow(ctx,
		`SELECT clashlens_enqueue_discovery_profiles($1::bigint[])`, []int64{unknownID},
	).Scan(&repeated); err != nil {
		t.Fatalf("repeat discovery profile work: %v", err)
	}
	if created != 1 || repeated != 0 {
		t.Fatalf("discovery jobs created = %d then %d, want 1 then 0", created, repeated)
	}
	var workerExecute, apiExecute, collectorExecute, workerInsert, apiTableRead, apiSelectorRead bool
	if err := store.pool.QueryRow(ctx, `
		SELECT
			has_function_privilege('clashlens_python_worker', 'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
			has_function_privilege('clashlens_python_api', 'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
			has_function_privilege('clashlens_collector', 'clashlens_enqueue_discovery_profiles(bigint[])', 'EXECUTE'),
			has_table_privilege('clashlens_python_worker', 'collector_jobs', 'INSERT'),
			has_table_privilege('clashlens_python_api', 'ranked_day_versions', 'SELECT'),
			bool_and(has_column_privilege('clashlens_python_api', 'ranked_day_versions', column_name, 'SELECT'))
		FROM unnest(ARRAY['id', 'ranked_day_end', 'official_season_id', 'season_day_number']) AS column_name
		GROUP BY 1, 2, 3, 4, 5
	`).Scan(&workerExecute, &apiExecute, &collectorExecute, &workerInsert, &apiTableRead, &apiSelectorRead); err != nil {
		t.Fatalf("read discovery privileges: %v", err)
	}
	if !workerExecute || apiExecute || collectorExecute || workerInsert || apiTableRead || !apiSelectorRead {
		t.Fatalf("unexpected discovery privileges: worker=%v api=%v collector=%v insert=%v table=%v selector=%v",
			workerExecute, apiExecute, collectorExecute, workerInsert, apiTableRead, apiSelectorRead)
	}
	if _, err := connection.Exec(ctx, `BEGIN; SET LOCAL ROLE clashlens_python_api`); err != nil {
		t.Fatalf("assume API role: %v", err)
	}
	if _, err := connection.Exec(ctx, `SELECT id, ranked_day_end, official_season_id, season_day_number FROM ranked_day_versions LIMIT 0`); err != nil {
		t.Fatalf("API read daily selector columns: %v", err)
	}
	expectInsufficientPrivilege(t, ctx, connection, `SELECT state FROM ranked_day_versions LIMIT 0`, "API reading unrelated ranked-day state")
	if _, err := connection.Exec(ctx, `ROLLBACK`); err != nil {
		t.Fatalf("finish API role probe: %v", err)
	}
	requests := make([]string, 0, 2)
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requests = append(requests, request.URL.Path)
		response.Header().Set("Content-Type", "application/json")
		if len(requests) == 1 {
			response.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(response, `{"reason":"retry"}`)
			return
		}
		_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
	}))
	t.Cleanup(api.Close)
	official := newTestOfficialAPIClient(t, api.URL, 1<<20)
	keys, err := newKeyPool([]APIKey{{Label: "normal-1", Secret: "normal-secret", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("create discovery key pool: %v", err)
	}
	worker := newWorker(store, &memoryArchive{}, official, keys, workerConfig{
		owner: "discovery-worker", leaseDuration: time.Minute,
		collectorVersion: "collector-v2", maximumRetries: 1,
		retryPolicy: newRetryPolicy(0, 0, 0),
	})
	for run := 0; run < 2; run++ {
		claimed, runErr := worker.runOnce(ctx, normalPool)
		if runErr != nil || !claimed {
			t.Fatalf("discovery worker run %d: claimed=%v err=%v", run+1, claimed, runErr)
		}
	}
	if len(requests) != 2 || requests[0] != "/v1/players/#2PP" || requests[1] != requests[0] {
		t.Fatalf("discovery requests = %v, want two profile requests", requests)
	}
	var retryEndpoints, battleEndpoints int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE job.work_type = 'endpoint_retry' AND job.required_endpoint = 'profile'),
		       count(*) FILTER (WHERE result.endpoint = 'battle_log')
		FROM collector_jobs AS job
		LEFT JOIN collector_attempts AS attempt ON attempt.job_id = job.id
		LEFT JOIN collector_endpoint_results AS result ON result.attempt_id = attempt.id
	`).Scan(&retryEndpoints, &battleEndpoints); err != nil {
		t.Fatalf("read discovery retry evidence: %v", err)
	}
	if retryEndpoints != 1 || battleEndpoints != 0 {
		t.Fatalf("discovery retry/profile evidence = %d retries, %d battle endpoints", retryEndpoints, battleEndpoints)
	}
}

func TestDiscoveryMigrationSeedsDuplicateTerminalGlobalCyclesIdempotently(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect for migration seed: %v", err)
	}
	defer connection.Close(ctx)
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, capacity_pool, priority, due_at, coalescing_key,
			required_endpoint, status
		) VALUES
			('global_player_rankings', 'global', 'normal', 300, '2026-08-04T12:05:00Z',
			 'global-player-rankings:2026-08-04T12:05:00Z', 'global_player_rankings', 'complete'),
			('global_player_rankings', 'global', 'normal', 300, '2026-08-04T13:05:00Z',
			 'global-player-rankings:2026-08-04T12:05:00Z', 'global_player_rankings', 'failed')
	`); err != nil {
		t.Fatalf("insert duplicate terminal jobs: %v", err)
	}
	migration := filepath.Join("..", "..", "deploy", "migrations", "0007_player_discovery.sql")
	applySQLFile(t, ctx, connection, migration)
	applySQLFile(t, ctx, connection, migration)
	var jobs, intents int
	if err := connection.QueryRow(ctx, `
		SELECT (SELECT count(*) FROM collector_jobs WHERE work_type = 'global_player_rankings'),
		       (SELECT count(*) FROM global_rankings_intents)
	`).Scan(&jobs, &intents); err != nil {
		t.Fatalf("read migrated cycles: %v", err)
	}
	if jobs != 2 || intents != 1 {
		t.Fatalf("migration retained %d jobs and seeded %d intents, want 2 and 1", jobs, intents)
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
	applySQLFile(t, bootstrapContext, connection, filepath.Join("..", "..", "deploy", "migrations", "0007_player_discovery.sql"))
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
