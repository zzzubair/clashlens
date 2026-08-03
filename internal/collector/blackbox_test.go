package collector

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCollectorExecutableHappyPathAgainstFakeOfficialAPI(t *testing.T) {
	if testing.Short() {
		t.Skip("black-box process test")
	}
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	now := time.Now().UTC()
	if _, _, err := store.scheduleResetSweep(ctx, resetBoundaryAtOrBefore(now)); err != nil {
		t.Fatalf("seed empty current reset sweep: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	store.close()

	profileBody := []byte("{\n  \"tag\": \"#2PP\", \"name\": \"Black Box\"\n}\n")
	battleLogBody := []byte("{\"items\":[],\"paging\":{}}\n")
	requestsArrived := make(chan struct{}, 2)
	releaseRequests := make(chan struct{})
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		requestsArrived <- struct{}{}
		<-releaseRequests
		response.Header().Set("Content-Type", "application/json")
		if request.URL.Path == "/v1/players/#2PP/battlelog" {
			_, _ = response.Write(battleLogBody)
			return
		}
		if request.URL.Path != "/v1/players/#2PP" {
			http.NotFound(response, request)
			return
		}
		_, _ = response.Write(profileBody)
	}))
	t.Cleanup(api.Close)
	s3Server, s3Backend := newFakeS3Server(t)

	binary := filepath.Join(t.TempDir(), "collector")
	build := exec.CommandContext(ctx, "go", "build", "-o", binary, "./cmd/collector")
	build.Dir = repositoryRootForTest(t)
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build collector executable: %v\n%s", err, output)
	}

	command := exec.CommandContext(ctx, binary, "run", "--once", "--role", "both")
	command.Env = append(os.Environ(),
		"CLASHLENS_DATABASE_URL="+databaseURL,
		"CLASHLENS_ARCHIVE_ENDPOINT="+strings.TrimPrefix(s3Server.URL, "http://"),
		"CLASHLENS_ARCHIVE_SECURE=false",
		"CLASHLENS_ARCHIVE_BUCKET=evidence",
		"CLASHLENS_ARCHIVE_ACCESS_KEY=archive-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY=archive-secret",
		"CLASHLENS_OFFICIAL_API_ORIGIN="+api.URL,
		"CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN=true",
		"CLASHLENS_ALLOW_REDUCED_KEY_POOLS=true",
		"CLASHLENS_NORMAL_API_KEYS=normal-1=normal-secret",
		"CLASHLENS_INTERACTIVE_API_KEYS=interactive-1=interactive-secret",
		"CLASHLENS_RETRY_BASE_DELAY=1ms",
		"CLASHLENS_RETRY_MAXIMUM_DELAY=1ms",
		"CLASHLENS_RETRY_JITTER_FRACTION=0",
	)
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		t.Fatalf("start collector executable: %v", err)
	}
	for range 2 {
		select {
		case <-requestsArrived:
		case <-ctx.Done():
			t.Fatalf("collector did not start both official requests concurrently; stderr: %s", stderr.String())
		}
	}
	close(releaseRequests)
	if err := command.Wait(); err != nil {
		t.Fatalf("collector executable failed: %v\nstdout: %s\nstderr: %s", err, stdout.String(), stderr.String())
	}
	if !strings.Contains(stdout.String(), `"status":"complete"`) {
		t.Fatalf("collector stdout = %q, want complete status", stdout.String())
	}
	for _, secret := range []string{"normal-secret", "interactive-secret", "archive-secret", string(profileBody), string(battleLogBody)} {
		if strings.Contains(stderr.String(), secret) || strings.Contains(stdout.String(), secret) {
			t.Fatalf("collector output contains a credential or raw body")
		}
	}

	store, err = openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer store.close()
	var observations, processingJobs, completedRegularJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE work_type = 'regular_poll' AND status = 'complete'
	`).Scan(&completedRegularJobs); err != nil {
		t.Fatalf("count completed regular jobs: %v", err)
	}
	if observations != 2 || processingJobs != 2 || completedRegularJobs != 1 {
		t.Fatalf("durable outputs = %d observations, %d processing jobs, %d completed regular jobs; want 2, 2, 1", observations, processingJobs, completedRegularJobs)
	}

	if objects := s3Backend.contentObjectCount(); objects != 2 {
		t.Fatalf("raw archive contains %d objects, want 2", objects)
	}
	s3Backend.mu.Lock()
	defer s3Backend.mu.Unlock()
	archivedBodies := map[string]bool{}
	for _, object := range s3Backend.objects {
		archivedBodies[string(object.body)] = true
	}
	if !archivedBodies[string(profileBody)] || !archivedBodies[string(battleLogBody)] {
		t.Fatal("raw archive did not preserve both exact response bodies")
	}
}

func repositoryRootForTest(t *testing.T) string {
	t.Helper()
	workingDirectory, err := os.Getwd()
	if err != nil {
		t.Fatalf("get test working directory: %v", err)
	}
	return filepath.Clean(filepath.Join(workingDirectory, "../.."))
}
