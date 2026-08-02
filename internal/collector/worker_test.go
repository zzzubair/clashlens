package collector

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

type memoryArchive struct {
	mu      sync.Mutex
	objects map[string][]byte
}

func (a *memoryArchive) store(_ context.Context, hash string, body []byte) (string, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.objects == nil {
		a.objects = make(map[string][]byte)
	}
	if existing, ok := a.objects[hash]; ok && string(existing) != string(body) {
		return "", fmt.Errorf("hash collision for %s", hash)
	}
	a.objects[hash] = append([]byte(nil), body...)
	return "memory://sha256/" + hash, nil
}

func TestWorkerCollectsProfileAndBattleLogConcurrentlyAndCommitsEvidence(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
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

	arrived := make(chan struct{}, 2)
	release := make(chan struct{})
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		arrived <- struct{}{}
		<-release
		response.Header().Set("Content-Type", "application/json")
		switch request.URL.Path {
		case "/v1/players/#2PP":
			_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
		case "/v1/players/#2PP/battlelog":
			_, _ = io.WriteString(response, `{"items":[]}`)
		default:
			http.NotFound(response, request)
		}
	}))
	t.Cleanup(api.Close)

	keys, err := newKeyPool([]APIKey{{Label: "normal-a", Secret: "not-a-real-key", Pool: normalPool}}, 30, false)
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
	archive := &memoryArchive{}
	worker := newWorker(store, archive, official, keys, workerConfig{
		owner:            "test-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   0,
	})

	done := make(chan error, 1)
	go func() {
		_, err := worker.runOnce(ctx, normalPool)
		done <- err
	}()
	for range 2 {
		select {
		case <-arrived:
		case <-ctx.Done():
			t.Fatal("profile and battle-log requests did not overlap")
		}
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatalf("worker run returned an error: %v", err)
	}

	var observations, processingJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count processing jobs: %v", err)
	}
	if observations != 2 || processingJobs != 2 {
		t.Fatalf("durable outputs = %d observations and %d processing jobs, want 2 and 2", observations, processingJobs)
	}

	for _, body := range [][]byte{[]byte(`{"tag":"#2PP"}`), []byte(`{"items":[]}`)} {
		digest := sha256.Sum256(body)
		hash := hex.EncodeToString(digest[:])
		archive.mu.Lock()
		stored := append([]byte(nil), archive.objects[hash]...)
		archive.mu.Unlock()
		if string(stored) != string(body) {
			t.Fatalf("archive body for %s = %q, want %q", hash, stored, body)
		}
	}

	var attemptStatus, jobStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read attempt status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs LIMIT 1`).Scan(&jobStatus); err != nil {
		t.Fatalf("read job status: %v", err)
	}
	if attemptStatus != "complete" || jobStatus != "complete" {
		t.Fatalf("attempt status = %q and job status = %q, want complete and complete", attemptStatus, jobStatus)
	}
}

func TestWorkerRetainsSuccessfulProfileAndRetriesOnlyFailedBattleLog(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
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
	failBattleLog := true
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestMu.Lock()
		requestCounts[request.URL.Path]++
		shouldFail := request.URL.Path == "/v1/players/#2PP/battlelog" && failBattleLog
		if shouldFail {
			failBattleLog = false
		}
		requestMu.Unlock()

		if shouldFail {
			response.Header().Set("Content-Length", "100")
			response.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(response, `{`)
			if flusher, ok := response.(http.Flusher); ok {
				flusher.Flush()
			}
			hijacker, ok := response.(http.Hijacker)
			if !ok {
				t.Error("fake API does not support connection hijacking")
				return
			}
			connection, _, err := hijacker.Hijack()
			if err != nil {
				t.Errorf("hijack fake API connection: %v", err)
				return
			}
			_ = connection.Close()
			return
		}

		response.Header().Set("Content-Type", "application/json")
		if request.URL.Path == "/v1/players/#2PP/battlelog" {
			_, _ = io.WriteString(response, `{"items":[]}`)
			return
		}
		_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
	}))
	t.Cleanup(api.Close)

	keys, err := newKeyPool([]APIKey{{Label: "normal-a", Secret: "not-a-real-key", Pool: normalPool}}, 30, false)
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
		maximumRetries:   1,
	})

	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("first worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("first worker run did not claim the regular job")
	}

	var observations, processingJobs, transportFailures int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count first observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count first processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_transport_failures`).Scan(&transportFailures); err != nil {
		t.Fatalf("count transport failures: %v", err)
	}
	if observations != 1 || processingJobs != 1 || transportFailures != 1 {
		t.Fatalf("first durable outputs = %d observations, %d processing jobs, %d failures; want 1, 1, 1", observations, processingJobs, transportFailures)
	}

	var attemptStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read incomplete attempt: %v", err)
	}
	if attemptStatus != "incomplete" {
		t.Fatalf("attempt status after transport failure = %q, want incomplete", attemptStatus)
	}

	claimed, err = worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("retry worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("retry worker run did not claim endpoint retry")
	}

	requestMu.Lock()
	profileRequests := requestCounts["/v1/players/#2PP"]
	battleLogRequests := requestCounts["/v1/players/#2PP/battlelog"]
	requestMu.Unlock()
	if profileRequests != 1 || battleLogRequests != 2 {
		t.Fatalf("request counts = profile %d, battle log %d; want 1 and 2", profileRequests, battleLogRequests)
	}

	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read completed attempt: %v", err)
	}
	if attemptStatus != "complete" {
		t.Fatalf("attempt status after retry = %q, want complete", attemptStatus)
	}
}
