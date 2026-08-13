package collector

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestSchedulerDoesNotScheduleGlobalRankingsWhenBetaGateIsDisabled(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	app := &application{
		config: collectorConfig{
			pollCycle:            5 * time.Minute,
			scheduleBatchSize:    10,
			enableGlobalRankings: false,
		},
		store:   store,
		metrics: newCollectorMetrics(),
		logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	now := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	if err := app.schedulerOnce(ctx, now); err != nil {
		t.Fatalf("schedulerOnce returned an error: %v", err)
	}
	var globalJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE work_type = 'global_player_rankings'
	`).Scan(&globalJobs); err != nil {
		t.Fatalf("count global rankings jobs: %v", err)
	}
	if globalJobs != 0 {
		t.Fatalf("global rankings jobs = %d, want 0 when beta gate is disabled", globalJobs)
	}
}

func TestSchedulerSchedulesGlobalRankingsWhenBetaGateIsEnabled(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	app := &application{
		config: collectorConfig{
			pollCycle:            5 * time.Minute,
			scheduleBatchSize:    10,
			enableGlobalRankings: true,
		},
		store:   store,
		metrics: newCollectorMetrics(),
		logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}

	now := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	if err := app.schedulerOnce(ctx, now); err != nil {
		t.Fatalf("schedulerOnce returned an error: %v", err)
	}
	var globalJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE work_type = 'global_player_rankings'
	`).Scan(&globalJobs); err != nil {
		t.Fatalf("count global rankings jobs: %v", err)
	}
	if globalJobs != 1 {
		t.Fatalf("global rankings jobs = %d, want 1 when beta gate is enabled", globalJobs)
	}
}

func TestStartupGuardFailsBeforeClaimWhenArchiveIsUnavailable(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, now() - interval '1 minute', 'startup-test', 'pending')
	`); err != nil {
		t.Fatalf("insert due job: %v", err)
	}
	store.close()

	archive := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		http.Error(response, "unavailable", http.StatusServiceUnavailable)
	}))
	t.Cleanup(archive.Close)
	apiRequests := 0
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		apiRequests++
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)

	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	var stdout, stderr bytes.Buffer
	err = RunCLI(ctx, []string{"run", "--once", "--role", "worker"}, func(name string) string {
		return environment[name]
	}, &stdout, &stderr)
	if err == nil || !strings.Contains(err.Error(), "startup guard") {
		t.Fatalf("RunCLI error = %v, want startup guard failure", err)
	}
	if apiRequests != 0 {
		t.Fatalf("official API request count = %d, want 0", apiRequests)
	}

	store, err = openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer store.close()
	var status string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE coalescing_key = 'startup-test'`).Scan(&status); err != nil {
		t.Fatalf("read due job status: %v", err)
	}
	if status != "pending" {
		t.Fatalf("due job status = %q, want pending", status)
	}
}

func TestWorkerDoesNotClaimWhenArchiveBecomesUnavailable(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, archiveBackend := newFakeS3Server(t)
	var apiRequests atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		apiRequests.Add(1)
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	app, err := newApplication(ctx, config, nil)
	if err != nil {
		t.Fatalf("newApplication returned an error: %v", err)
	}
	defer app.close()
	if _, err := app.store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, now() - interval '1 minute', 'admission-test', 'pending')
	`); err != nil {
		t.Fatalf("insert due job: %v", err)
	}

	archiveBackend.mu.Lock()
	archiveBackend.unavailable = true
	archiveBackend.mu.Unlock()
	claimed, err := app.configuredWorker("admission-test").runOnce(ctx, interactivePool)
	if err == nil || !strings.Contains(err.Error(), "archive") {
		t.Fatalf("worker error = %v, want archive readiness error", err)
	}
	if claimed {
		t.Fatal("worker claimed a job while archive readiness failed")
	}
	if requests := apiRequests.Load(); requests != 0 {
		t.Fatalf("official API request count = %d, want 0", requests)
	}

	var status string
	var attempts int
	if err := app.store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE coalescing_key = 'admission-test'`).Scan(&status); err != nil {
		t.Fatalf("read due job status: %v", err)
	}
	if err := app.store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_attempts`).Scan(&attempts); err != nil {
		t.Fatalf("count attempts: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("job status = %q and attempts = %d, want pending and 0", status, attempts)
	}
}

func TestWorkerDoesNotClaimWhenPostgreSQLBecomesUnavailable(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, _ := newFakeS3Server(t)
	var apiRequests atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		apiRequests.Add(1)
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	app, err := newApplication(ctx, config, nil)
	if err != nil {
		t.Fatalf("newApplication returned an error: %v", err)
	}
	if _, err := app.store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, now() - interval '1 minute', 'postgres-admission-test', 'pending')
	`); err != nil {
		t.Fatalf("insert due job: %v", err)
	}

	app.store.close()
	claimed, err := app.configuredWorker("postgres-admission-test").runOnce(ctx, interactivePool)
	if err == nil || !strings.Contains(err.Error(), "postgresql") {
		t.Fatalf("worker error = %v, want PostgreSQL readiness error", err)
	}
	if claimed {
		t.Fatal("worker claimed a job while PostgreSQL readiness failed")
	}
	if requests := apiRequests.Load(); requests != 0 {
		t.Fatalf("official API request count = %d, want 0", requests)
	}

	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer store.close()
	var status string
	var attempts int
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE coalescing_key = 'postgres-admission-test'`).Scan(&status); err != nil {
		t.Fatalf("read due job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_attempts`).Scan(&attempts); err != nil {
		t.Fatalf("count attempts: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("job status = %q and attempts = %d, want pending and 0", status, attempts)
	}
}

func TestReadinessReportsDependencyAndKeyPoolComponentsSeparately(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, _ := newFakeS3Server(t)
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	app, err := newApplication(ctx, config, nil)
	if err != nil {
		t.Fatalf("newApplication returned an error: %v", err)
	}
	defer app.close()
	if err := app.keys.quarantine("normal-1"); err != nil {
		t.Fatalf("quarantine normal key: %v", err)
	}

	request := httptest.NewRequest(http.MethodGet, "/readyz", nil)
	response := httptest.NewRecorder()
	app.operationalHandler().ServeHTTP(response, request)
	if response.Code != http.StatusServiceUnavailable {
		t.Fatalf("readiness status = %d, want %d", response.Code, http.StatusServiceUnavailable)
	}
	var report struct {
		Ready      bool              `json:"ready"`
		Components map[string]string `json:"components"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &report); err != nil {
		t.Fatalf("decode readiness response: %v", err)
	}
	if report.Ready {
		t.Fatal("readiness report is ready with no healthy normal key")
	}
	want := map[string]string{
		"postgresql":           "ready",
		"archive":              "ready",
		"normal_key_pool":      "not_ready",
		"interactive_key_pool": "ready",
	}
	for component, state := range want {
		if report.Components[component] != state {
			t.Fatalf("component %q = %q, want %q", component, report.Components[component], state)
		}
	}
}

func TestMaintenanceListFailedNeedsOnlyPostgreSQLConfiguration(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	environment := map[string]string{
		"CLASHLENS_DATABASE_URL": databaseURL,
	}
	var stdout, stderr bytes.Buffer

	err := RunCLI(
		ctx,
		[]string{"maintenance", "list-failed"},
		func(name string) string { return environment[name] },
		&stdout,
		&stderr,
	)
	if err != nil {
		t.Fatalf("maintenance list-failed returned an error: %v; stderr = %q", err, stderr.String())
	}
	if stdout.Len() != 0 {
		t.Fatalf("maintenance list-failed output = %q, want no failed work", stdout.String())
	}
}

func TestStartupGuardRequiresArchiveWriteCapability(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, backend := newFakeS3Server(t)
	backend.mu.Lock()
	backend.rejectWrites = true
	backend.mu.Unlock()
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	var stdout, stderr bytes.Buffer

	err := RunCLI(
		ctx,
		[]string{"run", "--once", "--role", "worker"},
		func(name string) string { return environment[name] },
		&stdout,
		&stderr,
	)
	if err == nil || !strings.Contains(err.Error(), "archive write readiness") {
		t.Fatalf("RunCLI error = %v, want archive write readiness error", err)
	}
}

func TestApplicationRequiredTrafficGateFailsClosedBeforeMigration(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, _ := newFakeS3Server(t)
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	environment["CLASHLENS_SCHEMA_VERSION"] = "2"
	environment["CLASHLENS_SHARED_TRAFFIC_GATE_MODE"] = "required"
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}

	app, err := newApplication(ctx, config, nil)
	if app != nil {
		app.close()
		t.Fatal("newApplication returned an app before migration")
	}
	if err == nil || !strings.Contains(err.Error(), "required shared traffic gate needs contract version 2") {
		t.Fatalf("newApplication error = %v, want required traffic-gate startup rejection", err)
	}
}

func TestNewApplicationBindsConfiguredCollectorDatabasePoolSize(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive, _ := newFakeS3Server(t)
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	environment["CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE"] = "48"
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}

	app, err := newApplication(ctx, config, nil)
	if err != nil {
		t.Fatalf("newApplication returned an error: %v", err)
	}
	defer app.close()
	if maximum := app.store.pool.Config().MaxConns; maximum != 48 {
		t.Fatalf("production store pool Config().MaxConns = %d, want the configured 48", maximum)
	}
}

func runtimeTestEnvironment(databaseURL, archiveURL, apiURL string) map[string]string {
	return map[string]string{
		"CLASHLENS_DATABASE_URL":               databaseURL,
		"CLASHLENS_ARCHIVE_ENDPOINT":           strings.TrimPrefix(archiveURL, "http://"),
		"CLASHLENS_ARCHIVE_SECURE":             "false",
		"CLASHLENS_ARCHIVE_BUCKET":             "evidence",
		"CLASHLENS_ARCHIVE_ACCESS_KEY":         "archive-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY":         "archive-secret",
		"CLASHLENS_OFFICIAL_API_ORIGIN":        apiURL,
		"CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN": "true",
		"CLASHLENS_ALLOW_REDUCED_KEY_POOLS":    "true",
		"CLASHLENS_NORMAL_API_KEYS":            "normal-1=normal-secret",
		"CLASHLENS_INTERACTIVE_API_KEYS":       "interactive-1=interactive-secret",
	}
}
