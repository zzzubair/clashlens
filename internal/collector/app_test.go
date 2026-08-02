package collector

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestStartupGuardFailsBeforeClaimWhenArchiveIsUnavailable(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
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

func TestReadinessReportsDependencyAndKeyPoolComponentsSeparately(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
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
