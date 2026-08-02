package collector

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"
)

func TestTerminalEndpointFailureCancelsSiblingRetryWork(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)
	now := time.Now().UTC()

	var rootJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status
		) VALUES ('live_refresh', '#2PP', 'interactive', 250, $1, 'terminal-root', 'waiting_retry')
		RETURNING id
	`, now).Scan(&rootJobID); err != nil {
		t.Fatalf("insert root job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at)
		VALUES ($1, 'incomplete', $2)
		RETURNING id
	`, rootJobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, rootJobID, attemptID); err != nil {
		t.Fatalf("link root attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome, retry_count)
		VALUES
			($1, 'profile', 'transport_failed', 1),
			($1, 'battle_log', 'retrying', 1)
	`, attemptID); err != nil {
		t.Fatalf("create endpoint state: %v", err)
	}

	var terminalRetryID, siblingRetryID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key,
			parent_attempt_id, required_endpoint, status, lease_owner, lease_token, lease_expires_at
		) VALUES (
			'endpoint_retry', '#2PP', 'interactive', 300, $1, 'terminal-profile-retry',
			$2, 'profile', 'leased', 'terminal-owner', 'terminal-token', $3
		) RETURNING id
	`, now, attemptID, now.Add(time.Minute)).Scan(&terminalRetryID); err != nil {
		t.Fatalf("insert terminal retry: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, normalized_tag, capacity_pool, priority, due_at, coalescing_key,
			parent_attempt_id, required_endpoint, status
		) VALUES (
			'endpoint_retry', '#2PP', 'interactive', 300, $1, 'sibling-battle-retry',
			$2, 'battle_log', 'pending'
		) RETURNING id
	`, now, attemptID).Scan(&siblingRetryID); err != nil {
		t.Fatalf("insert sibling retry: %v", err)
	}

	job := &collectionJob{
		id:               terminalRetryID,
		workType:         "endpoint_retry",
		normalizedTag:    "#2PP",
		pool:             interactivePool,
		parentAttemptID:  pgtype.Int8{Int64: attemptID, Valid: true},
		requiredEndpoint: pgtype.Text{String: string(profileEndpoint), Valid: true},
		leaseToken:       "terminal-token",
	}
	if err := store.resolveAttempt(ctx, job, attemptID, now, 1); err != nil {
		t.Fatalf("resolveAttempt returned an error: %v", err)
	}

	var rootStatus, siblingStatus string
	var siblingReason *string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, rootJobID).Scan(&rootStatus); err != nil {
		t.Fatalf("read root job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status, cancel_reason FROM collector_jobs WHERE id = $1`, siblingRetryID).Scan(&siblingStatus, &siblingReason); err != nil {
		t.Fatalf("read sibling retry status: %v", err)
	}
	if rootStatus != "failed" || siblingStatus != "cancelled" || siblingReason == nil || *siblingReason != "attempt_terminal" {
		t.Fatalf("terminal state = root %q, sibling %q (%v); want failed and cancelled", rootStatus, siblingStatus, siblingReason)
	}
}
