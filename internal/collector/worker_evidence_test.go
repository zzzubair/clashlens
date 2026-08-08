package collector

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestCompletedResponsesRemainExactEvidenceRegardlessOfStatusOrJSONValidity(t *testing.T) {
	for _, test := range []struct {
		name        string
		status      int
		profileBody []byte
	}{
		{name: "not found", status: http.StatusNotFound, profileBody: []byte(" {\n\"reason\":\"notFound\"}\n")},
		{name: "malformed success", status: http.StatusOK, profileBody: []byte("{not-json\x00\n")},
	} {
		t.Run(test.name, func(t *testing.T) {
			databaseURL := startContractDatabase(t)
			ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
			defer cancel()
			archive := &memoryArchive{}
			store, worker := newEvidenceTestWorker(t, ctx, databaseURL, archive, func(response http.ResponseWriter, request *http.Request) {
				response.Header().Set("Content-Type", "application/json")
				if request.URL.Path == "/v1/players/#2PP" {
					response.WriteHeader(test.status)
					_, _ = response.Write(test.profileBody)
					return
				}
				_, _ = response.Write([]byte(`{"items":[]}`))
			})
			if claimed, err := worker.runOnce(ctx, normalPool); err != nil || !claimed {
				t.Fatalf("worker run = claimed %v, error %v", claimed, err)
			}

			digest := sha256.Sum256(test.profileBody)
			hash := hex.EncodeToString(digest[:])
			archive.mu.Lock()
			archivedBody := append([]byte(nil), archive.objects[hash]...)
			archive.mu.Unlock()
			if string(archivedBody) != string(test.profileBody) {
				t.Fatalf("archived profile body = %q, want exact %q", archivedBody, test.profileBody)
			}
			var status int
			var storedHash string
			if err := store.pool.QueryRow(ctx, `
				SELECT http_status, response_hash
				FROM collector_observations
				WHERE endpoint = 'profile'
			`).Scan(&status, &storedHash); err != nil {
				t.Fatalf("read profile observation: %v", err)
			}
			if status != test.status || storedHash != hash {
				t.Fatalf("profile observation = status %d hash %q, want %d and %q", status, storedHash, test.status, hash)
			}
		})
	}
}

func TestIdenticalEndpointBodiesReuseOneArchiveObjectButKeepDistinctOccurrences(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	archive := &memoryArchive{}
	identicalBody := []byte("{\"same\":true}\n")
	store, worker := newEvidenceTestWorker(t, ctx, databaseURL, archive, func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write(identicalBody)
	})
	if claimed, err := worker.runOnce(ctx, normalPool); err != nil || !claimed {
		t.Fatalf("worker run = claimed %v, error %v", claimed, err)
	}

	archive.mu.Lock()
	objects := len(archive.objects)
	archive.mu.Unlock()
	var observations, endpoints int
	if err := store.pool.QueryRow(context.Background(), `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(context.Background(), `
		SELECT count(DISTINCT endpoint) FROM collector_observations
	`).Scan(&endpoints); err != nil {
		t.Fatalf("count observed endpoints: %v", err)
	}
	if objects != 1 || observations != 2 || endpoints != 2 {
		t.Fatalf("dedup outputs = %d archive objects, %d observations, %d endpoints; want 1, 2, 2", objects, observations, endpoints)
	}
}

func newEvidenceTestWorker(
	t *testing.T,
	ctx context.Context,
	databaseURL string,
	archive archiveStore,
	handler http.HandlerFunc,
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
	apiServer := httptest.NewServer(handler)
	t.Cleanup(apiServer.Close)
	official, err := newOfficialAPIClient(officialAPIConfig{
		origin:                apiServer.URL,
		allowInsecureTestHTTP: true,
		connectionTimeout:     time.Second,
		responseHeaderTimeout: time.Second,
		totalTimeout:          3 * time.Second,
		maximumResponseBytes:  1 << 20,
	})
	if err != nil {
		t.Fatalf("newOfficialAPIClient returned an error: %v", err)
	}
	keys, err := newKeyPool([]APIKey{{Label: "normal-a", Secret: "secret-a", Pool: normalPool}}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}
	return store, newWorker(store, archive, official, keys, workerConfig{
		owner:            "evidence-test",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   1,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})
}
