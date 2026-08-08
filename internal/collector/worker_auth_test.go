package collector

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestWorkerQuarantinesOnlyUnauthorizedKeyAndRetriesWithHealthyKey(t *testing.T) {
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

	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		if request.Header.Get("Authorization") == "Bearer secret-a" {
			response.WriteHeader(http.StatusUnauthorized)
			_, _ = io.WriteString(response, `{"reason":"accessDenied"}`)
			return
		}
		if request.URL.Path == "/v1/players/#2PP/battlelog" {
			_, _ = io.WriteString(response, `{"items":[]}`)
			return
		}
		_, _ = io.WriteString(response, `{"tag":"#2PP"}`)
	}))
	t.Cleanup(api.Close)

	keys, err := newKeyPool([]APIKey{
		{Label: "normal-a", Secret: "secret-a", Pool: normalPool},
		{Label: "normal-b", Secret: "secret-b", Pool: normalPool},
	}, 30, false)
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
		retryPolicy:      newRetryPolicy(time.Millisecond, time.Millisecond, 0),
	})

	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("worker run did not claim work")
	}

	var observations, unauthorized, successful, processingJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations WHERE http_status = 401 AND key_label = 'normal-a'`).Scan(&unauthorized); err != nil {
		t.Fatalf("count unauthorized observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations WHERE http_status = 200 AND key_label = 'normal-b'`).Scan(&successful); err != nil {
		t.Fatalf("count healthy-key observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count processing jobs: %v", err)
	}
	if observations != 3 || unauthorized != 1 || successful != 2 || processingJobs != 3 {
		t.Fatalf("outputs = %d observations, %d unauthorized, %d successful, %d processing; want 3, 1, 2, 3", observations, unauthorized, successful, processingJobs)
	}

	var attemptStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts LIMIT 1`).Scan(&attemptStatus); err != nil {
		t.Fatalf("read attempt status: %v", err)
	}
	if attemptStatus != "complete" {
		t.Fatalf("attempt status = %q, want complete", attemptStatus)
	}
}

func TestWorkerQuarantinesUnauthorizedKeyWhenObservationTransactionFails(t *testing.T) {
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
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('reset_profile', '#2PP', 'normal', 400, $1, 'auth-database-failure', 'pending')
	`, now); err != nil {
		t.Fatalf("insert reset job: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		CREATE FUNCTION fail_auth_observation() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			RAISE EXCEPTION 'injected auth observation transaction failure';
		END;
		$$;
		CREATE TRIGGER fail_auth_observation_trigger
		BEFORE INSERT ON collector_observations
		FOR EACH ROW EXECUTE FUNCTION fail_auth_observation();
	`); err != nil {
		t.Fatalf("install transaction failure trigger: %v", err)
	}

	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		response.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(response, `{"reason":"accessDenied"}`)
	}))
	t.Cleanup(api.Close)
	keys, err := newKeyPool([]APIKey{
		{Label: "normal-a", Secret: "secret-a", Pool: normalPool},
	}, 30, false)
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
		owner:            "auth-database-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   1,
		retryPolicy:      newRetryPolicy(time.Millisecond, time.Millisecond, 0),
	})
	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("worker run did not claim work")
	}

	statuses := keys.statuses(now)
	if len(statuses) != 1 || !statuses[0].Quarantined {
		t.Fatalf("key statuses = %#v, want unauthorized key quarantined", statuses)
	}
}
