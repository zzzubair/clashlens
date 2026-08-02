package collector

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestGracefulShutdownReleasesUnfinishedLeaseForReclaim(t *testing.T) {
	databaseURL := startContractDatabase(t)
	s3Server, _ := newFakeS3Server(t)
	requestsStarted := make(chan struct{}, 2)
	api := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
		requestsStarted <- struct{}{}
		<-request.Context().Done()
	}))
	t.Cleanup(api.Close)

	environment := runtimeTestEnvironment(databaseURL, s3Server.URL, api.URL)
	environment["CLASHLENS_LEASE_DURATION"] = "5s"
	config, err := loadConfig(func(name string) string { return environment[name] })
	if err != nil {
		t.Fatalf("loadConfig returned an error: %v", err)
	}
	appContext, cancelApp := context.WithCancel(context.Background())
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	app, err := newApplication(appContext, config, logger)
	if err != nil {
		cancelApp()
		t.Fatalf("newApplication returned an error: %v", err)
	}
	defer app.close()
	intent, err := app.store.enqueueInteractive(
		appContext,
		"live_refresh",
		"#2PP",
		time.Now().UTC(),
		config.interactiveCooldown,
		false,
	)
	if err != nil {
		cancelApp()
		t.Fatalf("enqueue interactive work: %v", err)
	}

	runResult := make(chan error, 1)
	go func() { runResult <- app.run(appContext, "worker") }()
	for range 2 {
		select {
		case <-requestsStarted:
		case <-time.After(5 * time.Second):
			cancelApp()
			t.Fatal("worker did not start both endpoint requests")
		}
	}
	cancelApp()
	select {
	case err := <-runResult:
		if err != nil {
			t.Fatalf("application shutdown returned an error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("application did not stop within the shutdown bound")
	}

	checkContext, cancelCheck := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelCheck()
	var status string
	var owner, token *string
	if err := app.store.pool.QueryRow(checkContext, `
		SELECT status, lease_owner, lease_token
		FROM collector_jobs
		WHERE id = $1
	`, intent.jobID).Scan(&status, &owner, &token); err != nil {
		t.Fatalf("read shutdown job: %v", err)
	}
	if status != "pending" || owner != nil || token != nil {
		t.Fatalf("shutdown job = status %q owner %v token %v, want pending without lease", status, owner, token)
	}
	var observations int
	if err := app.store.pool.QueryRow(checkContext, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count shutdown observations: %v", err)
	}
	if observations != 0 {
		t.Fatalf("shutdown observation count = %d, want 0", observations)
	}

	claimed, err := app.store.claimNext(
		checkContext,
		"reclaimer",
		interactivePool,
		time.Now().UTC(),
		time.Minute,
		"reclaim-token",
	)
	if err != nil {
		t.Fatalf("reclaim shutdown work: %v", err)
	}
	if claimed == nil || claimed.id != intent.jobID {
		t.Fatalf("reclaimed job = %+v, want job %d", claimed, intent.jobID)
	}
	if strings.Contains(status, "complete") {
		t.Fatal("shutdown falsely completed unfinished work")
	}
}
