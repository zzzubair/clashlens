package collector

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

type interactiveIntentResult struct {
	jobID     int64
	attemptID int64
	reused    bool
}

func (s *store) enqueueInteractive(
	ctx context.Context,
	workType string,
	normalizedTag string,
	now time.Time,
	cooldown time.Duration,
	bypassCooldown bool,
) (interactiveIntentResult, error) {
	if workType != "initial_collection" && workType != "live_refresh" {
		return interactiveIntentResult{}, fmt.Errorf("unsupported interactive work type %q", workType)
	}
	if _, err := officialPlayerPath(normalizedTag); err != nil {
		return interactiveIntentResult{}, err
	}
	if cooldown < 0 {
		return interactiveIntentResult{}, errors.New("interactive cooldown must not be negative")
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return interactiveIntentResult{}, fmt.Errorf("begin interactive intent transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if _, err := transaction.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, normalizedTag); err != nil {
		return interactiveIntentResult{}, fmt.Errorf("lock interactive tag: %w", err)
	}

	var result interactiveIntentResult
	var attemptID pgtype.Int8
	err = transaction.QueryRow(ctx, `
		SELECT id, result_attempt_id
		FROM collector_jobs
		WHERE normalized_tag = $1
			AND capacity_pool = 'interactive'
			AND status IN ('pending', 'leased', 'waiting_retry')
		ORDER BY created_at DESC, id DESC
		LIMIT 1
	`, normalizedTag).Scan(&result.jobID, &attemptID)
	if err == nil {
		result.attemptID = attemptID.Int64
		result.reused = true
		if err := recordInteractiveIntentEvent(ctx, transaction, workType, normalizedTag, now, "coalesced", result); err != nil {
			return interactiveIntentResult{}, err
		}
		if err := transaction.Commit(ctx); err != nil {
			return interactiveIntentResult{}, fmt.Errorf("commit coalesced interactive intent: %w", err)
		}
		return result, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return interactiveIntentResult{}, fmt.Errorf("find active interactive job: %w", err)
	}

	if !bypassCooldown {
		err = transaction.QueryRow(ctx, `
			SELECT job.id, attempt.id
			FROM collector_attempts AS attempt
			JOIN collector_jobs AS job ON job.id = attempt.job_id
			WHERE job.normalized_tag = $1
				AND job.work_type IN ('initial_collection', 'live_refresh')
				AND attempt.status = 'complete'
				AND attempt.completed_at >= $2
			ORDER BY attempt.completed_at DESC, attempt.id DESC
			LIMIT 1
		`, normalizedTag, now.Add(-cooldown)).Scan(&result.jobID, &result.attemptID)
		if err == nil {
			result.reused = true
			if err := recordInteractiveIntentEvent(ctx, transaction, workType, normalizedTag, now, "cooldown_hit", result); err != nil {
				return interactiveIntentResult{}, err
			}
			if err := transaction.Commit(ctx); err != nil {
				return interactiveIntentResult{}, fmt.Errorf("commit interactive cooldown hit: %w", err)
			}
			return result, nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return interactiveIntentResult{}, fmt.Errorf("find recent interactive attempt: %w", err)
		}
	}

	var missingEndpoint pgtype.Text
	var priorAttemptID, priorJobID pgtype.Int8
	err = transaction.QueryRow(ctx, `
		SELECT attempt.id,
			attempt.job_id,
			max(endpoint_result.endpoint) FILTER (WHERE endpoint_result.outcome <> 'observed')
		FROM collector_attempts AS attempt
		JOIN collector_jobs AS job ON job.id = attempt.job_id
		JOIN collector_endpoint_results AS endpoint_result ON endpoint_result.attempt_id = attempt.id
		WHERE job.normalized_tag = $1
			AND job.work_type IN ('initial_collection', 'live_refresh')
			AND attempt.status = 'incomplete'
		GROUP BY attempt.id, attempt.job_id
		HAVING count(*) FILTER (WHERE endpoint_result.outcome = 'observed') = 1
			AND count(*) FILTER (WHERE endpoint_result.outcome <> 'observed') = 1
		ORDER BY attempt.started_at DESC, attempt.id DESC
		LIMIT 1
	`, normalizedTag).Scan(&priorAttemptID, &priorJobID, &missingEndpoint)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return interactiveIntentResult{}, fmt.Errorf("find partial interactive attempt: %w", err)
	}
	if err == nil && missingEndpoint.Valid {
		var playerID pgtype.Int8
		if err := transaction.QueryRow(ctx, `SELECT player_id FROM collector_jobs WHERE id = $1`, priorJobID).Scan(&playerID); err != nil {
			return interactiveIntentResult{}, fmt.Errorf("read partial interactive player: %w", err)
		}
		coalescingKey := fmt.Sprintf("retry:%d:%s:intent", priorAttemptID.Int64, missingEndpoint.String)
		if err := transaction.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority, due_at,
				coalescing_key, parent_attempt_id, required_endpoint, status
			)
			VALUES ('endpoint_retry', $1, $2, 'interactive', 300, $3, $4, $5, $6, 'pending')
			ON CONFLICT DO NOTHING
			RETURNING id
		`, playerID, normalizedTag, now, coalescingKey, priorAttemptID, missingEndpoint).Scan(&result.jobID); err != nil {
			if !errors.Is(err, pgx.ErrNoRows) {
				return interactiveIntentResult{}, fmt.Errorf("create partial interactive retry: %w", err)
			}
			if err := transaction.QueryRow(ctx, `
				SELECT id FROM collector_jobs WHERE coalescing_key = $1 AND status IN ('pending', 'leased', 'waiting_retry')
			`, coalescingKey).Scan(&result.jobID); err != nil {
				return interactiveIntentResult{}, fmt.Errorf("find partial interactive retry: %w", err)
			}
			result.reused = true
		}
		result.attemptID = priorAttemptID.Int64
		if err := recordInteractiveIntentEvent(ctx, transaction, workType, normalizedTag, now, "partial_retry", result); err != nil {
			return interactiveIntentResult{}, err
		}
		if err := transaction.Commit(ctx); err != nil {
			return interactiveIntentResult{}, fmt.Errorf("commit partial interactive retry: %w", err)
		}
		return result, nil
	}

	coalescingKey := "interactive:" + normalizedTag
	err = transaction.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type,
			player_id,
			normalized_tag,
			capacity_pool,
			priority,
			due_at,
			coalescing_key,
			status
		)
		VALUES (
			$1,
			(SELECT id FROM players WHERE normalized_tag = $2),
			$2,
			'interactive',
			250,
			$3,
			$4,
			'pending'
		)
		RETURNING id
	`, workType, normalizedTag, now, coalescingKey).Scan(&result.jobID)
	if err != nil {
		return interactiveIntentResult{}, fmt.Errorf("create interactive job: %w", err)
	}
	if err := recordInteractiveIntentEvent(ctx, transaction, workType, normalizedTag, now, "created", result); err != nil {
		return interactiveIntentResult{}, err
	}
	if err := transaction.Commit(ctx); err != nil {
		return interactiveIntentResult{}, fmt.Errorf("commit interactive intent: %w", err)
	}
	return result, nil
}

func recordInteractiveIntentEvent(
	ctx context.Context,
	transaction pgx.Tx,
	workType string,
	normalizedTag string,
	requestedAt time.Time,
	outcome string,
	result interactiveIntentResult,
) error {
	attemptID := pgtype.Int8{Int64: result.attemptID, Valid: result.attemptID > 0}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_interactive_intent_events (
			requested_work_type, normalized_tag, requested_at, outcome, result_job_id, result_attempt_id
		) VALUES ($1, $2, $3, $4, $5, $6)
	`, workType, normalizedTag, requestedAt, outcome, result.jobID, attemptID); err != nil {
		return fmt.Errorf("record interactive intent event: %w", err)
	}
	return nil
}

func (s *store) scheduleResetSweep(ctx context.Context, boundary time.Time) (int64, bool, error) {
	boundary = boundary.UTC()
	if boundary.Hour() != 5 || boundary.Minute() != 0 || boundary.Second() != 0 || boundary.Nanosecond() != 0 {
		return 0, false, errors.New("reset sweep boundary must be exactly 05:00 UTC")
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, false, fmt.Errorf("begin reset sweep transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var sweepID int64
	err = transaction.QueryRow(ctx, `
		INSERT INTO collector_reset_sweeps (boundary_at)
		VALUES ($1)
		ON CONFLICT (boundary_at) DO NOTHING
		RETURNING id
	`, boundary).Scan(&sweepID)
	if errors.Is(err, pgx.ErrNoRows) {
		if err := transaction.QueryRow(ctx, `
			SELECT id FROM collector_reset_sweeps WHERE boundary_at = $1
		`, boundary).Scan(&sweepID); err != nil {
			return 0, false, fmt.Errorf("find existing reset sweep: %w", err)
		}
		if err := transaction.Commit(ctx); err != nil {
			return 0, false, fmt.Errorf("commit existing reset sweep lookup: %w", err)
		}
		return sweepID, false, nil
	}
	if err != nil {
		return 0, false, fmt.Errorf("create reset sweep: %w", err)
	}

	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_reset_sweep_members (sweep_id, player_id)
		SELECT $1, id FROM players WHERE active
	`, sweepID); err != nil {
		return 0, false, fmt.Errorf("fix reset sweep membership: %w", err)
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type,
			player_id,
			normalized_tag,
			capacity_pool,
			priority,
			due_at,
			coalescing_key,
			sweep_id,
			status
		)
		SELECT
			'reset_profile',
			player.id,
			player.normalized_tag,
			'normal',
			400,
			$2,
			'reset:' || $1::bigint::text || ':' || player.id::text,
			$1::bigint,
			'pending'
		FROM collector_reset_sweep_members AS member
		JOIN players AS player ON player.id = member.player_id
		WHERE member.sweep_id = $1::bigint
	`, sweepID, boundary); err != nil {
		return 0, false, fmt.Errorf("create reset profile jobs: %w", err)
	}

	if err := transaction.Commit(ctx); err != nil {
		return 0, false, fmt.Errorf("commit reset sweep: %w", err)
	}
	return sweepID, true, nil
}

func resetBoundaryAtOrBefore(now time.Time) time.Time {
	now = now.UTC()
	boundary := time.Date(now.Year(), now.Month(), now.Day(), 5, 0, 0, 0, time.UTC)
	if now.Before(boundary) {
		boundary = boundary.Add(-24 * time.Hour)
	}
	return boundary
}
