package collector

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

func newFaultTestSpool(t *testing.T) *evidenceSpool {
	t.Helper()
	spool, err := newEvidenceSpool(spoolConfig{
		root: filepath.Join(t.TempDir(), "spool"), maxBytes: 4 << 20, maxObjects: 100,
		staleTempAge: time.Hour,
	})
	if err != nil {
		t.Fatalf("newEvidenceSpool returned an error: %v", err)
	}
	t.Cleanup(spool.close)
	return spool
}

func ledgerFinalBytes(t *testing.T, spool *evidenceSpool) spoolLedger {
	t.Helper()
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatalf("read spool ledger: %v", err)
	}
	return ledger
}

func TestSpoolShortWriteLeavesNoPartialFile(t *testing.T) {
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	body := []byte(strings.Repeat("x", 4096))
	short := io.MultiReader(bytes.NewReader(body[:16]), failingShortReader{})
	_, err = spool.write(reservation, short)
	if err == nil {
		t.Fatal("short write was accepted")
	}
	assertNoFinalAndReleased(t, spool, reservation)
}

type failingShortReader struct{}

func (failingShortReader) Read([]byte) (int, error) { return 0, errors.New("injected transport stall") }

func assertNoFinalAndReleased(t *testing.T, spool *evidenceSpool, reservation *spoolReservation) {
	t.Helper()
	if !reservation.released {
		t.Fatal("reservation was not released after failure")
	}
	entries, err := os.ReadDir(filepath.Join(spool.cfg.root, "tmp"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 {
		t.Fatalf("temporary files survived the failed write: %d entries", len(entries))
	}
	prefixes, err := os.ReadDir(filepath.Join(spool.cfg.root, "sha256"))
	if err != nil {
		t.Fatal(err)
	}
	for _, prefix := range prefixes {
		files, _ := os.ReadDir(filepath.Join(spool.cfg.root, "sha256", prefix.Name()))
		if len(files) != 0 {
			t.Fatalf("partial final file exists after failed write: %s", files[0].Name())
		}
	}
	if ledger := ledgerFinalBytes(t, spool); ledger.FinalBytes != 0 || ledger.FinalObjects != 0 {
		t.Fatalf("ledger after failure = %+v, want no final objects", ledger)
	}
}

func TestSpoolENOSPCClassifiesDegradedCapacityAndPromotesNothing(t *testing.T) {
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	spool.faults = &spoolFaults{writeFileErr: syscall.ENOSPC}
	defer func() { spool.faults = nil }()
	body := []byte("exact bytes")
	_, err = spool.write(reservation, bytes.NewReader(body))
	if !errors.Is(err, syscall.ENOSPC) {
		t.Fatalf("write error = %v, want injected ENOSPC", err)
	}
	if category := archiveFailureCategory(err); category != "degraded_capacity" {
		t.Fatalf("ENOSPC category = %q, want degraded_capacity", category)
	}
	assertNoFinalAndReleased(t, spool, reservation)
}

func TestSpoolFileSyncFailureKeepsNoFinalFile(t *testing.T) {
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	spool.faults = &spoolFaults{fileSyncErr: syscall.EIO}
	defer func() { spool.faults = nil }()
	body := []byte("needs durable fsync")
	_, err = spool.write(reservation, bytes.NewReader(body))
	if err == nil || !strings.Contains(err.Error(), "input/output error") {
		t.Fatalf("fsync failure not surfaced: %v", err)
	}
	assertNoFinalAndReleased(t, spool, reservation)
}

func TestSpoolPromotionFailureRemovesTemporaryAndKeepsAdmission(t *testing.T) {
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	spool.faults = &spoolFaults{promoteErr: errors.New("injected link failure")}
	defer func() { spool.faults = nil }()
	body := []byte("promotion candidate")
	_, err = spool.write(reservation, bytes.NewReader(body))
	if err == nil {
		t.Fatal("promotion failure was swallowed")
	}
	assertNoFinalAndReleased(t, spool, reservation)
}

func TestSpoolDirectoryFsyncFailureReconcilesExactLedger(t *testing.T) {
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	spool.faults = &spoolFaults{dirSyncErr: syscall.EIO}
	body := []byte("directory fsync failure")
	_, err = spool.write(reservation, bytes.NewReader(body))
	if err == nil {
		t.Fatal("directory fsync failure was swallowed")
	}
	spool.faults = nil
	// The promoted file itself must be hash-valid even though the directory
	// fsync failed; reconciliation rebuilds exact totals from the filesystem.
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	final := filepath.Join(spool.cfg.root, "sha256", hash[:2], hash)
	winner, readErr := spool.readFinalNoFollow(final)
	if readErr != nil || !bytes.Equal(winner, body) {
		t.Fatalf("final file after dir-fsync failure is missing or corrupt: %v", readErr)
	}
	before := ledgerFinalBytes(t, spool)
	if err := spool.reconcile(); err != nil {
		t.Fatalf("reconcile returned an error: %v", err)
	}
	after := ledgerFinalBytes(t, spool)
	if after.FinalBytes != int64(len(body)) || after.FinalObjects != 1 {
		t.Fatalf("reconciled ledger = %+v, want exactly one %d-byte object", after, len(body))
	}
	if before.FinalBytes+before.TemporaryBytes+before.AbandonedTempBytes > 0 {
		t.Logf("pre-reconcile ledger retained partial accounting: %+v", before)
	}
}

func TestSpoolCorruptWinnerIsHashVerifiedAndReplaced(t *testing.T) {
	spool := newFaultTestSpool(t)
	body := []byte("authoritative bytes")
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	final := filepath.Join(spool.cfg.root, "sha256", hash[:2], hash)
	if err := os.MkdirAll(filepath.Dir(final), 0700); err != nil {
		t.Fatal(err)
	}
	if writeErr := os.WriteFile(final, []byte("corrupt winner"), 0600); writeErr != nil {
		t.Fatal(writeErr)
	}
	// Make the ledger truthful about the pre-existing corrupt object before
	// exercising the replacement delta.
	if err := spool.reconcile(); err != nil {
		t.Fatalf("reconcile returned an error: %v", err)
	}
	reservation, err := spool.reserve(1 << 20)
	if err != nil {
		t.Fatalf("reserve returned an error: %v", err)
	}
	evidence, err := spool.write(reservation, bytes.NewReader(body))
	if err != nil {
		t.Fatalf("write over corrupt winner returned an error: %v", err)
	}
	winner, readErr := spool.readFinalNoFollow(evidence.Path)
	if readErr != nil || string(winner) != string(body) {
		t.Fatalf("corrupt winner was not replaced with verified bytes: %v", readErr)
	}
	ledger := ledgerFinalBytes(t, spool)
	if ledger.FinalBytes != int64(len(body)) || ledger.FinalObjects != 1 {
		t.Fatalf("ledger after replacement = %+v, want one %d-byte object", ledger, len(body))
	}
}

// TestMain supports the forced-process-death scenario below.
func TestMain(m *testing.M) {
	switch os.Getenv("CLASHLENS_SPOOL_CRASH_CHILD") {
	case "":
		os.Exit(m.Run())
	case "hold-write":
		spool, err := newEvidenceSpool(spoolConfig{
			root: os.Getenv("CLASHLENS_SPOOL_ROOT"), maxBytes: 8 << 20, maxObjects: 100,
			staleTempAge: time.Hour,
		})
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		reservation, err := spool.reserve(1 << 20)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		body := []byte(strings.Repeat("z", 1<<20))
		if _, err := spool.write(reservation, bytes.NewReader(body)); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		// Hold every lock open until the parent kills this process mid-flight
		// of a second write, leaving live reservations and a temporary file.
		secondReservation, err := spool.reserve(1 << 20)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		tempPath := filepath.Join(spool.cfg.root, "tmp", "crash-child.tmp")
		temp, err := os.OpenFile(tempPath, os.O_CREATE|os.O_WRONLY, 0600)
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if _, err := temp.WriteString(strings.Repeat("q", 4096)); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		_ = secondReservation
		_ = temp
		time.Sleep(time.Minute)
		os.Exit(0)
	default:
		os.Exit(m.Run())
	}
}

func TestSpoolForcedProcessDeathReconcilesWithoutLiveReservationLoss(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	// Re-invoke this test binary as the crash child; TestMain intercepts it
	// through CLASHLENS_SPOOL_CRASH_CHILD and never reaches the test body.
	child := exec.Command(os.Args[0], "-test.run=TestSpoolCrashChildEntry$")
	child.Env = append(os.Environ(),
		"CLASHLENS_SPOOL_CRASH_CHILD=hold-write",
		"CLASHLENS_SPOOL_ROOT="+root,
	)
	stderr := &bytes.Buffer{}
	child.Stderr = stderr
	if err := child.Start(); err != nil {
		t.Fatalf("start crash child: %v", err)
	}
	defer func() { _ = child.Process.Kill(); _, _ = child.Process.Wait() }()

	// Wait for the child to publish its live second reservation and its
	// abandoned temporary file (the first reservation was already consumed by
	// the promoted final object).
	reservationsDir := filepath.Join(root, ".control", "reservations")
	deadline := time.Now().Add(30 * time.Second)
	for {
		if time.Now().After(deadline) {
			t.Fatalf("crash child never created its reservation: %s", stderr.String())
		}
		entries, _ := os.ReadDir(reservationsDir)
		_, tempErr := os.Stat(filepath.Join(root, "tmp", "crash-child.tmp"))
		if len(entries) >= 1 && tempErr == nil {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := child.Process.Kill(); err != nil {
		t.Fatalf("kill crash child: %v", err)
	}
	_, _ = child.Process.Wait()

	// Restart: startup reconciliation must remove unlocked dead reservations,
	// keep no corrupt final, and rebuild exact final/abandoned-temp totals.
	restarted, err := newEvidenceSpool(spoolConfig{
		root: root, maxBytes: 8 << 20, maxObjects: 100, staleTempAge: time.Hour,
	})
	if err != nil {
		t.Fatalf("restart spool after crash: %v", err)
	}
	defer restarted.close()
	ledger := ledgerFinalBytes(t, restarted)
	if ledger.FinalBytes != 1<<20 || ledger.FinalObjects != 1 {
		t.Fatalf("post-crash ledger = %+v, want exactly the promoted 1 MiB object", ledger)
	}
	if ledger.ReservedBytes != 0 || ledger.ReservedObjects != 0 {
		t.Fatalf("dead reservations survived reconciliation: %+v", ledger)
	}
	if ledger.AbandonedTempBytes != 4096 || ledger.AbandonedTempObjects != 1 {
		t.Fatalf("abandoned crash-child temporary not accounted: %+v", ledger)
	}
	prefixes, err := os.ReadDir(filepath.Join(root, "sha256"))
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for _, prefix := range prefixes {
		files, _ := os.ReadDir(filepath.Join(root, "sha256", prefix.Name()))
		count += len(files)
	}
	if count != 1 {
		t.Fatalf("final object count after crash = %d, want 1", count)
	}
}

// TestSpoolCrashChildEntry is selected by -test.run in the forced-death test;
// TestMain intercepts it and runs the hold-write child instead of the test.
func TestSpoolCrashChildEntry(t *testing.T) {
	if os.Getenv("CLASHLENS_SPOOL_CRASH_CHILD") == "" {
		t.Skip("crash-child entry point; exercised by the forced-death test")
	}
	t.Fatal("child entry must be intercepted by TestMain")
}

// TestSpoolStripeLockInteroperatesWithPython proves the cross-runtime flock
// contract: an exclusive flock held by a Python process on the same stripe
// inode blocks the Go collector, and Go's exclusive lock blocks Python.
func TestSpoolStripeLockInteroperatesWithPython(t *testing.T) {
	if testing.Short() {
		t.Skip("short mode skips the Python interoperability probe")
	}
	python, err := exec.LookPath("python3")
	if err != nil {
		t.Skip("python3 is unavailable")
	}
	spool := newFaultTestSpool(t)
	stripePath := filepath.Join(spool.cfg.root, ".locks", "0042")
	holdExclusive := fmt.Sprintf(
		"import fcntl,sys,time;fd=open(%q,'r+b');fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(3)",
		stripePath,
	)
	child := exec.Command(python, "-c", holdExclusive)
	stdout := &bytes.Buffer{}
	child.Stdout = stdout
	if err := child.Start(); err != nil {
		t.Fatalf("start python holder: %v", err)
	}
	defer func() { _ = child.Process.Kill(); _, _ = child.Process.Wait() }()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if strings.Contains(stdout.String(), "held") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("python holder never acquired its lock")
		}
		time.Sleep(5 * time.Millisecond)
	}
	// Phase 1: while Python holds the exclusive flock, Go's non-blocking
	// exclusive attempt must fail.
	file, openErr := os.OpenFile(stripePath, os.O_RDWR, 0600)
	if openErr != nil {
		t.Fatal(openErr)
	}
	defer file.Close()
	if lockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); lockErr == nil {
		_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
		t.Fatal("Go acquired an exclusive stripe lock while Python still holds it")
	} else if !errors.Is(lockErr, syscall.EWOULDBLOCK) {
		t.Fatalf("unexpected stripe lock error: %v", lockErr)
	}
	// Phase 2: release the Python holder, then Go holds the stripe and a
	// fresh Python LOCK_EX|LOCK_NB probe must be blocked.
	_ = child.Process.Kill()
	_, _ = child.Process.Wait()
	index := 0x42 & 0xfff
	if err := syscall.Flock(int(spool.locks[index].Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatalf("Go could not lock its own stripe after holder exit: %v", err)
	}
	reverseScript := fmt.Sprintf(
		"import fcntl,sys;fd=open(%q,'r+b')\ntry:\n    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:\n    print('blocked')\nelse:\n    print('acquired')\n",
		stripePath,
	)
	probe := exec.Command(python, "-c", reverseScript)
	probeOutput, probeErr := probe.Output()
	_ = syscall.Flock(int(spool.locks[index].Fd()), syscall.LOCK_UN)
	if probeErr != nil {
		t.Fatalf("python probe failed: %v", probeErr)
	}
	if strings.TrimSpace(string(probeOutput)) != "blocked" {
		t.Fatalf("python bypassed the Go-held stripe lock: %s", probeOutput)
	}
}
