package collector

import (
	"context"
	"fmt"
	"sync"
	"time"
)

func (s *store) renewLease(ctx context.Context, job *collectionJob, expiresAt time.Time) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_expires_at = $3, updated_at = $3
		WHERE id = $1 AND lease_token = $2 AND status = 'leased'
	`, job.id, job.leaseToken, expiresAt)
	if err != nil {
		return fmt.Errorf("renew collector job lease: %w", err)
	}
	if command.RowsAffected() != 1 {
		var status string
		if err := s.pool.QueryRow(ctx, `
			SELECT status FROM collector_jobs WHERE id = $1
		`, job.id).Scan(&status); err != nil {
			return fmt.Errorf("check completed collector lease: %w", err)
		}
		if status != "leased" {
			return nil
		}
		return errLeaseLost
	}
	return nil
}

func (w *worker) startLeaseHeartbeat(ctx context.Context, job *collectionJob) (context.Context, func() error) {
	if w.config.disableLeaseRenewal {
		return ctx, func() error { return nil }
	}

	jobContext, cancel := context.WithCancel(ctx)
	completed := make(chan error, 1)
	interval := w.config.leaseDuration / 3
	if interval <= 0 {
		interval = time.Millisecond
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-jobContext.Done():
				completed <- nil
				return
			case now := <-ticker.C:
				if err := w.store.renewLease(
					jobContext,
					job,
					now.UTC().Add(w.config.leaseDuration),
				); err != nil {
					if jobContext.Err() != nil {
						completed <- nil
						return
					}
					cancel()
					completed <- fmt.Errorf("renew lease: %w", err)
					return
				}
			}
		}
	}()

	var once sync.Once
	var heartbeatError error
	return jobContext, func() error {
		once.Do(func() {
			cancel()
			heartbeatError = <-completed
		})
		return heartbeatError
	}
}
