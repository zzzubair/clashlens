package collector

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

func TestInteractiveGateDenialDoesNotBeginEndpointOrCallOfficialAPI(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	key := APIKey{Label: "interactive-1", Secret: "synthetic-interactive-key", Pool: interactivePool}
	fingerprint := bearerTokenFingerprint(key.Secret)
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "test:registration"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}
	if err := store.quarantineSharedCredential(ctx, fingerprint, "test:operator", "test_quarantine"); err != nil {
		t.Fatalf("quarantine shared credential: %v", err)
	}
	if _, err := store.enqueueInteractive(ctx, "live_refresh", "#2PP", time.Now().UTC(), 0, true); err != nil {
		t.Fatalf("enqueue interactive job: %v", err)
	}

	var officialCalls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		officialCalls.Add(1)
	}))
	t.Cleanup(api.Close)
	keys, err := newKeyPool([]APIKey{key}, 30, false)
	if err != nil {
		t.Fatalf("create interactive key pool: %v", err)
	}
	worker := newWorker(store, &memoryArchive{}, newTestOfficialAPIClient(t, api.URL, 1<<20), keys, workerConfig{
		owner:            "shared-gate-denial",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   0,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})

	claimed, err := worker.runOnce(ctx, interactivePool)
	if !claimed {
		t.Fatal("interactive worker did not claim the queued job")
	}
	if !errors.Is(err, errNoHealthyKey) {
		t.Fatalf("interactive worker error = %v, want errNoHealthyKey", err)
	}
	if got := officialCalls.Load(); got != 0 {
		t.Fatalf("official API calls = %d, want 0 when the shared gate denies the request", got)
	}
	var maximumRequestCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT coalesce(max(request_count), 0)
		FROM collector_endpoint_results
	`).Scan(&maximumRequestCount); err != nil {
		t.Fatalf("read endpoint request counts: %v", err)
	}
	if maximumRequestCount != 0 {
		t.Fatalf("denied gate request count = %d, want 0", maximumRequestCount)
	}
	statuses := keys.statuses(time.Now().UTC())
	if len(statuses) != 1 || statuses[0].RequestsInLastSecond != 0 {
		t.Fatalf("denied gate consumed local interactive capacity: %#v", statuses)
	}
	var permits int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM shared_api_permits`).Scan(&permits); err != nil {
		t.Fatalf("count shared permits: %v", err)
	}
	if permits != 0 {
		t.Fatalf("shared permits = %d, want 0 for a denied request", permits)
	}
}

func TestInteractiveUnknownAuthenticationResponseDoesNotQuarantineSharedCredential(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	key := APIKey{Label: "interactive-1", Secret: "synthetic-unknown-auth-key", Pool: interactivePool}
	fingerprint := bearerTokenFingerprint(key.Secret)
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "test:registration"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}
	if _, err := store.enqueueInteractive(ctx, "live_refresh", "#2PP", time.Now().UTC(), 0, true); err != nil {
		t.Fatalf("enqueue interactive job: %v", err)
	}

	var officialCalls atomic.Int32
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		officialCalls.Add(1)
		response.Header().Set("Content-Type", "application/json")
		response.WriteHeader(http.StatusForbidden)
		_, _ = response.Write([]byte(`{"reason":"accessDenied","message":"Changed provider wording"}`))
	}))
	t.Cleanup(api.Close)
	keys, err := newKeyPool([]APIKey{key}, 30, false)
	if err != nil {
		t.Fatalf("create interactive key pool: %v", err)
	}
	worker := newWorker(store, &memoryArchive{}, newTestOfficialAPIClient(t, api.URL, 1<<20), keys, workerConfig{
		owner:            "shared-gate-unknown-auth",
		leaseDuration:    time.Minute,
		collectorVersion: "test",
		maximumRetries:   0,
		retryPolicy:      newRetryPolicy(0, 0, 0),
	})

	claimed, _ := worker.runOnce(ctx, interactivePool)
	if !claimed {
		t.Fatal("interactive worker did not claim the queued job")
	}
	var state string
	if err := store.pool.QueryRow(ctx, `
		SELECT state FROM shared_api_credentials WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&state); err != nil {
		t.Fatalf("read shared credential state: %v", err)
	}
	if state != "active" {
		t.Fatalf("unknown authentication response changed shared credential state to %q, want active", state)
	}
	if got := officialCalls.Load(); got != 2 {
		t.Fatalf("official API calls = %d, want one request for each endpoint", got)
	}
}
