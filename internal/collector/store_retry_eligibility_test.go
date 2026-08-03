package collector

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

func TestRegularPollEndpointRetryCancelsWhenPlayerBecomesInactive(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	now := time.Now().UTC()

	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#2PP', true)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert player: %v", err)
	}
	var rootJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'retry-eligibility-root', 'waiting_retry')
		RETURNING id
	`, playerID, now).Scan(&rootJobID); err != nil {
		t.Fatalf("insert root job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at)
		VALUES ($1, 'incomplete', $2)
		RETURNING id
	`, rootJobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert parent attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, rootJobID, attemptID); err != nil {
		t.Fatalf("link root attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome, retry_count)
		VALUES ($1, 'profile', 'retrying', 1)
	`, attemptID); err != nil {
		t.Fatalf("insert retrying endpoint: %v", err)
	}
	var firstRetryJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, parent_attempt_id, required_endpoint, status
		) VALUES (
			'endpoint_retry', $1, '#2PP', 'normal', 300,
			$2, 'retry-eligibility-child', $3, 'profile', 'pending'
		)
		RETURNING id
	`, playerID, now, attemptID).Scan(&firstRetryJobID); err != nil {
		t.Fatalf("insert endpoint retry: %v", err)
	}
	var concurrentRetryJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, parent_attempt_id, required_endpoint, status,
			lease_owner, lease_token, lease_expires_at
		) VALUES (
			'endpoint_retry', $1, '#2PP', 'normal', 300,
			$2, 'retry-eligibility-concurrent', $3, 'profile', 'leased',
			'concurrent-worker', 'concurrent-token', $4
		)
		RETURNING id
	`, playerID, now, attemptID, now.Add(time.Minute)).Scan(&concurrentRetryJobID); err != nil {
		t.Fatalf("insert concurrent endpoint retry: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `UPDATE players SET active = false WHERE id = $1`, playerID); err != nil {
		t.Fatalf("deactivate player: %v", err)
	}

	job, err := store.claimNext(ctx, "eligibility-worker", normalPool, now, time.Minute, "eligibility-token")
	if err != nil {
		t.Fatalf("claimNext returned an error: %v", err)
	}
	if job == nil || job.workType != "endpoint_retry" {
		t.Fatalf("claimed job = %#v, want endpoint retry", job)
	}
	if _, _, err := store.prepareAttempt(ctx, job, now); !errors.Is(err, errJobCancelled) {
		t.Fatalf("prepareAttempt error = %v, want errJobCancelled", err)
	}
	concurrentJob := &collectionJob{
		id:               concurrentRetryJobID,
		workType:         "endpoint_retry",
		playerID:         pgtype.Int8{Int64: playerID, Valid: true},
		normalizedTag:    "#2PP",
		pool:             normalPool,
		parentAttemptID:  pgtype.Int8{Int64: attemptID, Valid: true},
		requiredEndpoint: pgtype.Text{String: string(profileEndpoint), Valid: true},
		leaseToken:       "concurrent-token",
	}
	if _, _, err := store.prepareAttempt(ctx, concurrentJob, now); !errors.Is(err, errJobCancelled) {
		t.Fatalf("concurrent prepareAttempt error = %v, want errJobCancelled", err)
	}

	var status string
	var reason *string
	if err := store.pool.QueryRow(ctx, `
		SELECT status, cancel_reason FROM collector_jobs WHERE id = $1
	`, job.id).Scan(&status, &reason); err != nil {
		t.Fatalf("read retry state: %v", err)
	}
	if status != "cancelled" || reason == nil || *reason != "player_inactive" {
		t.Fatalf("retry state = %q (%v), want cancelled (player_inactive)", status, reason)
	}

	var rootStatus, attemptStatus, endpointOutcome string
	if err := store.pool.QueryRow(ctx, `
		SELECT root.status, attempt.status, endpoint_result.outcome
		FROM collector_jobs AS root
		JOIN collector_attempts AS attempt ON attempt.id = root.result_attempt_id
		JOIN collector_endpoint_results AS endpoint_result ON endpoint_result.attempt_id = attempt.id
		WHERE root.id = $1
	`, rootJobID).Scan(&rootStatus, &attemptStatus, &endpointOutcome); err != nil {
		t.Fatalf("read root retry state: %v", err)
	}
	if rootStatus != "cancelled" || attemptStatus != "failed" || endpointOutcome != "failed" {
		t.Fatalf(
			"root retry state = job %q, attempt %q, endpoint %q; want cancelled, failed, failed",
			rootStatus,
			attemptStatus,
			endpointOutcome,
		)
	}
	var remainingLeasedRetries int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM collector_jobs
		WHERE parent_attempt_id = $1 AND status = 'leased'
	`, attemptID).Scan(&remainingLeasedRetries); err != nil {
		t.Fatalf("count leased retries: %v", err)
	}
	if remainingLeasedRetries != 0 {
		t.Fatalf("leased retries = %d, want 0", remainingLeasedRetries)
	}
}
