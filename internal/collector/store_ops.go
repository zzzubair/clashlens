package collector

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

type queueStatistics struct {
	depth                          int64
	oldestDueAt                    pgtype.Timestamptz
	activeLeases                   int64
	expiredLeases                  int64
	failedJobs                     int64
	waitingRetries                 int64
	waitingDependencies            int64
	pendingRemoteVerifications     int64
	incompleteAttempts             int64
	latestProfileAt                pgtype.Timestamptz
	latestBattleLogAt              pgtype.Timestamptz
	latestLiveRefreshLatencySecond pgtype.Float8
	liveRefreshCoalesced           int64
	liveRefreshCooldownHits        int64
	resetMembers                   int64
	resetObserved                  int64
	resetCreatedAt                 pgtype.Timestamptz
}

type failedWork struct {
	jobID           int64
	workType        string
	capacityPool    string
	attemptID       pgtype.Int8
	failureCategory pgtype.Text
	updatedAt       time.Time
}

type stuckLease struct {
	jobID     int64
	workType  string
	owner     string
	expiredAt time.Time
}

func (s *store) ready(ctx context.Context) error {
	if err := s.pool.Ping(ctx); err != nil {
		return fmt.Errorf("PostgreSQL readiness check: %w", err)
	}
	var version int
	if err := s.pool.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&version); err != nil {
		return fmt.Errorf("read shared schema contract during readiness check: %w", err)
	}
	if !supportsContractVersion(version, s.maxContractVersion) {
		return fmt.Errorf("%w: got %d, support through %d", errIncompatibleContract, version, s.maxContractVersion)
	}
	return nil
}

func (s *store) queueStatistics(ctx context.Context) (queueStatistics, error) {
	var statistics queueStatistics
	if err := s.pool.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE status = 'pending'),
			min(due_at) FILTER (WHERE status = 'pending'),
			count(*) FILTER (WHERE status = 'leased' AND lease_expires_at > now()),
			count(*) FILTER (WHERE status = 'leased' AND lease_expires_at <= now()),
			count(*) FILTER (WHERE status = 'failed'),
			count(*) FILTER (WHERE status = 'waiting_retry'),
			count(*) FILTER (WHERE status = 'waiting_dependency')
		FROM collector_jobs
	`).Scan(
		&statistics.depth,
		&statistics.oldestDueAt,
		&statistics.activeLeases,
		&statistics.expiredLeases,
		&statistics.failedJobs,
		&statistics.waitingRetries,
		&statistics.waitingDependencies,
	); err != nil {
		return queueStatistics{}, fmt.Errorf("read collector queue statistics: %w", err)
	}
	if s.contractVersion >= 3 {
		if err := s.pool.QueryRow(ctx, `SELECT count(*) FROM collector_endpoint_results WHERE outcome = 'pending_remote_verification'`).Scan(&statistics.pendingRemoteVerifications); err != nil {
			return queueStatistics{}, fmt.Errorf("read pending raw-evidence statistics: %w", err)
		}
	}
	if err := s.pool.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE status = 'incomplete'),
			(SELECT max(response_completed_at) FROM collector_observations WHERE endpoint = 'profile'),
			(SELECT max(response_completed_at) FROM collector_observations WHERE endpoint = 'battle_log'),
			(
				SELECT extract(epoch FROM (attempt.completed_at - attempt.started_at))
				FROM collector_attempts AS attempt
				JOIN collector_jobs AS job ON job.id = attempt.job_id
				WHERE job.work_type = 'live_refresh' AND attempt.status = 'complete'
				ORDER BY attempt.completed_at DESC, attempt.id DESC
				LIMIT 1
			),
			(SELECT count(*) FROM collector_interactive_intent_events WHERE requested_work_type = 'live_refresh' AND outcome = 'coalesced'),
			(SELECT count(*) FROM collector_interactive_intent_events WHERE requested_work_type = 'live_refresh' AND outcome = 'cooldown_hit')
		FROM collector_attempts
	`).Scan(
		&statistics.incompleteAttempts,
		&statistics.latestProfileAt,
		&statistics.latestBattleLogAt,
		&statistics.latestLiveRefreshLatencySecond,
		&statistics.liveRefreshCoalesced,
		&statistics.liveRefreshCooldownHits,
	); err != nil {
		return queueStatistics{}, fmt.Errorf("read collector attempt statistics: %w", err)
	}
	err := s.pool.QueryRow(ctx, `
		SELECT
			sweep.created_at,
			count(DISTINCT member.player_id),
			count(DISTINCT member.player_id) FILTER (WHERE job.status = 'complete')
		FROM (
			SELECT id, created_at
			FROM collector_reset_sweeps
			ORDER BY boundary_at DESC
			LIMIT 1
		) AS sweep
		LEFT JOIN collector_reset_sweep_members AS member ON member.sweep_id = sweep.id
		LEFT JOIN collector_jobs AS job
			ON job.sweep_id = sweep.id
			AND job.player_id = member.player_id
			AND job.work_type IN ('reset_baseline', 'legacy_reset_profile')
		GROUP BY sweep.id, sweep.created_at
	`).Scan(&statistics.resetCreatedAt, &statistics.resetMembers, &statistics.resetObserved)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return queueStatistics{}, fmt.Errorf("read reset sweep statistics: %w", err)
	}
	return statistics, nil
}

func (s *store) listStuckLeases(ctx context.Context, now time.Time, limit int) ([]stuckLease, error) {
	if limit < 1 {
		return nil, errors.New("stuck lease list limit must be positive")
	}
	rows, err := s.pool.Query(ctx, `
		SELECT id, work_type, lease_owner, lease_expires_at
		FROM collector_jobs
		WHERE status = 'leased' AND lease_expires_at <= $1
		ORDER BY lease_expires_at, id
		LIMIT $2
	`, now, limit)
	if err != nil {
		return nil, fmt.Errorf("query stuck collector leases: %w", err)
	}
	defer rows.Close()

	var leases []stuckLease
	for rows.Next() {
		var lease stuckLease
		if err := rows.Scan(&lease.jobID, &lease.workType, &lease.owner, &lease.expiredAt); err != nil {
			return nil, fmt.Errorf("scan stuck collector lease: %w", err)
		}
		leases = append(leases, lease)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate stuck collector leases: %w", err)
	}
	return leases, nil
}

func (s *store) listFailedWork(ctx context.Context, limit int) ([]failedWork, error) {
	if limit < 1 {
		return nil, errors.New("failure list limit must be positive")
	}
	rows, err := s.pool.Query(ctx, `
		SELECT
			job.id,
			job.work_type,
			job.capacity_pool,
			job.result_attempt_id,
			(
				SELECT endpoint.failure_category
				FROM collector_endpoint_results AS endpoint
				WHERE endpoint.attempt_id = job.result_attempt_id
					AND endpoint.failure_category IS NOT NULL
				ORDER BY endpoint.endpoint
				LIMIT 1
			),
			job.updated_at
		FROM collector_jobs AS job
		WHERE job.status = 'failed'
		ORDER BY job.updated_at DESC, job.id DESC
		LIMIT $1
	`, limit)
	if err != nil {
		return nil, fmt.Errorf("query failed collector work: %w", err)
	}
	defer rows.Close()

	var failures []failedWork
	for rows.Next() {
		var failure failedWork
		if err := rows.Scan(
			&failure.jobID,
			&failure.workType,
			&failure.capacityPool,
			&failure.attemptID,
			&failure.failureCategory,
			&failure.updatedAt,
		); err != nil {
			return nil, fmt.Errorf("scan failed collector work: %w", err)
		}
		failures = append(failures, failure)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate failed collector work: %w", err)
	}
	return failures, nil
}

func (s *store) requeueFailedJob(ctx context.Context, jobID int64, now time.Time) error {
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin failed-job requeue transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var attemptID pgtype.Int8
	var status string
	if err := transaction.QueryRow(ctx, `
		SELECT status, result_attempt_id FROM collector_jobs WHERE id = $1 FOR UPDATE
	`, jobID).Scan(&status, &attemptID); err != nil {
		return fmt.Errorf("lock failed collector job: %w", err)
	}
	if status != "failed" {
		return fmt.Errorf("collector job %d has status %q, not failed", jobID, status)
	}
	if attemptID.Valid {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_endpoint_results
			SET outcome = CASE WHEN outcome = 'observed' THEN outcome ELSE 'pending' END,
				retry_count = CASE WHEN outcome = 'observed' THEN retry_count ELSE 0 END,
				next_retry_at = NULL,
				failure_category = CASE WHEN outcome = 'observed' THEN failure_category ELSE NULL END,
				execution_token = NULL
			WHERE attempt_id = $1
		`, attemptID); err != nil {
			return fmt.Errorf("reset failed endpoint work: %w", err)
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts
			SET status = 'incomplete', completed_at = NULL
			WHERE id = $1
		`, attemptID); err != nil {
			return fmt.Errorf("reset failed attempt: %w", err)
		}
	}
	if _, err := transaction.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'pending',
			due_at = $2,
			lease_owner = NULL,
			lease_token = NULL,
			lease_expires_at = NULL,
			updated_at = $2
		WHERE id = $1
	`, jobID, now); err != nil {
		return fmt.Errorf("requeue failed collector job: %w", err)
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit failed-job requeue: %w", err)
	}
	return nil
}

func (s *store) resetProcessingJob(ctx context.Context, processingJobID int64, now time.Time) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE python_processing_jobs
		SET status = 'pending', due_at = $2, last_error = NULL, updated_at = $2
		WHERE id = $1 AND status = 'failed'
	`, processingJobID, now)
	if err != nil {
		return fmt.Errorf("reset Python processing job: %w", err)
	}
	if command.RowsAffected() != 1 {
		return fmt.Errorf("Python processing job %d was not failed or did not exist", processingJobID)
	}
	return nil
}

func (s *store) releaseOwnerLeases(ctx context.Context, owner string, now time.Time) error {
	_, err := s.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'pending',
			lease_owner = NULL,
			lease_token = NULL,
			lease_expires_at = NULL,
			due_at = LEAST(due_at, $2),
			updated_at = $2
		WHERE status = 'leased' AND lease_owner = $1
	`, owner, now)
	if err != nil {
		return fmt.Errorf("release collector leases for owner: %w", err)
	}
	return nil
}
