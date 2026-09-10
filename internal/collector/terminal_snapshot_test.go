package collector

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTerminalSnapshotQuiescedCountersAreExact(t *testing.T) {
	metrics := newCollectorMetrics()
	metrics.archiveRequests["put"] = 7
	metrics.archiveRequests["head"] = 3
	root := t.TempDir()
	if err := writeTerminalArchiveSnapshot(root, metrics.terminalArchiveSnapshot()); err != nil {
		t.Fatalf("write terminal snapshot: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(root, ".control", "terminal", "collector.json"))
	if err != nil {
		t.Fatalf("read terminal snapshot: %v", err)
	}
	var decoded terminalArchiveSnapshot
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("decode terminal snapshot: %v", err)
	}
	if decoded.Schema != terminalSnapshotSchema || decoded.Producer != "collector" || !decoded.Terminal {
		t.Fatalf("terminal snapshot markers wrong: %+v", decoded)
	}
	if decoded.ProcessID == "" || decoded.StartedAt == "" || decoded.CapturedAt == "" {
		t.Fatalf("terminal snapshot identity incomplete: %+v", decoded)
	}
	if decoded.Operations["put"] != 7 || decoded.Operations["head"] != 3 || len(decoded.Operations) != 2 {
		t.Fatalf("terminal snapshot counters wrong: %+v", decoded.Operations)
	}
	info, err := os.Stat(filepath.Join(root, ".control", "terminal", "collector.json"))
	if err != nil {
		t.Fatalf("stat terminal snapshot: %v", err)
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatalf("terminal snapshot mode = %o, want 600", info.Mode().Perm())
	}
}

func TestTerminalSnapshotWriteFailureIsVisible(t *testing.T) {
	metrics := newCollectorMetrics()
	if err := writeTerminalArchiveSnapshot("", metrics.terminalArchiveSnapshot()); err == nil {
		t.Fatal("empty spool root must fail")
	}
	if err := writeTerminalArchiveSnapshot("relative/path", metrics.terminalArchiveSnapshot()); err == nil {
		t.Fatal("relative spool root must fail")
	}
	// A regular file where the terminal directory belongs is not a spool.
	blocker := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(blocker, []byte("x"), 0o600); err != nil {
		t.Fatalf("seed blocker: %v", err)
	}
	if err := writeTerminalArchiveSnapshot(blocker, metrics.terminalArchiveSnapshot()); err == nil {
		t.Fatal("file-as-spool-root must fail")
	}
	// A symlink at the fixed path is never followed or replaced.
	root := t.TempDir()
	link := filepath.Join(root, ".control", "terminal", "collector.json")
	if err := os.MkdirAll(filepath.Dir(link), 0o700); err != nil {
		t.Fatalf("seed terminal dir: %v", err)
	}
	if err := os.Symlink("/tmp/clashlens-terminal-evil", link); err != nil {
		t.Fatalf("seed symlink: %v", err)
	}
	if err := writeTerminalArchiveSnapshot(root, metrics.terminalArchiveSnapshot()); err == nil {
		t.Fatal("symlinked terminal path must fail")
	}
	if _, err := os.Lstat("/tmp/clashlens-terminal-evil"); !os.IsNotExist(err) {
		t.Fatal("symlink target must never be created")
	}
	// An unmarked snapshot is never published as terminal evidence.
	bad := metrics.terminalArchiveSnapshot()
	bad.Terminal = false
	if err := writeTerminalArchiveSnapshot(root, bad); err == nil {
		t.Fatal("unmarked snapshot must fail")
	}
}

func TestTerminalSnapshotSkippedOnlyForEmptySpoolRoot(t *testing.T) {
	metrics := newCollectorMetrics()
	metrics.archiveRequests["put"] = 1
	// Legacy/default schema1 deployments run without a spool root: the
	// graceful shutdown path skips the snapshot and succeeds.
	if err := writeTerminalSnapshotUnlessLegacy("", metrics.terminalArchiveSnapshot()); err != nil {
		t.Fatalf("empty spool root must skip cleanly: %v", err)
	}
	if err := writeTerminalSnapshotUnlessLegacy("   ", metrics.terminalArchiveSnapshot()); err != nil {
		t.Fatalf("blank spool root must skip cleanly: %v", err)
	}
	// Every configured spool root attempts the write: a temp dir emits
	// the exact quiesced counters.
	root := t.TempDir()
	if err := writeTerminalSnapshotUnlessLegacy(root, metrics.terminalArchiveSnapshot()); err != nil {
		t.Fatalf("configured spool root must emit: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(root, ".control", "terminal", "collector.json"))
	if err != nil {
		t.Fatalf("read emitted terminal snapshot: %v", err)
	}
	var decoded terminalArchiveSnapshot
	if err := json.Unmarshal(raw, &decoded); err != nil {
		t.Fatalf("decode emitted terminal snapshot: %v", err)
	}
	if !decoded.Terminal || decoded.Operations["put"] != 1 {
		t.Fatalf("emitted terminal snapshot wrong: %+v", decoded)
	}
	// Configured but failing spool roots return a visible error: a
	// file blocking the terminal directory fails the write, never
	// silently bypasses it.
	blocked := t.TempDir()
	if err := os.MkdirAll(filepath.Join(blocked, ".control"), 0o700); err != nil {
		t.Fatalf("seed control dir: %v", err)
	}
	if err := os.WriteFile(filepath.Join(blocked, ".control", "terminal"), []byte("x"), 0o600); err != nil {
		t.Fatalf("seed terminal blocker: %v", err)
	}
	if err := writeTerminalSnapshotUnlessLegacy(blocked, metrics.terminalArchiveSnapshot()); err == nil {
		t.Fatal("blocked terminal dir must return a visible error")
	}
	// Invalid snapshots fail even under a healthy root.
	bad := metrics.terminalArchiveSnapshot()
	bad.Schema = "unknown-schema"
	if err := writeTerminalSnapshotUnlessLegacy(root, bad); err == nil {
		t.Fatal("unknown schema must fail validation")
	}
}

func TestTerminalSnapshotZeroAttemptsAreExplicit(t *testing.T) {
	// A producer with zero real attempts is accepted only via an explicit
	// matching terminal zero snapshot, never via absence.
	metrics := newCollectorMetrics()
	root := t.TempDir()
	if err := writeTerminalArchiveSnapshot(root, metrics.terminalArchiveSnapshot()); err != nil {
		t.Fatalf("write zero terminal snapshot: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(root, ".control", "terminal", "collector.json"))
	if err != nil {
		t.Fatalf("read zero terminal snapshot: %v", err)
	}
	if !strings.Contains(string(raw), `"terminal":true`) {
		t.Fatalf("zero snapshot is not marked terminal: %s", raw)
	}
}
