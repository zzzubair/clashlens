package collector

import (
	"context"
	"errors"
	"testing"
	"time"
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
			$2, 'retry-eligibility-child-1', $3, 'profile', 'waiting_retry'
		)
		RETURNING id
	`, playerID, now, attemptID).Scan(&firstRetryJobID); err != nil {
		t.Fatalf("insert first endpoint retry: %v", err)
	}
	var firstRetryAttemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at)
		VALUES ($1, 'incomplete', $2)
		RETURNING id
	`, firstRetryJobID, now).Scan(&firstRetryAttemptID); err != nil {
		t.Fatalf("insert first retry attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, firstRetryJobID, firstRetryAttemptID); err != nil {
		t.Fatalf("link first retry attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, parent_attempt_id, required_endpoint, status
		) VALUES (
			'endpoint_retry', $1, '#2PP', 'normal', 300,
			$2, 'retry-eligibility-child-2', $3, 'profile', 'pending'
		)
	`, playerID, now, firstRetryAttemptID); err != nil {
		t.Fatalf("insert second endpoint retry: %v", err)
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
}
