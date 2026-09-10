package collector

import (
	"errors"
	"os"
	"path/filepath"
	"sync"

	"golang.org/x/sys/unix"
)

// Capacity transactions keep the existing cross-process flock and JSON ledger.
// Only concurrent Go callers are grouped: no caller returns before its records,
// directories and the combined ledger are durable. Batches never acquire stripes;
// callers acquire those first, just as Python and reconciliation do.
type spoolCapacityRequest struct {
	apply func(*spoolCapacityBatch) error
	done  chan error
}

type spoolCapacityBatch struct {
	spool *evidenceSpool
	value *spoolLedger
	files []*os.File
	dirs  map[string]bool
}

func (s *evidenceSpool) withCapacity(apply func(*spoolCapacityBatch) error) error {
	request := spoolCapacityRequest{apply: apply, done: make(chan error, 1)}
	s.batchMu.Lock()
	s.batchQueue = append(s.batchQueue, request)
	if !s.batchRunning {
		s.batchRunning = true
		go s.runCapacityBatches()
	}
	s.batchMu.Unlock()
	return <-request.done
}

func (s *evidenceSpool) runCapacityBatches() {
	for {
		s.batchMu.Lock()
		if len(s.batchQueue) == 0 {
			s.batchRunning = false
			s.batchMu.Unlock()
			return
		}
		// Bound lock occupancy so another process can make progress as well.
		count := min(len(s.batchQueue), 64)
		requests := append([]spoolCapacityRequest(nil), s.batchQueue[:count]...)
		s.batchQueue = s.batchQueue[count:]
		s.batchMu.Unlock()
		s.applyCapacityBatch(requests)
	}
}

func (s *evidenceSpool) applyCapacityBatch(requests []spoolCapacityRequest) {
	results := make([]error, len(requests))
	err := s.lockCapacity()
	if err == nil {
		err = s.batchFailure
		if err == nil {
			batch := &spoolCapacityBatch{spool: s, dirs: make(map[string]bool)}
			for i, request := range requests {
				results[i] = request.apply(batch)
			}
			err = batch.flush()
			if err != nil {
				// A failed durability barrier cannot be acknowledged or reused.
				// Explicit reconciliation (or restart) is required before admission.
				s.batchFailure = err
			}
		}
		s.unlockCapacity()
	}
	for i, request := range requests {
		request.done <- errors.Join(results[i], err)
	}
}

func (b *spoolCapacityBatch) ledger() (spoolLedger, error) {
	if b.value != nil {
		return *b.value, nil
	}
	return b.spool.ledger()
}

func (b *spoolCapacityBatch) writeLedger(value spoolLedger) error {
	b.value = &value
	return nil
}

func (b *spoolCapacityBatch) syncDir(path string) error {
	b.dirs[filepath.Clean(path)] = true
	return nil
}

func (b *spoolCapacityBatch) syncFile(file *os.File) error {
	// Own the duplicate until the barrier: the transaction may close its copy.
	fd, err := unix.FcntlInt(file.Fd(), unix.F_DUPFD_CLOEXEC, 0)
	if err != nil {
		return err
	}
	b.files = append(b.files, os.NewFile(uintptr(fd), file.Name()))
	return nil
}

func (b *spoolCapacityBatch) flush() error {
	var result error
	// Issue record fsyncs together: on Btrfs, serial calls still serialize
	// log-tree commits even when all file writes preceded the first fsync.
	// At most one record per request (64) is pending in this batch.
	results := make([]error, len(b.files))
	var pending sync.WaitGroup
	for i, file := range b.files {
		pending.Go(func() {
			results[i] = errors.Join(file.Sync(), file.Close())
		})
	}
	pending.Wait()
	result = errors.Join(results...)
	for dir := range b.dirs {
		result = errors.Join(result, b.spool.syncDir(dir))
	}
	if result == nil && b.value != nil {
		result = b.spool.writeLedger(*b.value)
	}
	return result
}
