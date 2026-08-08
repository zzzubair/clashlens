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

func TestLateWorkerAfterLeaseReclaimCannotDuplicateObservation(t *testing.T) {
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
	firstRequestsArrived := make(chan struct{}, 2)
	releaseFirstRequests := make(chan struct{})
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestMu.Lock()
		requestCounts[request.URL.Path]++
		count := requestCounts[request.URL.Path]
		requestMu.Unlock()
		if count == 1 {
			firstRequestsArrived <- struct{}{}
			<-releaseFirstRequests
		}
		response.Header().Set("Content-Type", "application/json")
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
		responseHeaderTimeout: 3 * time.Second,
		totalTimeout:          5 * time.Second,
		maximumResponseBytes:  1 << 20,
	})
	if err != nil {
		t.Fatalf("newOfficialAPIClient returned an error: %v", err)
	}
	archive := &memoryArchive{}
	firstWorker := newWorker(store, archive, official, keys, workerConfig{
		owner:               "first-worker",
		leaseDuration:       40 * time.Millisecond,
		collectorVersion:    "test",
		maximumRetries:      1,
		retryPolicy:         newRetryPolicy(0, 0, 0),
		disableLeaseRenewal: true,
	})
	secondWorker := newWorker(store, archive, official, keys, workerConfig{
		owner:            "second-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   1,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})

	firstResult := make(chan error, 1)
	go func() {
		_, runError := firstWorker.runOnce(ctx, normalPool)
		firstResult <- runError
	}()
	for range 2 {
		select {
		case <-firstRequestsArrived:
		case <-ctx.Done():
			t.Fatal("first worker did not start both requests")
		}
	}
	time.Sleep(60 * time.Millisecond)

	claimed, err := secondWorker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("second worker returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("second worker did not reclaim the expired lease")
	}
	close(releaseFirstRequests)
	if err := <-firstResult; err == nil {
		t.Fatal("late first worker returned no lease-loss error")
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
	var attemptStatus, jobStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read attempt status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE work_type = 'regular_poll'`).Scan(&jobStatus); err != nil {
		t.Fatalf("read job status: %v", err)
	}
	if attemptStatus != "complete" || jobStatus != "complete" {
		t.Fatalf("final statuses = attempt %q and job %q, want complete and complete", attemptStatus, jobStatus)
	}
}
