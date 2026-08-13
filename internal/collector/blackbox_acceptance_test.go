package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestCollectorExecutableDoesNotClaimWhenArchiveReadinessFails(t *testing.T) {
	if testing.Short() {
		t.Skip("black-box process test")
	}
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, now() - interval '1 minute', 'blackbox-admission-test', 'pending')
	`); err != nil {
		t.Fatalf("insert due job: %v", err)
	}
	store.close()

	var apiRequests atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		apiRequests.Add(1)
		response.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(api.Close)
	archive, archiveBackend := newFakeS3Server(t)
	archiveBackend.mu.Lock()
	archiveBackend.failBucketChecksAfter = 1
	archiveBackend.mu.Unlock()
	binary := buildCollectorBinary(t, ctx)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	command := exec.CommandContext(ctx, binary, "run", "--once", "--role", "worker")
	command.Env = collectorProcessEnvironment(environment)
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err == nil || !strings.Contains(stderr.String(), "archive") {
		t.Fatalf("collector error = %v and stderr = %q, want archive readiness failure", err, stderr.String())
	}
	if requests := apiRequests.Load(); requests != 0 {
		t.Fatalf("official API request count = %d, want 0", requests)
	}

	store, err = openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer store.close()
	var status string
	var attempts int
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE coalescing_key = 'blackbox-admission-test'`).Scan(&status); err != nil {
		t.Fatalf("read due job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_attempts`).Scan(&attempts); err != nil {
		t.Fatalf("count attempts: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("job status = %q and attempts = %d, want pending and 0", status, attempts)
	}
}

func TestCollectorExecutablePreserves404MalformedJSONAndContentReuse(t *testing.T) {
	if testing.Short() {
		t.Skip("black-box process test")
	}
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
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
		VALUES
			('#2PP', true, $1),
			('#3PP', true, $1),
			('#4PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active players: %v", err)
	}
	store.close()

	notFoundBody := []byte(`{"reason":"notFound"}`)
	malformedBody := []byte(`{"tag":`)
	battleLogBody := []byte(`{"items":[]}`)
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(request.URL.Path, "/battlelog") {
			_, _ = response.Write(battleLogBody)
			return
		}
		switch request.URL.Path {
		case "/v1/players/#2PP":
			response.WriteHeader(http.StatusNotFound)
			_, _ = response.Write(notFoundBody)
		case "/v1/players/#3PP", "/v1/players/#4PP":
			_, _ = response.Write(malformedBody)
		default:
			http.NotFound(response, request)
		}
	}))
	t.Cleanup(api.Close)
	archive, archiveBackend := newFakeS3Server(t)
	binary := buildCollectorBinary(t, ctx)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	stdout, stderr := runCollectorProcess(t, ctx, binary, environment, "run", "--once", "--role", "both")
	if !strings.Contains(stdout, `"status":"complete"`) {
		t.Fatalf("collector stdout = %q, want complete status", stdout)
	}
	for _, sensitive := range []string{
		environment["CLASHLENS_NORMAL_API_KEYS"],
		environment["CLASHLENS_INTERACTIVE_API_KEYS"],
		environment["CLASHLENS_ARCHIVE_SECRET_KEY"],
		string(notFoundBody),
		string(malformedBody),
		string(battleLogBody),
	} {
		if strings.Contains(stdout, sensitive) || strings.Contains(stderr, sensitive) {
			t.Fatalf("collector output contains a credential or raw response body")
		}
	}

	store, err = openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("reopen store: %v", err)
	}
	defer store.close()
	var observations, processingJobs, notFound, activePlayers int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations WHERE http_status = 404`).Scan(&notFound); err != nil {
		t.Fatalf("count 404 observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM players WHERE active`).Scan(&activePlayers); err != nil {
		t.Fatalf("count active players: %v", err)
	}
	malformedDigest := sha256.Sum256(malformedBody)
	malformedHash := hex.EncodeToString(malformedDigest[:])
	var malformedOccurrences int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_observations WHERE response_hash = $1
	`, malformedHash).Scan(&malformedOccurrences); err != nil {
		t.Fatalf("count malformed-body occurrences: %v", err)
	}
	if observations != 6 || processingJobs != 6 || notFound != 1 || activePlayers != 3 || malformedOccurrences != 2 {
		t.Fatalf(
			"durable outputs = observations %d, processing %d, 404 %d, active %d, reused malformed %d; want 6, 6, 1, 3, 2",
			observations,
			processingJobs,
			notFound,
			activePlayers,
			malformedOccurrences,
		)
	}

	if objects := archiveBackend.contentObjectCount(); objects != 3 {
		t.Fatalf("archive object count = %d, want 3 distinct content objects", objects)
	}
	archiveBackend.mu.Lock()
	defer archiveBackend.mu.Unlock()
	if archiveBackend.bucketChecks != 2 {
		t.Fatalf("archive bucket readiness checks = %d, want startup and first-admission checks", archiveBackend.bucketChecks)
	}
	for _, body := range [][]byte{notFoundBody, malformedBody, battleLogBody} {
		found := false
		for _, object := range archiveBackend.objects {
			if bytes.Equal(object.body, body) {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("archive does not contain exact body %q", body)
		}
	}
}

func TestCollectorExecutableCoalescesConcurrentRefreshAndHonorsCooldown(t *testing.T) {
	if testing.Short() {
		t.Skip("black-box process test")
	}
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	defer store.close()
	var apiRequests atomic.Int64
	api := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		apiRequests.Add(1)
		response.Header().Set("Content-Type", "application/json")
		if strings.HasSuffix(request.URL.Path, "/battlelog") {
			_, _ = response.Write([]byte(`{"items":[]}`))
			return
		}
		_, _ = response.Write([]byte(`{"tag":"#2PP"}`))
	}))
	t.Cleanup(api.Close)
	archive, _ := newFakeS3Server(t)
	binary := buildCollectorBinary(t, ctx)
	environment := runtimeTestEnvironment(databaseURL, archive.URL, api.URL)
	environment["CLASHLENS_INTERACTIVE_COOLDOWN"] = "30s"

	type enqueueOutput struct {
		JobID  int64 `json:"job_id"`
		Reused bool  `json:"reused"`
	}
	commands := make([]*exec.Cmd, 2)
	stdout := make([]bytes.Buffer, 2)
	stderr := make([]bytes.Buffer, 2)
	for index := range commands {
		command := exec.CommandContext(ctx, binary, "enqueue", "--type", "live_refresh", "--tag", "#2PP")
		command.Env = collectorProcessEnvironment(environment)
		command.Stdout = &stdout[index]
		command.Stderr = &stderr[index]
		commands[index] = command
		if err := command.Start(); err != nil {
			t.Fatalf("start concurrent enqueue %d: %v", index, err)
		}
	}
	results := make([]enqueueOutput, 2)
	for index, command := range commands {
		if err := command.Wait(); err != nil {
			t.Fatalf("concurrent enqueue %d failed: %v\nstdout: %s\nstderr: %s", index, err, stdout[index].String(), stderr[index].String())
		}
		if err := json.Unmarshal(stdout[index].Bytes(), &results[index]); err != nil {
			t.Fatalf("decode concurrent enqueue %d: %v", index, err)
		}
	}
	if results[0].JobID != results[1].JobID || results[0].Reused == results[1].Reused {
		t.Fatalf("concurrent enqueue results = %#v, want one shared job and one reused result", results)
	}
	var activeJobs, createdEvents, coalescedEvents int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs
		WHERE normalized_tag = '#2PP' AND capacity_pool = 'interactive' AND status IN ('pending', 'leased', 'waiting_retry')
	`).Scan(&activeJobs); err != nil {
		t.Fatalf("count active interactive jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_interactive_intent_events WHERE outcome = 'created'`).Scan(&createdEvents); err != nil {
		t.Fatalf("count created intent events: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_interactive_intent_events WHERE outcome = 'coalesced'`).Scan(&coalescedEvents); err != nil {
		t.Fatalf("count coalesced intent events: %v", err)
	}
	if activeJobs != 1 || createdEvents != 1 || coalescedEvents != 1 {
		t.Fatalf("coalescing state = jobs %d, created %d, coalesced %d; want 1, 1, 1", activeJobs, createdEvents, coalescedEvents)
	}

	runCollectorProcess(t, ctx, binary, environment, "run", "--once", "--role", "worker")
	if apiRequests.Load() != 2 {
		t.Fatalf("official API requests after shared job = %d, want 2", apiRequests.Load())
	}
	thirdStdout, _ := runCollectorProcess(t, ctx, binary, environment, "enqueue", "--type", "live_refresh", "--tag", "#2PP")
	var third enqueueOutput
	if err := json.Unmarshal([]byte(thirdStdout), &third); err != nil {
		t.Fatalf("decode cooldown enqueue: %v", err)
	}
	if !third.Reused || third.JobID != results[0].JobID {
		t.Fatalf("cooldown enqueue = %#v, want prior completed job", third)
	}
	if apiRequests.Load() != 2 {
		t.Fatalf("official API requests after cooldown hit = %d, want 2", apiRequests.Load())
	}
	var cooldownEvents int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_interactive_intent_events WHERE outcome = 'cooldown_hit'`).Scan(&cooldownEvents); err != nil {
		t.Fatalf("count cooldown intent events: %v", err)
	}
	if cooldownEvents != 1 {
		t.Fatalf("cooldown event count = %d, want 1", cooldownEvents)
	}
}

func buildCollectorBinary(t *testing.T, ctx context.Context) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "collector")
	command := exec.CommandContext(ctx, "go", "build", "-o", binary, "./cmd/collector")
	command.Dir = repositoryRootForTest(t)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("build collector executable: %v\n%s", err, output)
	}
	return binary
}

func runCollectorProcess(
	t *testing.T,
	ctx context.Context,
	binary string,
	environment map[string]string,
	arguments ...string,
) (string, string) {
	t.Helper()
	command := exec.CommandContext(ctx, binary, arguments...)
	command.Env = collectorProcessEnvironment(environment)
	var stdout, stderr bytes.Buffer
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		if ctx.Err() != nil {
			t.Fatalf("collector process timed out: %v\nstdout: %s\nstderr: %s", ctx.Err(), stdout.String(), stderr.String())
		}
		t.Fatalf("collector process failed: %v\nstdout: %s\nstderr: %s", err, stdout.String(), stderr.String())
	}
	return stdout.String(), stderr.String()
}

func collectorProcessEnvironment(environment map[string]string) []string {
	values := append([]string{}, os.Environ()...)
	for name, value := range environment {
		values = append(values, name+"="+value)
	}
	return values
}
