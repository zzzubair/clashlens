package collector

import (
	"context"
	"errors"
	"testing"
	"time"
)

// terminalRetrySeed holds the identifiers of one seeded terminal-parent
// endpoint-retry scenario: an active player, a completed regular-poll root
// job, a complete parent attempt with an observed required endpoint, a due
// pending retry for that endpoint, and a second pending retry for a different
// endpoint that must stay untouched.
type terminalRetrySeed struct {
	rootJobID    int64
	attemptID    int64
	retryJobID   int64
	siblingJobID int64
}

// seedTerminalParentRetry seeds the exact production shape behind the
// endpoint-retry lease churn: a legacy endpoint_retry job whose parent
// collector attempt is already terminal ('complete') and whose required
// endpoint result is already 'observed'. When linkResultAttempt is true the
// retry row carries result_attempt_id = parent_attempt_id (the 1,499 live
// rows); when false it carries NULL (the 3,440 live rows).
func seedTerminalParentRetry(
	t *testing.T,
	ctx context.Context,
	store *store,
	now time.Time,
	linkResultAttempt bool,
) terminalRetrySeed {
	t.Helper()

	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
		RETURNING id
	`, now).Scan(&playerID); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	var rootJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'terminal-debris-root', 'complete')
		RETURNING id
	`, playerID, now).Scan(&rootJobID); err != nil {
		t.Fatalf("insert complete root job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', $2, $2)
		RETURNING id
	`, rootJobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert complete parent attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, rootJobID, attemptID); err != nil {
		t.Fatalf("link root result attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome, request_count)
		VALUES
			($1, 'profile', 'observed', 1),
			($1, 'battle_log', 'observed', 1)
	`, attemptID); err != nil {
		t.Fatalf("insert observed endpoint results: %v", err)
	}

	var retryJobID int64
	if linkResultAttempt {
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, parent_attempt_id, required_endpoint,
				status, result_attempt_id
			) VALUES (
				'endpoint_retry', $1, '#2PP', 'normal', 300,
				$2, 'terminal-debris-profile-retry', $3, 'profile',
				'pending', $3
			) RETURNING id
		`, playerID, now, attemptID).Scan(&retryJobID); err != nil {
			t.Fatalf("insert linked terminal endpoint retry: %v", err)
		}
	} else {
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, parent_attempt_id, required_endpoint,
				status
			) VALUES (
				'endpoint_retry', $1, '#2PP', 'normal', 300,
				$2, 'terminal-debris-profile-retry', $3, 'profile',
				'pending'
			) RETURNING id
		`, playerID, now, attemptID).Scan(&retryJobID); err != nil {
			t.Fatalf("insert terminal endpoint retry: %v", err)
		}
	}
	var siblingJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, parent_attempt_id, required_endpoint, status
		) VALUES (
			'endpoint_retry', $1, '#2PP', 'normal', 300,
			$2, 'terminal-debris-battle-retry', $3, 'battle_log', 'pending'
		) RETURNING id
	`, playerID, now.Add(time.Hour), attemptID).Scan(&siblingJobID); err != nil {
		t.Fatalf("insert sibling endpoint retry: %v", err)
	}
	return terminalRetrySeed{
		rootJobID:    rootJobID,
		attemptID:    attemptID,
		retryJobID:   retryJobID,
		siblingJobID: siblingJobID,
	}
}

// TestTerminalParentEndpointRetryNullResultAttemptCancelsOnPrepare pins the
// production churn shape where the pending endpoint_retry row has
// result_attempt_id NULL: claimNext V2 then prepareAttempt must fence-cancel
// the retry with errJobCancelled and leave the parent evidence untouched.
func TestTerminalParentEndpointRetryNullResultAttemptCancelsOnPrepare(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()

	seed := seedTerminalParentRetry(t, ctx, store, now, false)

	job, err := store.claimNext(ctx, "terminal-worker", normalPool, now, time.Minute, "terminal-token")
	if err != nil {
		t.Fatalf("claimNext returned an error: %v", err)
	}
	if job == nil || job.workType != "endpoint_retry" || job.id != seed.retryJobID {
		t.Fatalf("claimed job = %#v, want endpoint retry %d", job, seed.retryJobID)
	}

	if _, _, err := store.prepareAttempt(ctx, job, now); !errors.Is(err, errJobCancelled) {
		t.Fatalf("prepareAttempt error = %v, want errJobCancelled", err)
	}
	assertTerminalRetryCancellation(t, ctx, store, seed, false)
}

// TestTerminalParentEndpointRetryLinkedResultAttemptCancelsOnPrepare pins the
// production churn shape where the pending endpoint_retry row already carries
// result_attempt_id = parent_attempt_id: claimNext V2 then prepareAttempt must
// fence-cancel the retry with errJobCancelled and preserve the link.
func TestTerminalParentEndpointRetryLinkedResultAttemptCancelsOnPrepare(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()

	seed := seedTerminalParentRetry(t, ctx, store, now, true)

	job, err := store.claimNext(ctx, "terminal-worker", normalPool, now, time.Minute, "terminal-token")
	if err != nil {
		t.Fatalf("claimNext returned an error: %v", err)
	}
	if job == nil || job.workType != "endpoint_retry" || job.id != seed.retryJobID {
		t.Fatalf("claimed job = %#v, want endpoint retry %d", job, seed.retryJobID)
	}

	if _, _, err := store.prepareAttempt(ctx, job, now); !errors.Is(err, errJobCancelled) {
		t.Fatalf("prepareAttempt error = %v, want errJobCancelled", err)
	}
	assertTerminalRetryCancellation(t, ctx, store, seed, true)
}

// assertTerminalRetryCancellation verifies the fence-cancellation contract:
// only the claimed retry changes, and it becomes cancelled/attempt_terminal
// with cleared lease fields, while the parent attempt, endpoint results,
// observations, root job, and the other pending retry stay exactly as seeded.
func assertTerminalRetryCancellation(
	t *testing.T,
	ctx context.Context,
	store *store,
	seed terminalRetrySeed,
	linkResultAttempt bool,
) {
	t.Helper()

	var status string
	var cancelReason *string
	var leaseOwner, leaseToken *string
	var leaseExpiresAt *time.Time
	var resultAttemptID *int64
	var parentAttemptID *int64
	if err := store.pool.QueryRow(ctx, `
		SELECT status, cancel_reason, lease_owner, lease_token, lease_expires_at,
			result_attempt_id, parent_attempt_id
		FROM collector_jobs
		WHERE id = $1
	`, seed.retryJobID).Scan(
		&status, &cancelReason, &leaseOwner, &leaseToken, &leaseExpiresAt,
		&resultAttemptID, &parentAttemptID,
	); err != nil {
		t.Fatalf("read cancelled retry state: %v", err)
	}
	if status != "cancelled" {
		t.Fatalf("retry status = %q, want cancelled", status)
	}
	if cancelReason == nil || *cancelReason != "attempt_terminal" {
		t.Fatalf("retry cancel_reason = %v, want attempt_terminal", cancelReason)
	}
	if leaseOwner != nil || leaseToken != nil || leaseExpiresAt != nil {
		t.Fatalf("retry lease fields not cleared: owner=%v token=%v expires=%v", leaseOwner, leaseToken, leaseExpiresAt)
	}
	if parentAttemptID == nil || *parentAttemptID != seed.attemptID {
		t.Fatalf("retry parent_attempt_id = %v, want %d", parentAttemptID, seed.attemptID)
	}
	if linkResultAttempt {
		if resultAttemptID == nil || *resultAttemptID != seed.attemptID {
			t.Fatalf("retry result_attempt_id = %v, want preserved link %d", resultAttemptID, seed.attemptID)
		}
	} else if resultAttemptID != nil {
		t.Fatalf("retry result_attempt_id = %v, want preserved NULL", resultAttemptID)
	}

	var rootStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, seed.rootJobID).Scan(&rootStatus); err != nil {
		t.Fatalf("read root job state: %v", err)
	}
	if rootStatus != "complete" {
		t.Fatalf("root job status = %q, want complete", rootStatus)
	}

	var attemptStatus string
	var completedAt *time.Time
	if err := store.pool.QueryRow(ctx, `
		SELECT status, completed_at FROM collector_attempts WHERE id = $1
	`, seed.attemptID).Scan(&attemptStatus, &completedAt); err != nil {
		t.Fatalf("read parent attempt state: %v", err)
	}
	if attemptStatus != "complete" || completedAt == nil {
		t.Fatalf("parent attempt = %q (completed %v), want complete with completed_at", attemptStatus, completedAt)
	}

	var outcome string
	var requestCount int
	var observationID *int64
	var retryCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT outcome, request_count, observation_id, retry_count
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = 'profile'
	`, seed.attemptID).Scan(&outcome, &requestCount, &observationID, &retryCount); err != nil {
		t.Fatalf("read endpoint result state: %v", err)
	}
	if outcome != "observed" || requestCount != 1 || observationID != nil || retryCount != 0 {
		t.Fatalf(
			"endpoint result = %q request_count %d observation %v retry_count %d; want observed/1/nil/0",
			outcome, requestCount, observationID, retryCount,
		)
	}

	var attemptCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_attempts WHERE job_id = $1
	`, seed.rootJobID).Scan(&attemptCount); err != nil {
		t.Fatalf("count collector attempts: %v", err)
	}
	if attemptCount != 1 {
		t.Fatalf("collector attempt count = %d, want 1 (no new attempt)", attemptCount)
	}

	var observationCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_observations WHERE attempt_id = $1
	`, seed.attemptID).Scan(&observationCount); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if observationCount != 0 {
		t.Fatalf("observation count = %d, want 0", observationCount)
	}

	var eventCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_attempt_events WHERE attempt_id = $1
	`, seed.attemptID).Scan(&eventCount); err != nil {
		t.Fatalf("count attempt events: %v", err)
	}
	if eventCount != 0 {
		t.Fatalf("attempt event count = %d, want 0 (no claimed event)", eventCount)
	}

	var siblingStatus string
	var siblingReason *string
	if err := store.pool.QueryRow(ctx, `
		SELECT status, cancel_reason FROM collector_jobs WHERE id = $1
	`, seed.siblingJobID).Scan(&siblingStatus, &siblingReason); err != nil {
		t.Fatalf("read sibling retry state: %v", err)
	}
	if siblingStatus != "pending" || siblingReason != nil {
		t.Fatalf("sibling retry = %q (%v), want untouched pending", siblingStatus, siblingReason)
	}
}

// TestGenuineEndpointRetryPreparesAgainstIncompleteParent pins the positive
// path: a retry whose parent attempt is still incomplete and whose required
// endpoint is still retrying must prepare normally (return the endpoint work),
// create no new attempt, and stay leased.
func TestGenuineEndpointRetryPreparesAgainstIncompleteParent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()

	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
		RETURNING id
	`, now).Scan(&playerID); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	var rootJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'genuine-retry-root', 'waiting_retry')
		RETURNING id
	`, playerID, now).Scan(&rootJobID); err != nil {
		t.Fatalf("insert waiting root job: %v", err)
	}
	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at)
		VALUES ($1, 'incomplete', $2)
		RETURNING id
	`, rootJobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert incomplete parent attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET result_attempt_id = $2 WHERE id = $1
	`, rootJobID, attemptID); err != nil {
		t.Fatalf("link root result attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome, retry_count, next_retry_at)
		VALUES ($1, 'profile', 'retrying', 1, $2)
	`, attemptID, now.Add(time.Minute)); err != nil {
		t.Fatalf("insert retrying endpoint: %v", err)
	}
	var retryJobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, parent_attempt_id, required_endpoint, status
		) VALUES (
			'endpoint_retry', $1, '#2PP', 'normal', 300,
			$2, 'genuine-profile-retry', $3, 'profile', 'pending'
		) RETURNING id
	`, playerID, now, attemptID).Scan(&retryJobID); err != nil {
		t.Fatalf("insert genuine endpoint retry: %v", err)
	}

	job, err := store.claimNext(ctx, "genuine-worker", normalPool, now, time.Minute, "genuine-token")
	if err != nil {
		t.Fatalf("claimNext returned an error: %v", err)
	}
	if job == nil || job.workType != "endpoint_retry" || job.id != retryJobID {
		t.Fatalf("claimed job = %#v, want endpoint retry %d", job, retryJobID)
	}

	preparedAttemptID, endpoints, err := store.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepareAttempt returned an error: %v", err)
	}
	if preparedAttemptID != attemptID {
		t.Fatalf("prepared attempt = %d, want parent attempt %d", preparedAttemptID, attemptID)
	}
	if len(endpoints) != 1 || endpoints[0] != profileEndpoint {
		t.Fatalf("prepared endpoints = %v, want [profile]", endpoints)
	}

	var status, leaseOwner, leaseToken string
	var resultAttemptID *int64
	if err := store.pool.QueryRow(ctx, `
		SELECT status, lease_owner, lease_token, result_attempt_id
		FROM collector_jobs
		WHERE id = $1
	`, retryJobID).Scan(&status, &leaseOwner, &leaseToken, &resultAttemptID); err != nil {
		t.Fatalf("read prepared retry state: %v", err)
	}
	if status != "leased" || leaseOwner != "genuine-worker" || leaseToken != "genuine-token" {
		t.Fatalf("prepared retry = %q owner %q token %q, want leased by genuine-worker", status, leaseOwner, leaseToken)
	}
	if resultAttemptID == nil || *resultAttemptID != attemptID {
		t.Fatalf("prepared retry result_attempt_id = %v, want %d", resultAttemptID, attemptID)
	}

	var attemptCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_attempts WHERE job_id = $1
	`, rootJobID).Scan(&attemptCount); err != nil {
		t.Fatalf("count collector attempts: %v", err)
	}
	if attemptCount != 1 {
		t.Fatalf("collector attempt count = %d, want 1 (no new attempt)", attemptCount)
	}
}
