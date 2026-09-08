package collector

import "syscall"

// Named filesystem magics keep capacity classification dependency-free.
// Btrfs is the only type granted the dynamic-inode exemption; every other
// type must supply a valid finite inode pool or be rejected as unknown.
const (
	btrfsMagic = 0x9123683E
	ext4Magic  = 0xEF53
	xfsMagic   = 0x58465342
)

// spoolStatfs is the narrow probe seam for capacity classification tests.
// Production uses syscall.Statfs; tests stub it for Btrfs/unknown cases.
var spoolStatfs = syscall.Statfs

type spoolMetrics struct {
	finalBytes                int64
	temporaryBytes            int64
	abandonedTemporaryBytes   int64
	finalObjects              int64
	temporaryObjects          int64
	abandonedTemporaryObjects int64
	reservedBytes             int64
	reservedObjects           int64
	highWaterBytes            int64
	allocatedBytes            uint64
	freeBytes                 uint64
	freeInodes                uint64
	filesystemType            string
	inodeModel                string
}

func spoolFilesystemType(fsType int64) string {
	switch fsType {
	case btrfsMagic:
		return "btrfs"
	case ext4Magic:
		return "ext4"
	case xfsMagic:
		return "xfs"
	case 0:
		return "unknown"
	default:
		return "other"
	}
}

func classifySpoolInodeModel(filesystemType string, files, ffree uint64) string {
	// Explicit Btrfs identification is the only dynamic exemption.
	if filesystemType == "btrfs" {
		return "dynamic"
	}
	const maxUint64 = ^uint64(0)
	// Non-Btrfs 0/0, unavailable counts, sentinel values and inconsistent
	// measurements are unknown: they must reject admission, never admit.
	if files == 0 || files == maxUint64 || ffree == maxUint64 {
		return "unknown"
	}
	if ffree > files {
		return "unknown"
	}
	return "finite"
}

func probeSpoolCapacity(root string) (filesystemType, inodeModel string, freeBytes, freeInodes uint64, err error) {
	var stat syscall.Statfs_t
	if err := spoolStatfs(root, &stat); err != nil {
		return "", "", 0, 0, err
	}
	filesystemType = spoolFilesystemType(stat.Type)
	inodeModel = classifySpoolInodeModel(filesystemType, stat.Files, stat.Ffree)
	freeBytes = stat.Bavail * uint64(stat.Bsize)
	freeInodes = stat.Ffree
	return filesystemType, inodeModel, freeBytes, freeInodes, nil
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
	filesystemType, inodeModel, freeBytes, freeInodes, err := probeSpoolCapacity(s.cfg.root)
	if err != nil {
		return spoolMetrics{}, err
	}
	return spoolMetrics{
		finalBytes: ledger.FinalBytes, temporaryBytes: ledger.TemporaryBytes,
		abandonedTemporaryBytes: ledger.AbandonedTempBytes,
		finalObjects:            ledger.FinalObjects, temporaryObjects: ledger.TemporaryObjects,
		abandonedTemporaryObjects: ledger.AbandonedTempObjects,
		reservedBytes:             ledger.ReservedBytes, reservedObjects: ledger.ReservedObjects,
		highWaterBytes: ledger.HighWaterBytes,
		allocatedBytes: uint64(ledger.FinalBytes + ledger.TemporaryBytes + ledger.AbandonedTempBytes + ledger.ReservedBytes),
		freeBytes:      freeBytes,
		freeInodes:     freeInodes,
		filesystemType: filesystemType,
		inodeModel:     inodeModel,
	}, nil
}
