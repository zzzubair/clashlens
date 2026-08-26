package collector

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestGoCollectorHandoffToPythonSignedPlayerPage(t *testing.T) {
	if testing.Short() {
		t.Skip("focused Go→Python process test")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	collectorStore := startVersionTwoStore(t, ctx)
	databaseURL := collectorStore.pool.Config().ConnString()
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))
	archive, backend := newFakeS3Server(t)
	profileBody, err := os.ReadFile(filepath.Join(repositoryRootForTest(t), "python", "testdata", "legend_i_profile_v1.json"))
	if err != nil {
		t.Fatalf("read synthetic profile fixture: %v", err)
	}
	digest := sha256.Sum256(profileBody)
	hash := hex.EncodeToString(digest[:])
	objectKey := "sha256/" + hash[:2] + "/" + hash
	backend.mu.Lock()
	backend.objects[objectKey] = &fakeS3Object{body: profileBody, hash: hash, writes: 1}
	backend.mu.Unlock()
	reference := "s3://evidence/" + objectKey

	now := time.Now().UTC()
	if _, err := collectorStore.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := collectorStore.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule production collector job: %v", err)
	}
	job, err := collectorStore.claimNext(ctx, "go-python-seam", normalPool, now, time.Minute, "go-python-token")
	if err != nil || job == nil {
		t.Fatalf("claim production collector job: job=%v err=%v", job, err)
	}
	attemptID, _, err := collectorStore.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepare production collector attempt: %v", err)
	}
	requestCount, err := collectorStore.beginEndpointRequest(ctx, job, attemptID, profileEndpoint, now.Add(-time.Second))
	if err != nil {
		t.Fatalf("begin production profile request: %v", err)
	}
	provenance, _, err := officialRequest(profileEndpoint, "#2PP")
	if err != nil {
		t.Fatalf("build production profile request: %v", err)
	}
	if err := collectorStore.commitObservation(
		ctx,
		job,
		attemptID,
		profileEndpoint,
		requestCount,
		officialResponse{
			requestStartedAt:    now.Add(-time.Second),
			responseCompletedAt: now,
			statusCode:          http.StatusOK,
			body:                profileBody,
			headers:             map[string]string{"Content-Type": "application/json"},
			request:             provenance,
			pagingEnvelopeState: "not_applicable",
		},
		hash,
		reference,
		"collector-v2",
		"normal-a",
		"observed",
		nil,
	); err != nil {
		t.Fatalf("commit production Go→Python handoff: %v", err)
	}
	// The worker image is contract-4 only; migrate after the collector has
	// written its v2 observation so this remains a genuine handoff test.
	for _, migration := range []string{
		"0004_source_parser_v2.sql",
		"0005_army_decoding.sql",
		"0006_provider_identities.sql",
		"0007_player_discovery.sql",
		"0008_public_army_analytics.sql",
		"0009_raw_evidence.sql",
		"0010_boundary_publication_coordinator.sql",
		"0011_boundary_publication_contract.sql",
	} {
		applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", migration))
	}
	boundary := boundaryAdmissionBoundary(now)
	if _, err := collectorStore.pool.Exec(ctx, `
		INSERT INTO collector_boundary_admission (
			boundary_at, regular_drain_complete, reset_drain_complete,
			safe_handoff, state
		) VALUES ($1, true, true, true, 'safe_handoff')
		ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff = true, state = 'safe_handoff'
	`, boundary); err != nil {
		t.Fatalf("seed worker handoff: %v", err)
	}

	environment := map[string]string{
		"CLASHLENS_DATABASE_URL":       databaseURL,
		"CLASHLENS_ARCHIVE_ENDPOINT":   strings.TrimPrefix(archive.URL, "http://"),
		"CLASHLENS_ARCHIVE_BUCKET":     "evidence",
		"CLASHLENS_ARCHIVE_ACCESS_KEY": "prototype-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY": "prototype-secret",
		"PYTHONPATH":                   filepath.Join(repositoryRootForTest(t), "python", "src"),
		"UV_LINK_MODE":                 "copy",
	}
	pythonEnvironment := filepath.Join(t.TempDir(), "venv")
	environment["UV_PROJECT_ENVIRONMENT"] = pythonEnvironment
	workerOutput, _ := runPythonService(
		t,
		ctx,
		environment,
		"worker",
		"--max-jobs", "1",
		"--archive-endpoint", strings.TrimPrefix(archive.URL, "http://"),
		"--archive-bucket", "evidence",
		"--archive-insecure-test-only",
	)
	if !strings.Contains(workerOutput, `"outcome": "processed"`) && !strings.Contains(workerOutput, `"outcome":"processed"`) {
		t.Fatalf("worker output = %q", workerOutput)
	}

	var state string
	if err := connection.QueryRow(ctx, "SELECT status FROM python_processing_jobs").Scan(&state); err != nil {
		t.Fatalf("read Python job state: %v", err)
	}
	if state != "complete" {
		t.Fatalf("Python job state = %q, want complete", state)
	}
	var effects int
	if err := connection.QueryRow(ctx, "SELECT count(*) FROM player_profile_effects").Scan(&effects); err != nil {
		t.Fatalf("count profile effects: %v", err)
	}
	if effects != 1 {
		t.Fatalf("profile effects = %d, want 1", effects)
	}

	port := unusedPortForPythonService(t)
	key := bytesFromHexForPythonService(t, strings.Repeat("11", 32))
	secretFile := filepath.Join(t.TempDir(), "typescript-hmac.key")
	if err := os.WriteFile(secretFile, []byte(base64.RawURLEncoding.EncodeToString(key)+"\n"), 0o600); err != nil {
		t.Fatalf("write Python API HMAC secret fixture: %v", err)
	}
	officialKeyFile := filepath.Join(t.TempDir(), "official-api.key")
	if err := os.WriteFile(officialKeyFile, []byte("synthetic-official-api-key\n"), 0o600); err != nil {
		t.Fatalf("write Python API official key fixture: %v", err)
	}
	serve := exec.CommandContext(
		ctx,
		filepath.Join(pythonEnvironment, "bin", "python"),
		"-m", "clashlens.cli", "serve",
		"--host", "127.0.0.1",
		"--port", fmt.Sprint(port),
		"--secret-file", secretFile,
		"--official-key-file", officialKeyFile,
		"--official-proxy-url", "http://127.0.0.1:9",
	)
	serve.Dir = repositoryRootForTest(t)
	serve.Env = pythonServiceEnvironment(environment)
	serve.Stdout = io.Discard
	serve.Stderr = io.Discard
	if err := serve.Start(); err != nil {
		t.Fatalf("start Python API: %v", err)
	}
	defer stopPythonServiceProcess(t, serve)
	waitForTCP(t, ctx, port)

	target := "/v1/players/%232PP?view=summary&view=live"
	requestTime := time.Now().Unix()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+fmt.Sprint(port)+target, nil)
	if err != nil {
		t.Fatalf("create signed player request: %v", err)
	}
	for name, value := range pythonServiceProofHeaders(key, target, requestTime) {
		request.Header.Set(name, value)
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("request signed player page: %v", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("player page status = %d, body = %q", response.StatusCode, body)
	}
	var payload map[string]any
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		t.Fatalf("decode player page: %v", err)
	}
	if payload["tag"] != "#2PP" || payload["eligibility"] != "eligible" || payload["freshness"] != "fresh" {
		t.Fatalf("player page payload = %#v", payload)
	}
	if _, found := payload["id"]; found {
		t.Fatalf("player page exposed an internal numeric ID: %#v", payload)
	}
}

func stopPythonServiceProcess(t *testing.T, command *exec.Cmd) {
	t.Helper()
	if command.Process == nil {
		return
	}
	_ = command.Process.Signal(os.Interrupt)
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	select {
	case <-done:
		return
	case <-time.After(5 * time.Second):
		_ = command.Process.Kill()
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Errorf("Python API process did not stop after interrupt and kill")
	}
}

func runPythonService(t *testing.T, ctx context.Context, environment map[string]string, arguments ...string) (string, string) {
	t.Helper()
	commandArguments := append([]string{"run", "--locked", "--project", "python", "python", "-m", "clashlens.cli"}, arguments...)
	command := exec.CommandContext(ctx, "uv", commandArguments...)
	command.Dir = repositoryRootForTest(t)
	command.Env = pythonServiceEnvironment(environment)
	var stdout, stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		t.Fatalf("Python service command %v failed: %v\nstdout: %s\nstderr: %s", arguments, err, stdout.String(), stderr.String())
	}
	return stdout.String(), stderr.String()
}

func pythonServiceEnvironment(environment map[string]string) []string {
	values := append([]string{}, os.Environ()...)
	for name, value := range environment {
		values = append(values, name+"="+value)
	}
	return values
}

func unusedPortForPythonService(t *testing.T) int {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("find Python API port: %v", err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	if err := listener.Close(); err != nil {
		t.Fatalf("release Python API port: %v", err)
	}
	return port
}

func waitForTCP(t *testing.T, ctx context.Context, port int) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		connection, err := net.DialTimeout("tcp", "127.0.0.1:"+fmt.Sprint(port), 100*time.Millisecond)
		if err == nil {
			_ = connection.Close()
			return
		}
		select {
		case <-ctx.Done():
			t.Fatalf("wait for Python API: %v", ctx.Err())
		case <-time.After(50 * time.Millisecond):
		}
	}
	t.Fatalf("Python API did not listen on port %d", port)
}

func bytesFromHexForPythonService(t *testing.T, value string) []byte {
	t.Helper()
	decoded, err := hex.DecodeString(value)
	if err != nil {
		t.Fatalf("decode HMAC key: %v", err)
	}
	return decoded
}

func pythonServiceProofHeaders(key []byte, target string, now int64) map[string]string {
	caller := base64.RawURLEncoding.EncodeToString([]byte("typescript-website"))
	keyID := base64.RawURLEncoding.EncodeToString([]byte("current"))
	targetB64 := base64.RawURLEncoding.EncodeToString([]byte(target))
	bodyHash := sha256.Sum256(nil)
	requestID := "00000000-0000-4000-8000-000000000029"
	signing := strings.Join([]string{
		"clashlens-hmac-v1",
		"caller:" + caller,
		"key-id:" + keyID,
		"audience:clashlens-python-private-api",
		"method:GET",
		"target:" + targetB64,
		"body-sha256:" + hex.EncodeToString(bodyHash[:]),
		"issued-at:" + fmt.Sprint(now),
		"expires-at:" + fmt.Sprint(now+10),
		"request-id:" + requestID,
		"provider:",
		"provider-subject:",
	}, "\n")
	mac := hmac.New(sha256.New, key)
	_, _ = mac.Write([]byte(signing))
	return map[string]string{
		"X-ClashLens-Proof-Version":    "clashlens-hmac-v1",
		"X-ClashLens-Caller":           caller,
		"X-ClashLens-Key-Id":           keyID,
		"X-ClashLens-Issued-At":        fmt.Sprint(now),
		"X-ClashLens-Expires-At":       fmt.Sprint(now + 10),
		"X-ClashLens-Request-Id":       requestID,
		"X-ClashLens-Provider":         "",
		"X-ClashLens-Provider-Subject": "",
		"X-ClashLens-Signature":        base64.RawURLEncoding.EncodeToString(mac.Sum(nil)),
	}
}
