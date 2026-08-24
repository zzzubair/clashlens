package collector

import (
	"bufio"
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

// startVersionTwoStoreWithURL returns a version-two store and its database
// URL so a test can reopen the same database after closing the store.
func startVersionTwoStoreWithURL(t *testing.T, ctx context.Context) (*store, string) {
	t.Helper()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to version-two PostgreSQL: %v", err)
	}
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	if err := connection.Close(ctx); err != nil {
		t.Fatalf("close migration connection: %v", err)
	}
	opened, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("open version-two store: %v", err)
	}
	t.Cleanup(opened.close)
	return opened, databaseURL
}

// TestSharedTrafficGateCrossRuntimeConcurrentPermits proves the fixed
// non-borrowing budgets when Go and Python acquire permits concurrently
// against one PostgreSQL database. Go may grant at most 29 per rolling
// second, Python at most 1, and the combined total at most 30. A denied
// request must not insert a permit.
func TestSharedTrafficGateCrossRuntimeConcurrentPermits(t *testing.T) {
	if testing.Short() {
		t.Skip("shared traffic gate cross-runtime integration test")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	store, databaseURL := startVersionTwoStoreWithURL(t, ctx)
	fingerprint := bearerTokenFingerprint("cross-runtime-shared-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}

	script := `
import os
from clashlens.api_db import ApiDatabase

url = os.environ["CLASHLENS_TEST_DATABASE_URL"]
fingerprint = os.environ["CLASHLENS_TEST_SHARED_FINGERPRINT"]
database = ApiDatabase(url, max_size=4)
try:
    database.register_official_credential(fingerprint)
    first = database.acquire_official_permit(fingerprint, caller="python", request_id="cross-runtime-python-1")
    print("PYTHON_FIRST", first.granted, first.reason, flush=True)
    second = database.acquire_official_permit(fingerprint, caller="python", request_id="cross-runtime-python-2")
    print("PYTHON_SECOND", second.granted, second.reason, flush=True)
finally:
    database.close()
`
	scriptFile := filepath.Join(t.TempDir(), "cross_runtime_gate.py")
	if err := os.WriteFile(scriptFile, []byte(script), 0o600); err != nil {
		t.Fatalf("write cross-runtime gate script: %v", err)
	}

	command := exec.CommandContext(ctx, "uv", "run", "--locked", "--project", "python", "python", scriptFile)
	command.Dir = repositoryRootForTest(t)
	command.Env = pythonServiceEnvironment(map[string]string{
		"CLASHLENS_TEST_DATABASE_URL":       databaseURL,
		"CLASHLENS_TEST_SHARED_FINGERPRINT": fingerprint,
		"PYTHONPATH":                        filepath.Join(repositoryRootForTest(t), "python", "src"),
	})
	stdout, err := command.StdoutPipe()
	if err != nil {
		t.Fatalf("create python stdout pipe: %v", err)
	}
	command.Stderr = os.Stderr
	if err := command.Start(); err != nil {
		t.Fatalf("start cross-runtime gate script: %v", err)
	}

	pythonFirst := make(chan string, 1)
	scannerDone := make(chan struct{})
	go func() {
		defer close(scannerDone)
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			line := scanner.Text()
			if strings.HasPrefix(line, "PYTHON_FIRST") {
				select {
				case pythonFirst <- line:
				default:
				}
			}
		}
	}()

	// Wait until Python reports its first grant, then fire Go permits in the
	// same rolling second so the two runtimes contend for one budget.
	select {
	case firstLine := <-pythonFirst:
		if !strings.Contains(firstLine, "True") {
			t.Fatalf("python first permit = %q, want granted", firstLine)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("python process did not report its first permit")
	case <-ctx.Done():
		t.Fatalf("wait for python first permit: %v", ctx.Err())
	}

	type result struct {
		granted bool
		err     error
	}
	results := make(chan result, 32)
	var wait sync.WaitGroup
	for range 30 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
			results <- result{granted: permit.granted, err: err}
		}()
	}
	wait.Wait()
	close(results)

	goGranted := 0
	for result := range results {
		if result.err != nil {
			t.Fatalf("acquire go permit: %v", result.err)
		}
		if result.granted {
			goGranted++
		}
	}

	if err := command.Wait(); err != nil {
		t.Fatalf("cross-runtime gate script failed: %v", err)
	}
	<-scannerDone

	if goGranted != 29 {
		t.Fatalf("go permits granted = %d, want exactly 29", goGranted)
	}
	var totalPermits int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_permits WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&totalPermits); err != nil {
		t.Fatalf("count shared permits: %v", err)
	}
	if totalPermits != 30 {
		t.Fatalf("shared permits = %d, want exactly 30 (29 Go + 1 Python)", totalPermits)
	}
}

// TestSharedTrafficGateRestartPreservesPermitsAndQuarantine proves that a
// process restart does not clear permits or quarantine: a fresh store against
// the same database sees the same durable state and keeps failing closed.
func TestSharedTrafficGateRestartPreservesPermitsAndQuarantine(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	first, databaseURL := startVersionTwoStoreWithURL(t, ctx)
	fingerprint := bearerTokenFingerprint("restart-shared-secret")
	if err := first.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}
	for range 3 {
		permit, err := first.acquireSharedPermit(ctx, fingerprint, "go")
		if err != nil {
			t.Fatalf("acquire go permit: %v", err)
		}
		if !permit.granted {
			t.Fatal("expected first three go permits to be granted")
		}
	}
	if err := first.quarantineSharedCredential(ctx, fingerprint, "collector:test", "official_api_authentication_failure"); err != nil {
		t.Fatalf("quarantine shared credential: %v", err)
	}
	first.close()

	// Reopen the same database as a fresh store, as after a process restart.
	reopened, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("reopen store after restart: %v", err)
	}
	t.Cleanup(reopened.close)

	var permits int
	if err := reopened.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_permits WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&permits); err != nil {
		t.Fatalf("count permits after restart: %v", err)
	}
	if permits != 3 {
		t.Fatalf("permits after restart = %d, want 3", permits)
	}

	permit, err := reopened.acquireSharedPermit(ctx, fingerprint, "python")
	if err != nil {
		t.Fatalf("read quarantine decision after restart: %v", err)
	}
	if permit.granted || permit.state != "quarantined" {
		t.Fatalf("permit after restart = %+v, want denied quarantined", permit)
	}

	var events int
	if err := reopened.pool.QueryRow(ctx, `
		SELECT count(*) FROM shared_api_credential_events WHERE credential_fingerprint = $1
	`, fingerprint).Scan(&events); err != nil {
		t.Fatalf("count credential events after restart: %v", err)
	}
	if events != 2 {
		t.Fatalf("credential events after restart = %d, want registration and quarantine", events)
	}
}

// TestSharedTrafficGateModeSequencing proves the bridge and required modes
// match the deployed contract version: bridge requires version 1, required
// requires version 2, and each rejects the other version.
func TestSharedTrafficGateModeSequencing(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	t.Run("bridge accepts version one", func(t *testing.T) {
		databaseURL := startContractDatabase(t)
		opened, err := openStore(ctx, databaseURL, 2)
		if err != nil {
			t.Fatalf("open bridge store: %v", err)
		}
		t.Cleanup(opened.close)
		if err := opened.validateTrafficGateMode(ctx, bridgeTrafficGateMode); err != nil {
			t.Fatalf("bridge mode rejected version one: %v", err)
		}
	})

	t.Run("bridge rejects version two", func(t *testing.T) {
		opened, _ := startVersionTwoStoreWithURL(t, ctx)
		if err := opened.validateTrafficGateMode(ctx, bridgeTrafficGateMode); err == nil {
			t.Fatal("bridge mode accepted version two")
		} else if !strings.Contains(err.Error(), "bridge shared traffic gate needs contract version 1") {
			t.Fatalf("bridge mode version-two error = %v", err)
		}
	})

	t.Run("required rejects version one", func(t *testing.T) {
		databaseURL := startContractDatabase(t)
		opened, err := openStore(ctx, databaseURL, 2)
		if err != nil {
			t.Fatalf("open required store: %v", err)
		}
		t.Cleanup(opened.close)
		if err := opened.validateTrafficGateMode(ctx, requiredTrafficGateMode); err == nil {
			t.Fatal("required mode accepted version one")
		} else if !strings.Contains(err.Error(), "required shared traffic gate needs contract version 2") {
			t.Fatalf("required mode version-one error = %v", err)
		}
	})

	t.Run("required accepts version two", func(t *testing.T) {
		opened, _ := startVersionTwoStoreWithURL(t, ctx)
		if err := opened.validateTrafficGateMode(ctx, requiredTrafficGateMode); err != nil {
			t.Fatalf("required mode rejected version two: %v", err)
		}
	})
}

// TestSharedTrafficGateUnknownCallerFailsClosed proves the gate rejects
// callers outside the fixed go/python pair before any product work.
func TestSharedTrafficGateUnknownCallerFailsClosed(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	opened, _ := startVersionTwoStoreWithURL(t, ctx)
	fingerprint := bearerTokenFingerprint("unknown-caller-secret")
	if err := opened.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:test"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}
	if _, err := opened.acquireSharedPermit(ctx, fingerprint, "browser"); err == nil {
		t.Fatal("unknown shared-gate caller was accepted")
	}
}
