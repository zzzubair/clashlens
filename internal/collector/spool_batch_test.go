package collector

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

func TestSpoolCapacityBatchLimitsAndDurability(t *testing.T) {
	spool := newFaultTestSpool(t)
	spool.cfg.maxBytes = 2048
	var reservations [3]*spoolReservation
	requests := make([]spoolCapacityRequest, len(reservations))
	for i := range requests {
		requests[i] = spoolCapacityRequest{
			apply: func(batch *spoolCapacityBatch) error {
				var err error
				reservations[i], err = spool.reserveLocked(batch, 1024)
				return err
			},
			done: make(chan error, 1),
		}
	}
	spool.applyCapacityBatch(requests)
	for i, request := range requests {
		err := <-request.done
		if i < 2 && err != nil || i == 2 && !errors.Is(err, errSpoolCapacity) {
			t.Fatalf("reservation %d: %v", i, err)
		}
	}
	ledger := ledgerFinalBytes(t, spool)
	if ledger.ReservedBytes != 2048 || ledger.ReservedObjects != 2 {
		t.Fatalf("batch did not preserve the shared limit: %+v", ledger)
	}
	// A fresh spool must see both live records and exactly the same occupancy.
	other, err := newEvidenceSpool(spool.cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer other.close()
	if _, err := other.reserve(1); !errors.Is(err, errSpoolCapacity) {
		t.Fatalf("another owner admitted beyond the batch limit: %v", err)
	}
	for _, reservation := range reservations[:2] {
		if err := reservation.release(); err != nil {
			t.Fatal(err)
		}
	}
	if ledger := ledgerFinalBytes(t, spool); ledger.ReservedBytes != 0 || ledger.ReservedObjects != 0 {
		t.Fatalf("released capacity was lost: %+v", ledger)
	}
}

func TestSpoolCapacityBatchFailureBlocksAdmissionUntilReconcile(t *testing.T) {
	spool := newFaultTestSpool(t)
	spool.faults = &spoolFaults{dirSyncErr: syscall.EIO}
	requests := make([]spoolCapacityRequest, 2)
	for i := range requests {
		requests[i] = spoolCapacityRequest{
			apply: func(batch *spoolCapacityBatch) error {
				// A failed final-directory barrier must fail every participant,
				// even one whose own callback completed successfully.
				if i == 0 {
					return batch.syncDir(filepath.Join(spool.cfg.root, "sha256"))
				}
				return nil
			},
			done: make(chan error, 1),
		}
	}
	spool.applyCapacityBatch(requests)
	for _, request := range requests {
		if err := <-request.done; !errors.Is(err, syscall.EIO) {
			t.Fatalf("batch acknowledged a failed durability barrier: %v", err)
		}
	}
	spool.faults = nil
	if _, err := spool.reserve(1024); !errors.Is(err, syscall.EIO) {
		t.Fatalf("admission resumed without reconciliation: %v", err)
	}
	if err := spool.reconcile(); err != nil {
		t.Fatal(err)
	}
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	if err := reservation.release(); err != nil {
		t.Fatal(err)
	}
}

func TestSpoolCapacityBatchCrashBeforeBarrier(t *testing.T) {
	if root := os.Getenv("CLASHLENS_BATCH_CRASH_ROOT"); root != "" {
		spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 4 << 20, maxObjects: 100, staleTempAge: time.Hour})
		if err != nil {
			t.Fatal(err)
		}
		_ = spool.withCapacity(func(batch *spoolCapacityBatch) error {
			if _, err := spool.reserveLocked(batch, 1024); err != nil {
				t.Error(err)
				return err
			}
			// Kill the owner after mutation, before the group's durability barrier.
			os.Exit(77)
			return nil
		})
		t.Fatal("crash point not reached")
	}
	spool := newFaultTestSpool(t)
	reservation, err := spool.reserve(1024)
	if err != nil {
		t.Fatal(err)
	}
	body := []byte("previously acknowledged evidence")
	evidence, err := spool.write(reservation, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	child := exec.Command(os.Args[0], "-test.run=^TestSpoolCapacityBatchCrashBeforeBarrier$")
	child.Env = append(os.Environ(), "CLASHLENS_BATCH_CRASH_ROOT="+spool.cfg.root)
	output, err := child.CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 77 {
		t.Fatalf("crash child: %v, %s", err, output)
	}
	if err := spool.reconcile(); err != nil {
		t.Fatal(err)
	}
	ledger := ledgerFinalBytes(t, spool)
	if ledger.ReservedBytes != 0 || ledger.ReservedObjects != 0 || ledger.FinalBytes != int64(len(body)) || ledger.FinalObjects != 1 {
		t.Fatalf("crashed batch lost evidence or capacity: %+v", ledger)
	}
	if ok, err := spool.verify(evidence.Hash, evidence.Size); err != nil || !ok {
		t.Fatalf("acknowledged evidence lost: %v", err)
	}
}

// Run on the actual spool filesystem; this measures durable publication, not
// official API or whole-collector capacity. Every body is distinct.
func BenchmarkSpoolConcurrentEvidence(b *testing.B) {
	root := b.TempDir()
	if parent := os.Getenv("CLASHLENS_SPOOL_BENCH_DIR"); parent != "" {
		var err error
		root, err = os.MkdirTemp(parent, "spool-benchmark-")
		if err != nil {
			b.Fatal(err)
		}
		b.Logf("retained benchmark spool: %s", root)
	}
	spool, err := newEvidenceSpool(spoolConfig{root: root, maxBytes: 16 << 30, maxObjects: 1000000, staleTempAge: time.Hour})
	if err != nil {
		b.Fatal(err)
	}
	defer spool.close()
	var workers sync.WaitGroup
	jobs := make(chan int)
	errorsFound := make(chan error, 32)
	b.ResetTimer()
	for range 32 {
		workers.Go(func() {
			for i := range jobs {
				body := []byte(fmt.Sprintf("%010d%s", i, string(bytes.Repeat([]byte("x"), 10000))))
				reservation, err := spool.reserve(1 << 20)
				if err == nil {
					_, err = spool.write(reservation, bytes.NewReader(body))
				}
				if err != nil {
					errorsFound <- err
					return
				}
			}
		})
	}
	for i := range b.N {
		select {
		case jobs <- i:
		case err := <-errorsFound:
			close(jobs)
			workers.Wait()
			b.Fatal(err)
		}
	}
	close(jobs)
	workers.Wait()
	b.StopTimer()
	close(errorsFound)
	for err := range errorsFound {
		b.Fatal(err)
	}
}
