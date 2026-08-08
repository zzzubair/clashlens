package collector

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

type failOnceArchive struct {
	mu       sync.Mutex
	failed   bool
	delegate memoryArchive
}

func (a *failOnceArchive) store(ctx context.Context, hash string, body []byte) (string, error) {
	a.mu.Lock()
	if !a.failed {
		a.failed = true
		a.mu.Unlock()
		return "", errors.New("injected archive failure")
	}
	a.mu.Unlock()
	return a.delegate.store(ctx, hash, body)
}

func TestArchiveFailureCreatesNoObservationAndRetriesEndpoint(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, worker := newStorageTestWorker(t, ctx, databaseURL, &failOnceArchive{})

	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("first worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("first worker run did not claim work")
	}

	var observations, processingJobs, storageFailures int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count first observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count first processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE failure_category = 'archive_write_failed' AND observation_id IS NULL AND http_status = 200
	`).Scan(&storageFailures); err != nil {
		t.Fatalf("count storage failures: %v", err)
	}
	if observations != 1 || processingJobs != 1 || storageFailures != 1 {
		t.Fatalf("first outputs = %d observations, %d processing jobs, %d storage failures; want 1, 1, 1", observations, processingJobs, storageFailures)
	}

	claimed, err = worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("retry worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("retry worker run did not claim work")
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count final observations: %v", err)
	}
	if observations != 2 {
		t.Fatalf("final observation count = %d, want 2", observations)
	}
}

func TestPostgreSQLObservationTransactionFailureReusesArchiveAndCommitsOnce(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive := &memoryArchive{}
	store, worker := newStorageTestWorker(t, ctx, databaseURL, archive)

	if _, err := store.pool.Exec(ctx, `
		CREATE SEQUENCE fail_first_observation_sequence;
		CREATE FUNCTION fail_first_observation() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF nextval('fail_first_observation_sequence') = 1 THEN
				RAISE EXCEPTION 'injected observation transaction failure';
			END IF;
			RETURN NEW;
		END;
		$$;
		CREATE TRIGGER fail_first_observation_trigger
		BEFORE INSERT ON collector_observations
		FOR EACH ROW EXECUTE FUNCTION fail_first_observation();
	`); err != nil {
		t.Fatalf("install transaction failure trigger: %v", err)
	}

	claimed, err := worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("first worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("first worker run did not claim work")
	}

	var observations, databaseFailures int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count first observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE failure_category = 'database_transaction_failed' AND observation_id IS NULL
	`).Scan(&databaseFailures); err != nil {
		t.Fatalf("count database transaction failures: %v", err)
	}
	if observations != 1 || databaseFailures != 1 {
		t.Fatalf("first outputs = %d observations and %d database failures, want 1 and 1", observations, databaseFailures)
	}
	archive.mu.Lock()
	objectsAfterFailure := len(archive.objects)
	archive.mu.Unlock()
	if objectsAfterFailure != 2 {
		t.Fatalf("archive object count after transaction failure = %d, want 2", objectsAfterFailure)
	}

	claimed, err = worker.runOnce(ctx, normalPool)
	if err != nil {
		t.Fatalf("retry worker run returned an error: %v", err)
	}
	if !claimed {
		t.Fatal("retry worker run did not claim work")
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count final observations: %v", err)
	}
	if observations != 2 {
		t.Fatalf("final observation count = %d, want 2", observations)
	}
	archive.mu.Lock()
	finalObjects := len(archive.objects)
	archive.mu.Unlock()
	if finalObjects != 2 {
		t.Fatalf("final archive object count = %d, want 2", finalObjects)
	}
}

func newStorageTestWorker(
	t *testing.T,
	ctx context.Context,
	databaseURL string,
	archive archiveStore,
) (*store, *worker) {
	t.Helper()
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
	worker := newWorker(store, archive, official, keys, workerConfig{
		owner:            "test-worker",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   1,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})
	return store, worker
}
