package collector

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

func TestWorkerArchivesRetryableHTTPResponsesAndRetriesWithBackoff(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100); err != nil {
		t.Fatalf("schedule due player: %v", err)
	}

	var requestMu sync.Mutex
	requestCounts := map[string]int{}
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestMu.Lock()
		requestCounts[request.URL.Path]++
		count := requestCounts[request.URL.Path]
		requestMu.Unlock()

		response.Header().Set("Content-Type", "application/json")
		if count == 1 && request.URL.Path == "/v1/players/#2PP" {
			response.Header().Set("Retry-After", "0")
			response.WriteHeader(http.StatusTooManyRequests)
			_, _ = io.WriteString(response, `{"reason":"rateLimit"}`)
			return
		}
		if count == 1 && request.URL.Path == "/v1/players/#2PP/battlelog" {
			response.WriteHeader(http.StatusServiceUnavailable)
			_, _ = io.WriteString(response, `{"reason":"maintenance"}`)
			return
		}
		if request.URL.Path == "/v1/players/#2PP/battlelog" {
			_, _ = io.WriteString(response, `{"items":[]}`)
			return
		}
		_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
	}))
	t.Cleanup(api.Close)

	keys, err := newKeyPool([]APIKey{{Label: "normal-a", Secret: "secret-a", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}
	official, err := newOfficialAPIClient(officialAPIConfig{
		origin:                api.URL,
		allowInsecureTestHTTP: true,
		connectionTimeout:     time.Second,
		responseHeaderTimeout: time.Second,
		totalTimeout:          3 * time.Second,
		maximumResponseBytes:  1 << 20,
	})
	if err != nil {
		t.Fatalf("newOfficialAPIClient returned an error: %v", err)
	}
	worker := newWorker(store, &memoryArchive{}, official, keys, workerConfig{
		owner:            "test-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   2,
		retryPolicy:      newRetryPolicy(time.Millisecond, time.Millisecond, 0),
	})

	for run := 1; run <= 3; run++ {
		if run > 1 {
			time.Sleep(2 * time.Millisecond)
		}
		claimed, err := worker.runOnce(ctx, normalPool)
		if err != nil {
			t.Fatalf("worker run %d returned an error: %v", run, err)
		}
		if !claimed {
			t.Fatalf("worker run %d did not claim work", run)
		}
	}

	var observations, processingJobs, retryableObservations int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_observations WHERE http_status IN (429, 503)
	`).Scan(&retryableObservations); err != nil {
		t.Fatalf("count retryable observations: %v", err)
	}
	if observations != 4 || processingJobs != 4 || retryableObservations != 2 {
		t.Fatalf("outputs = %d observations, %d processing jobs, %d retryable responses; want 4, 4, 2", observations, processingJobs, retryableObservations)
	}

	requestMu.Lock()
	profileRequests := requestCounts["/v1/players/#2PP"]
	battleLogRequests := requestCounts["/v1/players/#2PP/battlelog"]
	requestMu.Unlock()
	if profileRequests != 2 || battleLogRequests != 2 {
		t.Fatalf("request counts = profile %d, battle log %d; want 2 and 2", profileRequests, battleLogRequests)
	}

	var attemptStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read attempt status: %v", err)
	}
	if attemptStatus != "complete" {
		t.Fatalf("attempt status = %q, want complete", attemptStatus)
	}
}
