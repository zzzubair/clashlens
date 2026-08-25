package collector

import "syscall"

type spoolMetrics struct {
	finalBytes       int64
	temporaryBytes   int64
	finalObjects     int64
	temporaryObjects int64
	reservedBytes    int64
	reservedObjects  int64
	highWaterBytes   int64
	allocatedBytes   uint64
	freeInodes       uint64
}

func (s *evidenceSpool) metrics() (spoolMetrics, error) {
	if err := s.lockCapacity(); err != nil {
		return spoolMetrics{}, err
	}
	defer s.unlockCapacity()
	ledger, err := s.ledger()
	if err != nil {
		return spoolMetrics{}, err
	}
	var stat syscall.Statfs_t
	if err := syscall.Statfs(s.cfg.root, &stat); err != nil {
		return spoolMetrics{}, err
	}
	return spoolMetrics{
		finalBytes: ledger.FinalBytes, temporaryBytes: ledger.TemporaryBytes,
		finalObjects: ledger.FinalObjects, temporaryObjects: ledger.TemporaryObjects,
		reservedBytes: ledger.ReservedBytes, reservedObjects: ledger.ReservedObjects,
		highWaterBytes: ledger.HighWaterBytes,
		allocatedBytes: uint64(ledger.FinalBytes + ledger.TemporaryBytes + ledger.ReservedBytes),
		freeInodes:     stat.Ffree,
	}, nil
}
