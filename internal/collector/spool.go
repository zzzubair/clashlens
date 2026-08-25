package collector

// The spool is deliberately boring filesystem code. It is the cross-runtime
// contract: Python uses the same names and flock protocol.

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
)

const spoolStripeCount = 4096

// Capacity sentinels classify admission failures as retryable degraded
// capacity instead of generic storage failure (see archiveFailureCategory).
var (
	errSpoolCapacity       = errors.New("spool backpressure: capacity reservation denied")
	errSpoolFreeSpaceFloor = errors.New("spool backpressure: free-space floor reached")
	errSpoolFreeInodeFloor = errors.New("spool backpressure: free-inode floor reached")
)

// archiveFailureCategory maps raw-evidence errors into the collector failure
// categories without collapsing them into one storage failure. Capacity and
// ENOSPC errors are retryable degraded capacity; dependencyDeferralCategory
// treats them as non-consuming deferrals.
func archiveFailureCategory(err error) string {
	switch {
	case errors.Is(err, errArchiveChecksumMismatch):
		return "archive_checksum_mismatch"
	case errors.Is(err, errArchiveCatalogueContradiction):
		return "archive_catalogue_contradiction"
	case errors.Is(err, errArchiveTerminal):
		return "archive_terminal_configuration"
	case errors.Is(err, errSpoolCapacity),
		errors.Is(err, errSpoolFreeSpaceFloor),
		errors.Is(err, errSpoolFreeInodeFloor),
		errors.Is(err, syscall.ENOSPC):
		return "degraded_capacity"
	default:
		return "archive_write_failed"
	}
}

type spoolConfig struct {
	root           string
	maxBytes       int64
	maxObjects     int64
	freeSpaceFloor uint64
	freeInodeFloor uint64
	staleTempAge   time.Duration
}

type localEvidence struct {
	Hash string
	Size int64
	Path string
}
type spoolReservation struct {
	spool    *evidenceSpool
	file     *os.File
	path     string
	limit    int64
	released bool
}
type spoolFaults struct {
	// writeFileErr replaces the temp-body write result (short write, ENOSPC).
	writeFileErr error
	fileSyncErr  error
	dirSyncErr   error
	promoteErr   error
}

type spoolLedger struct {
	FinalBytes           int64 `json:"final_bytes"`
	FinalObjects         int64 `json:"final_objects"`
	TemporaryBytes       int64 `json:"temporary_bytes"`
	TemporaryObjects     int64 `json:"temporary_objects"`
	AbandonedTempBytes   int64 `json:"abandoned_temp_bytes"`
	AbandonedTempObjects int64 `json:"abandoned_temp_objects"`
	ReservedBytes        int64 `json:"reserved_bytes"`
	ReservedObjects      int64 `json:"reserved_objects"`
	// ReservedInodes is retained as a read-compatible alias for old ledgers.
	ReservedInodes int64 `json:"reserved_inodes,omitempty"`
	HighWaterBytes int64 `json:"high_water_bytes"`
}

type evidenceSpool struct {
	cfg spoolConfig
	// locks[i] is the flock inode for stripe i. flock reentrancy applies per
	// open file description, so one shared descriptor cannot arbitrate
	// concurrent in-process lockers; stripeMu serializes those first and the
	// flock keeps cross-process (Go vs Python) exclusion.
	locks      []*os.File
	stripeMu   [spoolStripeCount]sync.Mutex
	capacity   *os.File
	capacityMu sync.Mutex
	// faults is nil in production; tests set it to inject filesystem errors.
	faults *spoolFaults
}

func validateSpoolRoot(root string) error {
	if root == "" {
		return errors.New("spool root is required")
	}
	if !filepath.IsAbs(root) || filepath.Clean(root) == "/" {
		return errors.New("spool root must be an absolute non-root path")
	}
	info, err := os.Lstat(root)
	if err != nil {
		if !os.IsNotExist(err) {
			return fmt.Errorf("inspect spool root: %w", err)
		}
		return nil
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return errors.New("spool root must be a real directory")
	}
	return nil
}

func newEvidenceSpool(cfg spoolConfig) (*evidenceSpool, error) {
	if cfg.maxBytes <= 0 || cfg.maxObjects <= 0 {
		return nil, errors.New("spool byte and object limits must be positive")
	}
	if cfg.staleTempAge <= 0 {
		return nil, errors.New("spool stale temporary age must be positive")
	}
	if err := validateSpoolRoot(cfg.root); err != nil {
		return nil, err
	}
	for _, dir := range []string{cfg.root, filepath.Join(cfg.root, ".locks"), filepath.Join(cfg.root, ".control"), filepath.Join(cfg.root, ".control", "reservations"), filepath.Join(cfg.root, ".control", "operations"), filepath.Join(cfg.root, "tmp"), filepath.Join(cfg.root, "sha256")} {
		if info, statErr := os.Lstat(dir); statErr == nil && info.Mode()&os.ModeSymlink != 0 {
			return nil, errors.New("symlink beneath spool root")
		}
		if err := os.MkdirAll(dir, 0700); err != nil {
			return nil, fmt.Errorf("create spool directory: %w", err)
		}
		if err := os.Chmod(dir, 0700); err != nil {
			return nil, fmt.Errorf("protect spool directory: %w", err)
		}
	}
	capacity, err := os.OpenFile(filepath.Join(cfg.root, ".control", "capacity.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	result := &evidenceSpool{cfg: cfg, capacity: capacity}
	for i := 0; i < spoolStripeCount; i++ {
		file, err := os.OpenFile(filepath.Join(cfg.root, ".locks", fmt.Sprintf("%04x", i)), os.O_CREATE|os.O_RDWR, 0600)
		if err != nil {
			result.close()
			return nil, err
		}
		result.locks = append(result.locks, file)
	}
	if err := result.reconcile(); err != nil {
		result.close()
		return nil, err
	}
	return result, nil
}

func (s *evidenceSpool) close() {
	if s.capacity != nil {
		_ = s.capacity.Close()
	}
	for _, file := range s.locks {
		_ = file.Close()
	}
}
func (s *evidenceSpool) stripe(hash string) (*os.File, error) {
	if len(hash) < 3 {
		return nil, errors.New("invalid evidence hash")
	}
	value, err := strconv.ParseUint(hash[:3], 16, 16)
	if err != nil {
		return nil, err
	}
	return s.locks[value&0xfff], nil
}
func (s *evidenceSpool) heldStripeIndex(stripe *os.File) (int, error) {
	for index, file := range s.locks {
		if file == stripe {
			return index, nil
		}
	}
	return 0, errors.New("held spool stripe is not owned by this spool")
}

func (s *evidenceSpool) finalPath(hash string) string {
	return filepath.Join(s.cfg.root, "sha256", hash[:2], hash)
}
func safeSpoolPath(root, path string) error {
	relative, err := filepath.Rel(root, path)
	if err != nil || strings.HasPrefix(relative, "..") {
		return errors.New("spool path escapes root")
	}
	current := root
	for _, part := range strings.Split(relative, string(os.PathSeparator)) {
		current = filepath.Join(current, part)
		info, statErr := os.Lstat(current)
		if statErr == nil && info.Mode()&os.ModeSymlink != 0 {
			return errors.New("symlink beneath spool root")
		}
	}
	return nil
}
func (s *evidenceSpool) lockCapacity() error {
	s.capacityMu.Lock()
	if err := syscall.Flock(int(s.capacity.Fd()), syscall.LOCK_EX); err != nil {
		s.capacityMu.Unlock()
		return err
	}
	return nil
}

func (s *evidenceSpool) unlockCapacity() {
	_ = syscall.Flock(int(s.capacity.Fd()), syscall.LOCK_UN)
	s.capacityMu.Unlock()
}

func (s *evidenceSpool) lock(file *os.File, exclusive bool) error {
	mode := syscall.LOCK_SH
	if exclusive {
		mode = syscall.LOCK_EX
	}
	return syscall.Flock(int(file.Fd()), mode)
}
func unlock(file *os.File) { _ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN) }

func (s *evidenceSpool) stripeIndex(hash string) (int, error) {
	if len(hash) < 3 {
		return 0, errors.New("invalid evidence hash")
	}
	value, err := strconv.ParseUint(hash[:3], 16, 16)
	if err != nil {
		return 0, err
	}
	return int(value & 0xfff), nil
}

func (s *evidenceSpool) lockStripe(index int, exclusive bool) error {
	mode := syscall.LOCK_SH
	if exclusive {
		mode = syscall.LOCK_EX
	}
	s.stripeMu[index].Lock()
	if err := syscall.Flock(int(s.locks[index].Fd()), mode); err != nil {
		s.stripeMu[index].Unlock()
		return err
	}
	return nil
}

func (s *evidenceSpool) unlockStripe(index int) {
	_ = syscall.Flock(int(s.locks[index].Fd()), syscall.LOCK_UN)
	s.stripeMu[index].Unlock()
}
func syncDir(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	return file.Sync()
}

// syncDir is the fault-injection seam for directory fsync failures.
func (s *evidenceSpool) syncDir(path string) error {
	if s.faults != nil && s.faults.dirSyncErr != nil {
		if relative, relErr := filepath.Rel(s.cfg.root, path); relErr == nil && !strings.HasPrefix(relative, ".control") {
			return s.faults.dirSyncErr
		}
	}
	fd, err := openSpoolRelative(s.cfg.root, path, syscall.O_RDONLY|syscall.O_DIRECTORY, 0)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fd), path)
	if file == nil {
		_ = syscall.Close(fd)
		return errors.New("open spool directory")
	}
	defer file.Close()
	return file.Sync()
}

// readFinalNoFollow reads a final object without ever traversing a symlink:
// an O_NOFOLLOW open fails with ELOOP instead of following the link.
func (s *evidenceSpool) readFinalNoFollow(path string) ([]byte, error) {
	fileDescriptor, openErr := openSpoolRelative(s.cfg.root, path, syscall.O_RDONLY, 0)
	if openErr != nil {
		return nil, openErr
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	if file == nil {
		return nil, errors.New("open evidence final file")
	}
	defer file.Close()
	return io.ReadAll(file)
}

func (s *evidenceSpool) ledger() (spoolLedger, error) {
	body, err := readSpoolRelative(s.cfg.root, filepath.Join(".control", "capacity.json"), 0)
	if os.IsNotExist(err) {
		return spoolLedger{}, nil
	}
	if err != nil {
		return spoolLedger{}, err
	}
	var value spoolLedger
	if err := json.Unmarshal(body, &value); err != nil {
		return spoolLedger{}, err
	}
	return value, nil
}
func (s *evidenceSpool) writeLedger(value spoolLedger) error {
	body, _ := json.Marshal(value)
	path := filepath.Join(s.cfg.root, ".control", "capacity.json")
	// Unique per-writer name: two runtimes may publish concurrently.
	temp := path + "." + uuid.NewString() + ".tmp"
	fileDescriptor, err := openSpoolRelative(s.cfg.root, temp, os.O_WRONLY|syscall.O_CREAT|syscall.O_EXCL, 0600)
	if err != nil {
		return err
	}
	file := os.NewFile(uintptr(fileDescriptor), temp)
	if file == nil {
		_ = syscall.Close(fileDescriptor)
		_ = unlinkSpoolRelative(s.cfg.root, temp)
		return errors.New("create ledger temporary file")
	}
	if _, err := file.Write(body); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, temp)
		return err
	}
	if err = file.Sync(); err == nil {
		err = file.Close()
	} else {
		_ = file.Close()
	}
	if err != nil {
		return err
	}
	if err = renameSpoolRelative(s.cfg.root, temp, path); err != nil {
		return err
	}
	return s.syncDir(filepath.Dir(path))
}

func (s *evidenceSpool) reconcile() error {
	for _, file := range s.locks {
		if err := s.lock(file, true); err != nil {
			return err
		}
	}
	defer func() {
		for i := len(s.locks) - 1; i >= 0; i-- {
			unlock(s.locks[i])
		}
	}()
	if err := s.lockCapacity(); err != nil {
		return err
	}
	defer s.unlockCapacity()
	var ledger spoolLedger
	prefixes, err := listSpoolRelative(s.cfg.root, "sha256")
	if err != nil {
		return err
	}
	for _, prefix := range prefixes {
		if !prefix.IsDir() || len(prefix.Name()) != 2 {
			continue
		}
		entries, _ := listSpoolRelative(s.cfg.root, filepath.Join("sha256", prefix.Name()))
		for _, entry := range entries {
			if entry.IsDir() || len(entry.Name()) != 64 {
				continue
			}
			info, e := statSpoolRelative(s.cfg.root, filepath.Join("sha256", prefix.Name(), entry.Name()), false)
			if e == nil && info.Mode().IsRegular() {
				ledger.FinalBytes += info.Size()
				ledger.FinalObjects++
			}
		}
	}
	entries, _ := listSpoolRelative(s.cfg.root, "tmp")
	for _, entry := range entries {
		info, e := entry.Info()
		if e == nil {
			ledger.TemporaryBytes += info.Size()
			ledger.TemporaryObjects++
		}
	}
	records, _ := listSpoolRelative(s.cfg.root, filepath.Join(".control", "reservations"))
	for _, entry := range records {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		path := filepath.Join(".control", "reservations", entry.Name())
		file, openErr := openReservationRecordRelative(s.cfg.root, path)
		if openErr != nil {
			continue
		}
		if lockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); lockErr == nil {
			_ = file.Close()
			_ = unlinkSpoolRelative(s.cfg.root, path)
			continue
		}
		_ = file.Close()
		ledger.ReservedObjects++
		var record struct {
			Limit int64 `json:"limit"`
		}
		if body, readErr := readSpoolRelative(s.cfg.root, path, 1<<20); readErr == nil && json.Unmarshal(body, &record) == nil {
			ledger.ReservedBytes += record.Limit
		}
	}
	operationTempBytes, operationTempObjects := int64(0), int64(0)
	operations, _ := listSpoolRelative(s.cfg.root, filepath.Join(".control", "operations"))
	for _, entry := range operations {
		path := filepath.Join(".control", "operations", entry.Name())
		file, openErr := openReservationRecordRelative(s.cfg.root, path)
		if openErr != nil {
			continue
		}
		if lockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); lockErr == nil {
			var record struct {
				TemporaryPath string `json:"temporary_path"`
			}
			if body, readErr := readSpoolRelative(s.cfg.root, path, 1<<20); readErr == nil && json.Unmarshal(body, &record) == nil && record.TemporaryPath != "" {
				if safeErr := safeSpoolPath(s.cfg.root, record.TemporaryPath); safeErr == nil {
					info, statErr := statSpoolRelative(s.cfg.root, record.TemporaryPath, true)
					if statErr == nil {
						operationTempBytes += info.Size()
						operationTempObjects++
					}
					_ = unlinkSpoolRelative(s.cfg.root, record.TemporaryPath)
				}
			}
			_ = file.Close()
			_ = unlinkSpoolRelative(s.cfg.root, path)
			continue
		}
		_ = file.Close()
	}
	ledger.TemporaryBytes = maxInt64(0, ledger.TemporaryBytes-operationTempBytes)
	ledger.TemporaryObjects = maxInt64(0, ledger.TemporaryObjects-operationTempObjects)
	ledger.ReservedInodes = ledger.ReservedObjects
	ledger.HighWaterBytes = ledger.FinalBytes + ledger.TemporaryBytes + ledger.AbandonedTempBytes + ledger.ReservedBytes
	return s.writeLedger(ledger)
}

func (s *evidenceSpool) reservationLocked() bool {
	entries, err := os.ReadDir(filepath.Join(s.cfg.root, ".control", "reservations"))
	if err != nil {
		return false
	}
	for _, entry := range entries {
		file, openErr := os.OpenFile(filepath.Join(s.cfg.root, ".control", "reservations", entry.Name()), os.O_RDWR, 0600)
		if openErr != nil {
			continue
		}
		lockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		_ = file.Close()
		if lockErr != nil {
			return true
		}
	}
	return false
}

func (s *evidenceSpool) deadReservationExists() bool {
	entries, err := os.ReadDir(filepath.Join(s.cfg.root, ".control", "reservations"))
	if err != nil {
		return false
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		file, openErr := os.OpenFile(filepath.Join(s.cfg.root, ".control", "reservations", entry.Name()), os.O_RDWR, 0600)
		if openErr != nil {
			continue
		}
		lockErr := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if lockErr == nil {
			_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
			_ = file.Close()
			return true
		}
		_ = file.Close()
	}
	return false
}

func maxInt64(left, right int64) int64 {
	if left > right {
		return left
	}
	return right
}

func (s *evidenceSpool) reserve(limit int64) (*spoolReservation, error) {
	if limit <= 0 || limit > s.cfg.maxBytes {
		return nil, errors.New("spool reservation exceeds configured body limit")
	}
	if err := s.lockCapacity(); err != nil {
		return nil, err
	}
	hasDeadReservation := s.deadReservationExists()
	s.unlockCapacity()
	if hasDeadReservation {
		_ = s.reconcile()
		return nil, errors.New("spool backpressure: dead reservation requires reconciliation")
	}
	if err := s.lockCapacity(); err != nil {
		return nil, err
	}
	defer s.unlockCapacity()
	ledger, err := s.ledger()
	if err != nil {
		return nil, err
	}
	reservedBytes := ledger.ReservedBytes
	reservedObjects := ledger.ReservedObjects
	if reservedObjects == 0 && ledger.ReservedInodes > 0 {
		reservedObjects = ledger.ReservedInodes
		reservedBytes = reservedObjects * limit
	}
	if ledger.FinalBytes+ledger.TemporaryBytes+ledger.AbandonedTempBytes+reservedBytes+limit > s.cfg.maxBytes || ledger.FinalObjects+ledger.TemporaryObjects+reservedObjects+1 > s.cfg.maxObjects {
		return nil, errSpoolCapacity
	}
	if s.cfg.freeSpaceFloor > 0 || s.cfg.freeInodeFloor > 0 {
		var stat syscall.Statfs_t
		if err := syscall.Statfs(s.cfg.root, &stat); err == nil {
			if s.cfg.freeSpaceFloor > 0 && stat.Bavail*uint64(stat.Bsize) < s.cfg.freeSpaceFloor+uint64(limit) {
				return nil, errSpoolFreeSpaceFloor
			}
			if s.cfg.freeInodeFloor > 0 && stat.Ffree < s.cfg.freeInodeFloor+1 {
				return nil, errSpoolFreeInodeFloor
			}
		}
	}
	name := uuid.NewString() + ".json"
	path := filepath.Join(s.cfg.root, ".control", "reservations", name)
	fileDescriptor, err := openSpoolRelative(s.cfg.root, path, syscall.O_CREAT|syscall.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	if file == nil {
		_ = syscall.Close(fileDescriptor)
		return nil, errors.New("open spool reservation")
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, err
	}
	record := fmt.Sprintf(`{"limit":%d,"created_at":%d}`, limit, time.Now().UnixNano())
	if _, err := file.WriteString(record); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, err
	}
	if err := s.syncDir(filepath.Dir(path)); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, err
	}
	ledger.ReservedObjects++
	ledger.ReservedBytes += limit
	ledger.ReservedInodes = ledger.ReservedObjects
	ledger.HighWaterBytes = maxInt64(ledger.HighWaterBytes, ledger.FinalBytes+ledger.TemporaryBytes+ledger.AbandonedTempBytes+ledger.ReservedBytes)
	if err := s.writeLedger(ledger); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, err
	}
	return &spoolReservation{spool: s, file: file, path: path, limit: limit}, nil
}
func (r *spoolReservation) release() error {
	if r == nil || r.released {
		return nil
	}
	if err := r.spool.lockCapacity(); err != nil {
		return err
	}
	defer r.spool.unlockCapacity()
	return r.releaseLocked()
}
func (r *spoolReservation) releaseLocked() error {
	if r == nil || r.released {
		return nil
	}
	ledger, err := r.spool.ledger()
	if err != nil {
		return err
	}
	if ledger.ReservedObjects > 0 {
		ledger.ReservedObjects--
	}
	if ledger.ReservedBytes >= r.limit {
		ledger.ReservedBytes -= r.limit
	} else {
		ledger.ReservedBytes = 0
	}
	ledger.ReservedInodes = ledger.ReservedObjects
	if err := r.spool.writeLedger(ledger); err != nil {
		return err
	}
	if err := syscall.Flock(int(r.file.Fd()), syscall.LOCK_UN); err != nil {
		return err
	}
	if err := r.file.Close(); err != nil {
		return err
	}
	r.released = true
	if err := unlinkSpoolRelative(r.spool.cfg.root, r.path); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := r.spool.syncDir(filepath.Dir(r.path)); err != nil {
		return err
	}
	return nil
}

func (s *evidenceSpool) beginOperation(hash, temporaryPath string) (*os.File, string, error) {
	path := filepath.Join(s.cfg.root, ".control", "operations", uuid.NewString()+".json")
	fileDescriptor, err := openSpoolRelative(s.cfg.root, path, syscall.O_CREAT|syscall.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return nil, "", err
	}
	file := os.NewFile(uintptr(fileDescriptor), path)
	if file == nil {
		_ = syscall.Close(fileDescriptor)
		return nil, "", errors.New("open spool operation")
	}
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, "", err
	}
	if _, err := fmt.Fprintf(file, `{"operation":"publish","hash":%q,"temporary_path":%q}`, hash, temporaryPath); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, "", err
	}
	if err := file.Sync(); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, "", err
	}
	if err := s.syncDir(filepath.Dir(path)); err != nil {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, path)
		return nil, "", err
	}
	return file, path, nil
}

func (s *evidenceSpool) finishOperation(file *os.File, path string) error {
	if file == nil {
		return nil
	}
	_ = syscall.Flock(int(file.Fd()), syscall.LOCK_UN)
	if err := file.Close(); err != nil {
		return err
	}
	// The record lives at .control/operations/<name>; its parent directory is
	// .control, which is the correct sync target after the unlink below.
	if err := unlinkSpoolRelative(s.cfg.root, path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return syncDir(filepath.Dir(path))
}

func (s *evidenceSpool) write(reservation *spoolReservation, body io.Reader, heldStripe ...*os.File) (localEvidence, error) {
	if reservation == nil || reservation.spool != s {
		return localEvidence{}, errors.New("invalid spool reservation")
	}
	tempPath := filepath.Join(s.cfg.root, "tmp", uuid.NewString()+".tmp")
	fileDescriptor, err := openSpoolRelative(s.cfg.root, tempPath, syscall.O_CREAT|syscall.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		_ = reservation.release()
		return localEvidence{}, err
	}
	file := os.NewFile(uintptr(fileDescriptor), tempPath)
	if file == nil {
		_ = syscall.Close(fileDescriptor)
		_ = reservation.release()
		return localEvidence{}, errors.New("open spool temporary file")
	}
	hash := sha256.New()
	written, err := io.CopyN(io.MultiWriter(file, hash), body, reservation.limit+1)
	if err == io.EOF {
		err = nil
	}
	if err == nil && s.faults != nil && s.faults.writeFileErr != nil {
		err = s.faults.writeFileErr
	}
	if err != nil || written > reservation.limit {
		_ = file.Close()
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.release()
		if written > reservation.limit {
			err = errors.New("evidence body exceeds configured limit")
		}
		return localEvidence{}, err
	}
	fileSyncErr := error(nil)
	if s.faults != nil {
		fileSyncErr = s.faults.fileSyncErr
	}
	if fileSyncErr == nil {
		fileSyncErr = file.Sync()
	}
	if closeErr := file.Close(); fileSyncErr == nil {
		fileSyncErr = closeErr
	}
	if fileSyncErr != nil || err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.release()
		return localEvidence{}, fileSyncErr
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	stripeIndex, err := s.stripeIndex(digest)
	if err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.release()
		return localEvidence{}, err
	}
	heldIndex := -1
	if len(heldStripe) > 0 {
		heldIndex, err = s.heldStripeIndex(heldStripe[0])
		if err != nil || heldIndex != stripeIndex {
			_ = unlinkSpoolRelative(s.cfg.root, tempPath)
			_ = reservation.release()
			return localEvidence{}, errors.New("held spool stripe does not match evidence hash")
		}
	}
	if heldIndex != stripeIndex {
		if err := s.lockStripe(stripeIndex, true); err != nil {
			_ = unlinkSpoolRelative(s.cfg.root, tempPath)
			_ = reservation.release()
			return localEvidence{}, err
		}
		defer s.unlockStripe(stripeIndex)
	}
	operation, operationPath, err := s.beginOperation(digest, tempPath)
	if err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.release()
		return localEvidence{}, err
	}
	defer func() { _ = s.finishOperation(operation, operationPath) }()
	if err := s.lockCapacity(); err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.release()
		return localEvidence{}, err
	}
	defer s.unlockCapacity()
	final := s.finalPath(digest)
	if err := safeSpoolPath(s.cfg.root, final); err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.releaseLocked()
		return localEvidence{}, err
	}
	if err := mkdirAllSpoolRelative(s.cfg.root, filepath.Dir(final), 0700); err != nil {
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		_ = reservation.releaseLocked()
		return localEvidence{}, err
	}
	_, statErr := statSpoolRelative(s.cfg.root, filepath.Join("sha256", digest[:2], digest), false)
	created := os.IsNotExist(statErr)
	promote := func() error {
		if s.faults != nil && s.faults.promoteErr != nil {
			return s.faults.promoteErr
		}
		return linkSpoolRelative(s.cfg.root, tempPath, final)
	}
	var replacedSize int64
	if created {
		if err := promote(); err != nil && !os.IsExist(err) {
			_ = unlinkSpoolRelative(s.cfg.root, tempPath)
			_ = reservation.releaseLocked()
			return localEvidence{}, err
		}
		_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		if err := s.syncDir(filepath.Dir(final)); err != nil {
			_ = reservation.releaseLocked()
			return localEvidence{}, err
		}
	} else {
		// Hash-verify the winning inode without traversing symlinks; a
		// corrupt or unreadable winner is replaced under the exclusive stripe.
		winner, readErr := s.readFinalNoFollow(final)
		winnerDigest := sha256.Sum256(winner)
		if readErr != nil || int64(len(winner)) != written || hex.EncodeToString(winnerDigest[:]) != digest {
			replacedSize = int64(len(winner))
			if removeErr := unlinkSpoolRelative(s.cfg.root, final); removeErr != nil && !os.IsNotExist(removeErr) {
				_ = reservation.releaseLocked()
				return localEvidence{}, removeErr
			}
			if err := promote(); err != nil {
				_ = unlinkSpoolRelative(s.cfg.root, tempPath)
				_ = reservation.releaseLocked()
				return localEvidence{}, err
			}
			_ = unlinkSpoolRelative(s.cfg.root, tempPath)
			if err := s.syncDir(filepath.Dir(final)); err != nil {
				_ = reservation.releaseLocked()
				return localEvidence{}, err
			}
		} else {
			_ = unlinkSpoolRelative(s.cfg.root, tempPath)
		}
	}
	ledger, err := s.ledger()
	if err != nil {
		_ = reservation.releaseLocked()
		return localEvidence{}, err
	}
	if created {
		ledger.FinalBytes += written
		ledger.FinalObjects++
	} else if replacedSize != 0 {
		ledger.FinalBytes += written - replacedSize
	}
	if ledger.ReservedObjects > 0 {
		ledger.ReservedObjects--
	}
	if ledger.ReservedBytes >= reservation.limit {
		ledger.ReservedBytes -= reservation.limit
	} else {
		ledger.ReservedBytes = 0
	}
	ledger.ReservedInodes = ledger.ReservedObjects
	ledger.HighWaterBytes = maxInt64(ledger.HighWaterBytes, ledger.FinalBytes+ledger.TemporaryBytes+ledger.AbandonedTempBytes+ledger.ReservedBytes)
	if err := s.writeLedger(ledger); err != nil {
		return localEvidence{}, err
	}
	if err := syscall.Flock(int(reservation.file.Fd()), syscall.LOCK_UN); err != nil {
		return localEvidence{}, err
	}
	if err := reservation.file.Close(); err != nil {
		return localEvidence{}, err
	}
	reservation.released = true
	if err := unlinkSpoolRelative(s.cfg.root, reservation.path); err != nil && !os.IsNotExist(err) {
		return localEvidence{}, err
	}
	if err := s.syncDir(filepath.Dir(reservation.path)); err != nil {
		return localEvidence{}, err
	}
	return localEvidence{Hash: digest, Size: written, Path: final}, nil
}

func (s *evidenceSpool) verify(hash string, expectedSize int64, heldStripe ...*os.File) (bool, error) {
	stripeIndex, err := s.stripeIndex(hash)
	if err != nil {
		return false, err
	}
	heldIndex := -1
	if len(heldStripe) > 0 {
		heldIndex, err = s.heldStripeIndex(heldStripe[0])
		if err != nil || heldIndex != stripeIndex {
			return false, errors.New("held spool stripe does not match evidence hash")
		}
	}
	if heldIndex != stripeIndex {
		if err := s.lockStripe(stripeIndex, false); err != nil {
			return false, err
		}
		defer s.unlockStripe(stripeIndex)
	}
	if err := safeSpoolPath(s.cfg.root, s.finalPath(hash)); err != nil {
		return false, err
	}
	fileDescriptor, openErr := openSpoolRelative(s.cfg.root, s.finalPath(hash), syscall.O_RDONLY, 0)
	if openErr != nil && os.IsNotExist(openErr) {
		return false, nil
	}
	if openErr != nil {
		return false, openErr
	}
	file := os.NewFile(uintptr(fileDescriptor), s.finalPath(hash))
	if file == nil {
		return false, errors.New("open evidence final file")
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info.Size() != expectedSize {
		return false, nil
	}
	digest := sha256.New()
	if _, err := io.CopyN(digest, file, expectedSize+1); err != nil && !errors.Is(err, io.EOF) {
		return false, err
	}
	return hex.EncodeToString(digest.Sum(nil)) == hash, nil
}

func (s *evidenceSpool) read(hash string, expectedSize int64) ([]byte, bool, error) {
	ok, err := s.verify(hash, expectedSize)
	if err != nil || !ok {
		return nil, ok, err
	}
	fileDescriptor, openErr := openSpoolRelative(s.cfg.root, s.finalPath(hash), syscall.O_RDONLY, 0)
	if openErr != nil {
		return nil, false, openErr
	}
	file := os.NewFile(uintptr(fileDescriptor), s.finalPath(hash))
	if file == nil {
		return nil, false, errors.New("open evidence final file")
	}
	defer file.Close()
	body, readErr := io.ReadAll(io.LimitReader(file, expectedSize+1))
	return body, readErr == nil, readErr
}
func (s *evidenceSpool) sweepStale(now time.Time) error {
	if err := s.lockCapacity(); err != nil {
		return err
	}
	defer s.unlockCapacity()
	entries, err := listSpoolRelative(s.cfg.root, "tmp")
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if s.reservationLocked() {
		return nil
	}
	removed := int64(0)
	removedBytes := int64(0)
	for _, e := range entries {
		info, er := e.Info()
		if er != nil || now.Sub(info.ModTime()) <= s.cfg.staleTempAge {
			continue
		}
		temporaryPath := filepath.Join(s.cfg.root, "tmp", e.Name())
		if removeErr := unlinkSpoolRelative(s.cfg.root, temporaryPath); removeErr == nil {
			removed++
			removedBytes += info.Size()
		}
	}
	if removed > 0 {
		ledger, ledgerErr := s.ledger()
		if ledgerErr != nil {
			return ledgerErr
		}
		ledger.TemporaryObjects = maxInt64(0, ledger.TemporaryObjects-removed)
		ledger.TemporaryBytes = maxInt64(0, ledger.TemporaryBytes-removedBytes)
		ledger.AbandonedTempObjects = maxInt64(0, ledger.AbandonedTempObjects-removed)
		ledger.AbandonedTempBytes = maxInt64(0, ledger.AbandonedTempBytes-removedBytes)
		if err := s.writeLedger(ledger); err != nil {
			return err
		}
		return s.syncDir(filepath.Join(s.cfg.root, "tmp"))
	}
	return nil
}
