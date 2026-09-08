package collector

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestEvidenceSpoolPublishesAndReusesVerifiedBytes(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	body := []byte("exact response")
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := spool.write(reservation, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(body)
	if evidence.Hash != hex.EncodeToString(digest[:]) {
		t.Fatalf("hash = %s", evidence.Hash)
	}
	before, _ := os.Stat(evidence.Path)
	ok, err := spool.verify(evidence.Hash, int64(len(body)))
	if err != nil || !ok {
		t.Fatalf("verify = %v, %v", ok, err)
	}
	after, _ := os.Stat(evidence.Path)
	if !before.ModTime().Equal(after.ModTime()) {
		t.Fatal("verified reuse changed final file")
	}
	if got, _ := os.ReadFile(evidence.Path); !bytes.Equal(got, body) {
		t.Fatalf("body = %q", got)
	}
}

func TestEvidenceSpoolConcurrentWritersConverge(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1 << 20, maxObjects: 20, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	body := []byte("concurrent exact response")
	var wait sync.WaitGroup
	for i := 0; i < 8; i++ {
		wait.Add(1)
		go func() {
			defer wait.Done()
			reservation, reserveErr := spool.reserve(1024)
			if reserveErr != nil {
				t.Error(reserveErr)
				return
			}
			if _, writeErr := spool.write(reservation, bytes.NewReader(body)); writeErr != nil {
				t.Error(writeErr)
			}
		}()
	}
	wait.Wait()
	digest := sha256.Sum256(body)
	ok, err := spool.verify(hex.EncodeToString(digest[:]), int64(len(body)))
	if err != nil || !ok {
		t.Fatalf("concurrent final verification = %v, %v", ok, err)
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if ledger.FinalObjects != 1 || ledger.ReservedObjects != 0 {
		t.Fatalf("ledger = %+v, want one final and no reservations", ledger)
	}
}

func TestEvidenceSpoolRejectsSymlinkedControlDirectory(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	spool.close()
	outside := filepath.Join(t.TempDir(), "outside-control")
	if err := os.Mkdir(outside, 0700); err != nil {
		t.Fatal(err)
	}
	control := filepath.Join(root, ".control")
	if err := os.Rename(control, control+".real"); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, control); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = os.Remove(control)
		_ = os.Rename(control+".real", control)
	}()
	if _, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour}); err == nil {
		t.Fatal("symlinked .control accepted")
	}
	if entries, err := os.ReadDir(outside); err != nil || len(entries) != 0 {
		t.Fatalf("outside target was populated: %v %v", entries, err)
	}
}

func TestEvidenceSpoolRejectsSymlinkedLockDirectory(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	spool.close()
	outside := filepath.Join(t.TempDir(), "outside-locks")
	if err := os.Mkdir(outside, 0700); err != nil {
		t.Fatal(err)
	}
	locks := filepath.Join(root, ".locks")
	if err := os.Rename(locks, locks+".real"); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, locks); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = os.Remove(locks)
		_ = os.Rename(locks+".real", locks)
	}()
	if _, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour}); err == nil {
		t.Fatal("symlinked .locks accepted")
	}
	if entries, err := os.ReadDir(outside); err != nil || len(entries) != 0 {
		t.Fatalf("outside lock directory was populated: %v %v", entries, err)
	}
}

func TestEvidenceSpoolWriteFailureTransfersAbandonedTempBytes(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("permission-based unlink injection requires a non-root tester")
	}
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 4096, maxObjects: 10, staleTempAge: time.Millisecond})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	// Create a real temporary file exactly like a failed writer would, then
	// deny directory write permission so its deletion genuinely fails.
	tempPath := filepath.Join(root, "tmp", "abandoned.tmp")
	descriptor, err := openSpoolRelative(root, tempPath, syscall.O_CREAT|syscall.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		t.Fatal(err)
	}
	body := bytes.Repeat([]byte("x"), 2048)
	file := os.NewFile(uintptr(descriptor), tempPath)
	if _, err := file.Write(body); err != nil {
		t.Fatal(err)
	}
	_ = file.Close()
	tmpDir := filepath.Join(root, "tmp")
	if err := os.Chmod(tmpDir, 0500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(tmpDir, 0700)
	reservation, err := spool.reserve(int64(len(body)))
	if err != nil {
		t.Fatal(err)
	}
	// The shared failure path of write(): try to delete the temporary file,
	// then release the reservation whatever happened.
	spool.removeOrAbandonTemporary(tempPath)
	if err := reservation.release(); err != nil {
		t.Fatal(err)
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if ledger.AbandonedTempObjects != 1 || ledger.AbandonedTempBytes != int64(len(body)) {
		t.Fatalf("ledger after failed unlink = %+v, want one abandoned object with %d bytes", ledger, len(body))
	}
	if _, statErr := os.Stat(tempPath); statErr != nil {
		t.Fatalf("surviving temporary file vanished: %v", statErr)
	}
	// Admission must count the abandoned bytes against the configured maximum.
	if _, err := spool.reserve(4096); err == nil {
		t.Fatal("admission ignored abandoned temporary bytes")
	}
	// Reconciliation is idempotent for the crash-before-promotion state.
	before := ledger.AbandonedTempBytes
	if err := spool.reconcile(); err != nil {
		t.Fatal(err)
	}
	reconciled, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if reconciled.AbandonedTempBytes != before || reconciled.AbandonedTempObjects != 1 {
		t.Fatalf("reconcile changed abandoned accounting: %+v", reconciled)
	}
	// Stale cleanup reclaims the unremovable temporary file and its accounting.
	os.Chmod(tmpDir, 0700)
	if err := spool.sweepStale(time.Now().Add(time.Hour)); err != nil {
		t.Fatal(err)
	}
	finalLedger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if finalLedger.AbandonedTempObjects != 0 || finalLedger.AbandonedTempBytes != 0 {
		t.Fatalf("stale sweep left abandoned accounting: %+v", finalLedger)
	}
}

func TestEvidenceSpoolRejectsSymlinkedFinal(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	body := []byte("symlink-target")
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	final := spool.finalPath(hash)
	if err := os.MkdirAll(filepath.Dir(final), 0700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(target, body, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, final); err != nil {
		t.Fatal(err)
	}
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := spool.write(reservation, bytes.NewReader(body)); err == nil {
		t.Fatal("symlinked final accepted")
	}
	if got, _ := os.ReadFile(target); !bytes.Equal(got, body) {
		t.Fatal("outside target was changed")
	}
}

func TestEvidenceSpoolCleanupRejectsSymlinkedFinal(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	body := []byte("outside")
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	final := spool.finalPath(hash)
	if err := os.MkdirAll(filepath.Dir(final), 0700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(target, body, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, final); err != nil {
		t.Fatal(err)
	}
	if _, err := spool.cleanup(context.Background(), time.Now().Add(time.Hour), time.Second, 1, func(context.Context, string) (bool, error) { return true, nil }); err == nil {
		t.Fatal("cleanup accepted symlinked final")
	}
	if got, err := os.ReadFile(target); err != nil || !bytes.Equal(got, body) {
		t.Fatalf("outside target changed: %q, %v", got, err)
	}
}

func TestEvidenceSpoolRejectsOversizedBodies(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1024, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	reservation, err := spool.reserve(4)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := spool.write(reservation, bytes.NewReader([]byte("12345"))); err == nil {
		t.Fatal("oversized body accepted")
	}
}

func TestEvidenceSpoolSubstitutionRaceIsRejected(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	body := []byte("race target")
	digest := sha256.Sum256(body)
	hash := hex.EncodeToString(digest[:])
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := spool.write(reservation, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	outside := filepath.Join(t.TempDir(), "outside")
	if err := os.WriteFile(outside, []byte("outside"), 0600); err != nil {
		t.Fatal(err)
	}
	// Substitute a trusted directory component with a symlink to an outside
	// directory after publication; every descendant access must refuse.
	prefix := filepath.Join(root, "sha256", hash[:2])
	substitute := filepath.Join(t.TempDir(), "sub")
	if err := os.Mkdir(substitute, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(prefix, prefix+".real"); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(substitute, prefix); err != nil {
		t.Fatal(err)
	}
	defer func() {
		_ = os.Remove(prefix)
		_ = os.Rename(prefix+".real", prefix)
	}()
	// The original inode stays reachable through its descriptor-based path
	// only; reads through the substituted path must fail, not follow.
	if _, _, err := spool.read(hash, int64(len(body))); err == nil {
		t.Fatal("read followed a substituted symlinked directory")
	}
	if got, err := os.ReadFile(filepath.Join(substitute, hash)); err == nil {
		t.Fatalf("substituted directory was populated: %q", got)
	}
	_ = evidence
}

func TestEvidenceSpoolRejectsSymlinkedRootParent(t *testing.T) {
	base := t.TempDir()
	real := filepath.Join(base, "real")
	if err := os.MkdirAll(real, 0700); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Fatal(err)
	}
	if _, err := newEvidenceSpool(spoolConfig{root: filepath.Join(link, "spool"), maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour}); err == nil {
		t.Fatal("symlinked root parent accepted")
	}
}

func TestEvidenceSpoolListingConsumersDoNotFollowSubstitution(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	outside := filepath.Join(t.TempDir(), "outside")
	body := bytes.Repeat([]byte("o"), 8192)
	if err := os.WriteFile(outside, body, 0600); err != nil {
		t.Fatal(err)
	}
	staleTime := time.Now().Add(-2 * time.Hour)
	if err := os.Symlink(outside, filepath.Join(root, "tmp", "substituted.tmp")); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(root, "tmp", "substituted.tmp"), staleTime, staleTime); err != nil {
		t.Fatal(err)
	}
	if err := spool.reconcile(); err != nil {
		t.Fatal(err)
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if ledger.TemporaryBytes != 0 || ledger.TemporaryObjects != 0 {
		t.Fatalf("reconcile followed substituted symlink into the ledger: %+v", ledger)
	}
	if err := spool.sweepStale(time.Now()); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(outside)
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("outside target disturbed by sweep: %v", err)
	}
}

func TestEvidenceSpoolAbandonFailureKeepsReservationAlive(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("permission-based unlink injection requires a non-root tester")
	}
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	body := bytes.Repeat([]byte("x"), 2048)
	tempPath := filepath.Join(root, "tmp", "abandoned.tmp")
	if err := os.WriteFile(tempPath, body, 0600); err != nil {
		t.Fatal(err)
	}
	tmpDir := filepath.Join(root, "tmp")
	if err := os.Chmod(tmpDir, 0500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(tmpDir, 0700)
	// Unlink now fails and the abandoned-transfer ledger read is unreadable,
	// so the whole abandonment must fail instead of silently losing bytes.
	capacityPath := filepath.Join(root, ".control", "capacity.json")
	if err := os.WriteFile(capacityPath, []byte("corrupted"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := spool.removeOrAbandonTemporary(tempPath); err == nil {
		t.Fatal("failed abandonment did not propagate an error")
	}
	if reservation.released {
		t.Fatal("reservation released despite failed abandonment transfer")
	}
	if _, statErr := os.Stat(reservation.path); statErr != nil {
		t.Fatalf("reservation record vanished: %v", statErr)
	}
	if _, statErr := os.Stat(tempPath); statErr != nil {
		t.Fatalf("surviving temporary vanished during failed transfer: %v", statErr)
	}
	// Once the ledger is readable again the transfer succeeds durably and
	// only then may the reservation be released.
	if err := os.Remove(capacityPath); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(tmpDir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := reservation.discardFailedTemporary(tempPath); err != nil {
		t.Fatalf("discard after recovery failed: %v", err)
	}
	if !reservation.released {
		t.Fatal("reservation not released after durable transfer")
	}
}

func TestEvidenceSpoolReconcileDoesNotDoubleCountBoundTemporary(t *testing.T) {
	root := filepath.Join(t.TempDir(), "spool")
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	tempPath := filepath.Join(root, "tmp", "bound.tmp")
	boundBody := bytes.Repeat([]byte("b"), 4096)
	if err := os.WriteFile(tempPath, boundBody, 0600); err != nil {
		t.Fatal(err)
	}
	const limit = 8192
	recordPath := filepath.Join(root, ".control", "reservations", "manual.json")
	recordFile, err := os.OpenFile(recordPath, os.O_RDWR|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer recordFile.Close()
	fmt.Fprintf(recordFile, `{"limit":%d,"temporary_path":%q}`, limit, tempPath)
	if err := syscall.Flock(int(recordFile.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		t.Fatal(err)
	}
	defer func() { _ = syscall.Flock(int(recordFile.Fd()), syscall.LOCK_UN) }()
	if err := spool.reconcile(); err != nil {
		t.Fatal(err)
	}
	ledger, err := spool.ledger()
	if err != nil {
		t.Fatal(err)
	}
	if ledger.ReservedBytes != limit || ledger.ReservedObjects != 1 {
		t.Fatalf("reserved accounting wrong: %+v", ledger)
	}
	if ledger.TemporaryBytes != 0 || ledger.TemporaryObjects != 0 {
		t.Fatalf("live reservation-bound temp double-counted as temporary: %+v", ledger)
	}
	expectedHighWater := ledger.FinalBytes + ledger.TemporaryBytes + ledger.AbandonedTempBytes + ledger.ReservedBytes
	if ledger.HighWaterBytes < expectedHighWater {
		t.Fatalf("high water below components: %+v", ledger)
	}
}

func stubSpoolStatfs(t *testing.T, fsType int64, files, ffree, bavail uint64) {
	t.Helper()
	original := spoolStatfs
	spoolStatfs = func(_ string, stat *syscall.Statfs_t) error {
		stat.Type = fsType
		stat.Bsize = 4096
		stat.Files = files
		stat.Ffree = ffree
		stat.Bavail = bavail
		return nil
	}
	t.Cleanup(func() { spoolStatfs = original })
}

func TestSpoolCapacityClassificationAgreesWithPython(t *testing.T) {
	cases := []struct {
		fsType int64
		files  uint64
		ffree  uint64
		fstype string
		model  string
	}{
		{btrfsMagic, 0, 0, "btrfs", "dynamic"},
		{btrfsMagic, 1000, 10, "btrfs", "dynamic"},
		{ext4Magic, 1000, 999, "ext4", "finite"},
		{ext4Magic, 1000, 0, "ext4", "finite"},
		{xfsMagic, 100, 50, "xfs", "finite"},
		{0x794C7630, 1000, 10, "other", "finite"},
		{ext4Magic, 0, 0, "ext4", "unknown"},
		{0, 1000, 10, "unknown", "finite"},
		{ext4Magic, 1000, 1001, "ext4", "unknown"},
		{ext4Magic, ^uint64(0), ^uint64(0), "ext4", "unknown"},
	}
	for _, tc := range cases {
		fstype := spoolFilesystemType(tc.fsType)
		if fstype != tc.fstype {
			t.Errorf("type %#x = %q, want %q", tc.fsType, fstype, tc.fstype)
		}
		if model := classifySpoolInodeModel(fstype, tc.files, tc.ffree); model != tc.model {
			t.Errorf("model %s %d/%d = %q, want %q", fstype, tc.files, tc.ffree, model, tc.model)
		}
	}
	if got := spoolFilesystemType(0); got != "unknown" {
		t.Errorf("zero type = %q, want unknown", got)
	}
}

func TestSpoolReserveBtrfsSkipsOnlyInodeFloor(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1 << 20, maxObjects: 10, freeSpaceFloor: 1000, freeInodeFloor: 10000, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	// Explicit Btrfs 0/0 passes the inode gate.
	stubSpoolStatfs(t, btrfsMagic, 0, 0, 1<<20)
	reservation, err := spool.reserve(512)
	if err != nil {
		t.Fatalf("btrfs reserve = %v, want success", err)
	}
	if err := reservation.release(); err != nil {
		t.Fatal(err)
	}
	metrics, err := spool.metrics()
	if err != nil || metrics.inodeModel != "dynamic" || metrics.filesystemType != "btrfs" || metrics.freeInodes != 0 {
		t.Fatalf("btrfs metrics = %+v, %v", metrics, err)
	}
	// Low free bytes still blocks Btrfs.
	stubSpoolStatfs(t, btrfsMagic, 0, 0, 0)
	if _, err := spool.reserve(512); !errors.Is(err, errSpoolFreeSpaceFloor) {
		t.Fatalf("btrfs low-bytes reserve = %v, want free-space floor", err)
	}
	if err := (&s3Archive{spool: spool, maximumBodyBytes: 512}).spoolReady(); !errors.Is(err, errSpoolFreeSpaceFloor) {
		t.Fatalf("btrfs low-bytes ready = %v, want free-space floor", err)
	}
}

func TestSpoolReserveRejectsUnknownAndProbeFailure(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	// Unknown non-Btrfs 0/0 must not admit.
	stubSpoolStatfs(t, ext4Magic, 0, 0, 1<<20)
	if _, err := spool.reserve(512); !errors.Is(err, errSpoolUnknownCapacity) {
		t.Fatalf("unknown reserve = %v, want unknown capacity", err)
	}
	if category := archiveFailureCategory(errSpoolUnknownCapacity); category != "degraded_capacity" {
		t.Fatalf("unknown category = %q, want degraded_capacity", category)
	}
	if err := (&s3Archive{spool: spool, maximumBodyBytes: 512}).spoolReady(); !errors.Is(err, errSpoolUnknownCapacity) {
		t.Fatalf("unknown ready = %v, want unknown capacity", err)
	}
	// Inconsistent and sentinel counts are unknown, not finite.
	for _, tc := range [][2]uint64{{1000, 1001}, {^uint64(0), ^uint64(0)}} {
		stubSpoolStatfs(t, ext4Magic, tc[0], tc[1], 1<<20)
		if _, err := spool.reserve(512); !errors.Is(err, errSpoolUnknownCapacity) {
			t.Fatalf("inconsistent %v reserve = %v, want unknown", tc, err)
		}
	}
	// A failed probe must not create a reservation.
	original := spoolStatfs
	spoolStatfs = func(_ string, _ *syscall.Statfs_t) error { return fmt.Errorf("injected statfs failure") }
	defer func() { spoolStatfs = original }()
	before, _ := os.ReadDir(filepath.Join(spool.cfg.root, ".control", "reservations"))
	if _, err := spool.reserve(512); !errors.Is(err, errSpoolCapacity) {
		t.Fatalf("probe-failure reserve = %v, want capacity error", err)
	}
	after, _ := os.ReadDir(filepath.Join(spool.cfg.root, ".control", "reservations"))
	if len(after) != len(before) {
		t.Fatal("probe failure created a reservation")
	}
	if _, err := spool.metrics(); err == nil {
		t.Fatal("probe-failure metrics succeeded, want error")
	}
}

func TestSpoolReserveFiniteZeroFloorStillRequiresOneInode(t *testing.T) {
	spool, err := newEvidenceSpool(spoolConfig{root: filepath.Join(t.TempDir(), "spool"), maxBytes: 1 << 20, maxObjects: 10, staleTempAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	defer spool.close()
	stubSpoolStatfs(t, ext4Magic, 1000, 0, 1<<20)
	if _, err := spool.reserve(512); !errors.Is(err, errSpoolFreeInodeFloor) {
		t.Fatalf("exhausted finite reserve = %v, want inode floor", err)
	}
	if err := (&s3Archive{spool: spool, maximumBodyBytes: 512}).spoolReady(); !errors.Is(err, errSpoolFreeInodeFloor) {
		t.Fatalf("exhausted finite ready = %v, want inode floor", err)
	}
	stubSpoolStatfs(t, ext4Magic, 1000, 1, 1<<20)
	reservation, err := spool.reserve(512)
	if err != nil {
		t.Fatalf("finite one-free reserve = %v, want success", err)
	}
	_ = reservation.release()
}
