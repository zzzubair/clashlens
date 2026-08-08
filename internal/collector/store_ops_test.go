package collector

import (
	"context"
	"testing"
	"time"
)

func TestListStuckLeasesReturnsOnlyExpiredCollectorLeases(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	now := time.Now().UTC()
	var expiredID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key,
			status, lease_owner, lease_token, lease_expires_at
		) VALUES (
			'live_refresh', '#2PP', 'interactive', 250, $1, 'expired-lease',
			'leased', 'expired-owner', 'expired-token', $2
		) RETURNING id
	`, now.Add(-time.Minute), now.Add(-time.Second)).Scan(&expiredID); err != nil {
		t.Fatalf("insert expired lease: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key,
			status, lease_owner, lease_token, lease_expires_at
		) VALUES (
			'live_refresh', '#2PQ', 'interactive', 250, $1, 'active-lease',
			'leased', 'active-owner', 'active-token', $2
		)
	`, now.Add(-time.Minute), now.Add(time.Minute)); err != nil {
		t.Fatalf("insert active lease: %v", err)
	}

	leases, err := store.listStuckLeases(ctx, now, 10)
	if err != nil {
		t.Fatalf("listStuckLeases returned an error: %v", err)
	}
	if len(leases) != 1 {
		t.Fatalf("stuck lease count = %d, want 1", len(leases))
	}
	if leases[0].jobID != expiredID || leases[0].owner != "expired-owner" {
		t.Fatalf("stuck lease = %+v, want job %d owned by expired-owner", leases[0], expiredID)
	}
	if !leases[0].expiredAt.Before(now) {
		t.Fatalf("stuck lease expiry = %s, want before %s", leases[0].expiredAt, now)
	}
}

func TestResetProcessingJobRequeuesFailedWorkForExistingObservation(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	now := time.Now().UTC()

	var jobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, $1, 'processing-replay', 'complete')
		RETURNING id
	`, now).Scan(&jobID); err != nil {
		t.Fatalf("insert completed collector job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', $2, $2)
		RETURNING id
	`, jobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert completed attempt: %v", err)
	}
	var observationID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, normalized_tag, endpoint,
			request_started_at, response_completed_at, http_status, response_hash,
			archive_reference, collector_version, key_label, evidence_headers
		) VALUES (
			'processing-replay-observation', $1, $2, '#2PP', 'profile',
			$3, $3, 200, repeat('a', 64), 'sha256/aa', 'test', 'test-key', '{}'::jsonb
		) RETURNING id
	`, jobID, attemptID, now).Scan(&observationID); err != nil {
		t.Fatalf("insert observation: %v", err)
	}
	var processingJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO python_processing_jobs (observation_id, status, last_error)
		VALUES ($1, 'failed', 'temporary failure')
		RETURNING id
	`, observationID).Scan(&processingJobID); err != nil {
		t.Fatalf("insert failed processing job: %v", err)
	}

	requeueAt := now.Add(time.Minute)
	if err := store.resetProcessingJob(ctx, processingJobID, requeueAt); err != nil {
		t.Fatalf("resetProcessingJob returned an error: %v", err)
	}
	var status string
	var dueAt time.Time
	var lastError *string
	if err := store.pool.QueryRow(ctx, `
		SELECT status, due_at, last_error
		FROM python_processing_jobs
		WHERE id = $1
	`, processingJobID).Scan(&status, &dueAt, &lastError); err != nil {
		t.Fatalf("read reset processing job: %v", err)
	}
	if status != "pending" || dueAt.Sub(requeueAt).Abs() > time.Microsecond || lastError != nil {
		t.Fatalf("reset processing job = status %q, due %s, error %v", status, dueAt, lastError)
	}
}
