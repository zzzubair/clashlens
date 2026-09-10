package collector

// Bounded terminal archive-attempt snapshot for issue #92 Slice C.
//
// The collector writes exactly one terminal snapshot after its worker loops
// quiesce and before process exit, so the observer can close archive-attempt
// accounting without interpreting an absent producer as zero. The snapshot
// carries schema, producer kind, exact process/incarnation identity,
// capture time, a terminal marker, and per-operation nonnegative counters.
//
// Budget: one fixed file per producer kind
// (<spool>/.control/terminal/collector.json), each write bounded to
// terminalSnapshotMaxBytes; the transient temp file lives beside it and is
// removed on every path. The .control tree is already excluded from spool
// data cleanup, so terminal evidence survives until read at finalize.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const terminalSnapshotSchema = "clashlens-collector-terminal-v1"

// terminalSnapshotMaxBytes bounds one terminal snapshot for all time; the
// per-operation map is tiny (a handful of archive operations), so anything
// larger is corruption, never legitimate growth.
const terminalSnapshotMaxBytes = 64 << 10

const terminalSnapshotProducerCollector = "collector"

// terminalArchiveSnapshot is the exact on-disk contract. Field names are
// stable: the observer validates them strictly.
type terminalArchiveSnapshot struct {
	Schema     string            `json:"schema"`
	Producer   string            `json:"producer"`
	ProcessID  string            `json:"process_id"`
	StartedAt  string            `json:"process_started_at"`
	CapturedAt string            `json:"captured_at"`
	Terminal   bool              `json:"terminal"`
	Operations map[string]uint64 `json:"operations"`
}

// terminalArchiveSnapshot captures the quiesced counters with the exact
// process/incarnation identity. Call only after worker loops quiesce.
func (m *collectorMetrics) terminalArchiveSnapshot() terminalArchiveSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	operations := make(map[string]uint64, len(m.archiveRequests))
	for operation, count := range m.archiveRequests {
		operations[operation] = count
	}
	return terminalArchiveSnapshot{
		Schema:     terminalSnapshotSchema,
		Producer:   terminalSnapshotProducerCollector,
		ProcessID:  m.processIdentity,
		StartedAt:  m.processStartedAt.UTC().Format(time.RFC3339),
		CapturedAt: time.Now().UTC().Format(time.RFC3339),
		Terminal:   true,
		Operations: operations,
	}
}

// writeTerminalSnapshotUnlessLegacy persists the post-quiescence terminal
// snapshot, skipping only legacy/default schema1 deployments that run
// without a spool root. Every configured spool root MUST attempt the
// write and propagate validation/write/fsync failure visibly: silence
// would let the observer mistake a missing producer for zero attempts.
// Schema 4 production continues to require a configured spool root
// through config validation; this is only the shutdown-side contract.
func writeTerminalSnapshotUnlessLegacy(spoolRoot string, snap terminalArchiveSnapshot) error {
	if strings.TrimSpace(spoolRoot) == "" {
		return nil
	}
	return writeTerminalArchiveSnapshot(spoolRoot, snap)
}

// writeTerminalArchiveSnapshot persists snap atomically under the shared
// spool control area. A failed write is always an error: silence would let
// the observer mistake a missing producer for zero attempts.
func writeTerminalArchiveSnapshot(spoolRoot string, snap terminalArchiveSnapshot) error {
	if strings.TrimSpace(spoolRoot) == "" || !filepath.IsAbs(spoolRoot) {
		return fmt.Errorf("terminal snapshot needs an absolute spool root")
	}
	if snap.Schema != terminalSnapshotSchema {
		return fmt.Errorf("terminal snapshot has unknown schema %q", snap.Schema)
	}
	if snap.Producer != terminalSnapshotProducerCollector {
		return fmt.Errorf("terminal snapshot has unknown producer %q", snap.Producer)
	}
	if strings.TrimSpace(snap.ProcessID) == "" {
		return fmt.Errorf("terminal snapshot is missing process identity")
	}
	if !snap.Terminal {
		return fmt.Errorf("terminal snapshot is not marked terminal")
	}
	for operation, count := range snap.Operations {
		if strings.TrimSpace(operation) == "" {
			return fmt.Errorf("terminal snapshot has an unnamed operation")
		}
		_ = count
	}
	body, err := json.Marshal(snap)
	if err != nil {
		return fmt.Errorf("encode terminal snapshot: %w", err)
	}
	if len(body) > terminalSnapshotMaxBytes {
		return fmt.Errorf("terminal snapshot exceeds %d bytes", terminalSnapshotMaxBytes)
	}
	dir := filepath.Join(spoolRoot, ".control", "terminal")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("create terminal snapshot dir: %w", err)
	}
	path := filepath.Join(dir, "collector.json")
	if info, err := os.Lstat(path); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("terminal snapshot path is a symlink")
		}
		if info.IsDir() {
			return fmt.Errorf("terminal snapshot path is a directory")
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("inspect terminal snapshot path: %w", err)
	}
	temporary, err := os.CreateTemp(dir, ".collector-*.tmp")
	if err != nil {
		return fmt.Errorf("create terminal snapshot temp file: %w", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("protect terminal snapshot temp file: %w", err)
	}
	if _, err := temporary.Write(append(body, '\n')); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("write terminal snapshot: %w", err)
	}
	if err := temporary.Sync(); err != nil {
		_ = temporary.Close()
		return fmt.Errorf("fsync terminal snapshot: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close terminal snapshot: %w", err)
	}
	if err := os.Rename(temporaryPath, path); err != nil {
		return fmt.Errorf("publish terminal snapshot: %w", err)
	}
	return nil
}
