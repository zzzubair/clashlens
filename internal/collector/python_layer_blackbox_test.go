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
	"github.com/zzzubair/ClashLens/internal/testsupport"
)

func TestPythonPrototypeSuiteEmbeddedPostgres(t *testing.T) {
	if testing.Short() {
		t.Skip("Python prototype PostgreSQL integration suite")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	projectRoot := filepath.Join(repositoryRootForTest(t), "python-prototype")
	environment := map[string]string{
		"CLASHLENS_TEST_DATABASE_URL": testsupport.StartPostgres(t),
		"UV_LINK_MODE":                "copy",
		"UV_PROJECT_ENVIRONMENT":      filepath.Join(t.TempDir(), "venv"),
	}
	command := exec.CommandContext(ctx, "uv", "run", "--locked", "--python", "3.12", "pytest", "-q")
	command.Dir = projectRoot
	command.Env = pythonPrototypeEnvironment(environment)
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("Python prototype suite failed: %v\n%s", err, output)
	}
	t.Log(strings.TrimSpace(string(output)))
}

func TestPythonPrototypeBlackBoxEmbeddedPostgresToSignedPlayerPage(t *testing.T) {
	if testing.Short() {
		t.Skip("Python prototype black-box process test")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	databaseURL := testsupport.StartPostgres(t)
	archive, backend := newFakeS3Server(t)
	profileBody, err := os.ReadFile(filepath.Join(repositoryRootForTest(t), "python-prototype", "testdata", "legend_i_profile_v1.json"))
	if err != nil {
		t.Fatalf("read synthetic profile fixture: %v", err)
	}
	digest := sha256.Sum256(profileBody)
	hash := hex.EncodeToString(digest[:])
	objectKey := "sha256/" + hash[:2] + "/" + hash
	backend.mu.Lock()
	backend.objects[objectKey] = &fakeS3Object{body: profileBody, hash: hash, writes: 1}
	backend.mu.Unlock()

	environment := map[string]string{
		"CLASHLENS_DATABASE_URL":       databaseURL,
		"CLASHLENS_ARCHIVE_ENDPOINT":   strings.TrimPrefix(archive.URL, "http://"),
		"CLASHLENS_ARCHIVE_BUCKET":     "evidence",
		"CLASHLENS_ARCHIVE_ACCESS_KEY": "prototype-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY": "prototype-secret",
		"PYTHONPATH":                   filepath.Join(repositoryRootForTest(t), "python-prototype", "src"),
		"UV_LINK_MODE":                 "copy",
	}
	pythonEnvironment := filepath.Join(t.TempDir(), "venv")
	environment["UV_PROJECT_ENVIRONMENT"] = pythonEnvironment
	python := func(arguments ...string) (string, string) {
		t.Helper()
		return runPythonPrototype(t, ctx, environment, arguments...)
	}
	python("init-db")
	reference := "s3://evidence/" + objectKey
	seedOutput, _ := python(
		"seed",
		"--occurrence-key", "blackbox-profile-1",
		"--tag", "#2PP",
		"--observed-at", time.Now().UTC().Add(-time.Minute).Format(time.RFC3339),
		"--response-hash", hash,
		"--archive-reference", reference,
	)
	if !strings.Contains(seedOutput, `"status": "seeded"`) && !strings.Contains(seedOutput, `"status":"seeded"`) {
		t.Fatalf("seed output = %q", seedOutput)
	}
	workerOutput, _ := python(
		"worker",
		"--max-jobs", "1",
		"--archive-endpoint", strings.TrimPrefix(archive.URL, "http://"),
		"--archive-bucket", "evidence",
		"--archive-insecure-test-only",
	)
	if !strings.Contains(workerOutput, `"outcome": "processed"`) && !strings.Contains(workerOutput, `"outcome":"processed"`) {
		t.Fatalf("worker output = %q", workerOutput)
	}

	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect after Python worker: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
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

	port := unusedPortForPythonPrototype(t)
	key := bytesFromHexForPythonPrototype(t, strings.Repeat("11", 32))
	secretFile := filepath.Join(t.TempDir(), "typescript-hmac.key")
	if err := os.WriteFile(secretFile, []byte(base64.RawURLEncoding.EncodeToString(key)+"\n"), 0o600); err != nil {
		t.Fatalf("write Python API HMAC secret fixture: %v", err)
	}
	serve := exec.CommandContext(ctx, filepath.Join(pythonEnvironment, "bin", "python"), "-m", "clashlens_prototype.cli", "serve", "--host", "127.0.0.1", "--port", fmt.Sprint(port), "--secret-file", secretFile)
	serve.Dir = repositoryRootForTest(t)
	serve.Env = pythonPrototypeEnvironment(environment)
	serve.Stdout = io.Discard
	serve.Stderr = io.Discard
	if err := serve.Start(); err != nil {
		t.Fatalf("start Python API: %v", err)
	}
	defer stopPythonPrototypeProcess(t, serve)
	waitForTCP(t, ctx, port)

	target := "/v1/players/%232PP?view=summary&view=live"
	now := time.Now().Unix()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://127.0.0.1:"+fmt.Sprint(port)+target, nil)
	if err != nil {
		t.Fatalf("create signed player request: %v", err)
	}
	for name, value := range pythonPrototypeProofHeaders(key, target, now) {
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

func stopPythonPrototypeProcess(t *testing.T, command *exec.Cmd) {
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

func runPythonPrototype(t *testing.T, ctx context.Context, environment map[string]string, arguments ...string) (string, string) {
	t.Helper()
	commandArguments := append([]string{"run", "--locked", "--project", "python-prototype", "python", "-m", "clashlens_prototype.cli"}, arguments...)
	command := exec.CommandContext(ctx, "uv", commandArguments...)
	command.Dir = repositoryRootForTest(t)
	command.Env = pythonPrototypeEnvironment(environment)
	var stdout, stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	if err := command.Run(); err != nil {
		t.Fatalf("Python prototype command %v failed: %v\nstdout: %s\nstderr: %s", arguments, err, stdout.String(), stderr.String())
	}
	return stdout.String(), stderr.String()
}

func pythonPrototypeEnvironment(environment map[string]string) []string {
	values := append([]string{}, os.Environ()...)
	for name, value := range environment {
		values = append(values, name+"="+value)
	}
	return values
}

func unusedPortForPythonPrototype(t *testing.T) int {
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

func bytesFromHexForPythonPrototype(t *testing.T, value string) []byte {
	t.Helper()
	decoded, err := hex.DecodeString(value)
	if err != nil {
		t.Fatalf("decode HMAC key: %v", err)
	}
	return decoded
}

func pythonPrototypeProofHeaders(key []byte, target string, now int64) map[string]string {
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
