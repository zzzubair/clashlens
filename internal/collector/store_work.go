package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

var (
	errLeaseLost    = errors.New("collector job lease was lost")
	errJobCancelled = errors.New("collector job was cancelled")
)

type collectionJob struct {
	id                   int64
	workType             string
	scope                string
	playerID             pgtype.Int8
	normalizedTag        string
	pool                 capacityPool
	parentAttemptID      pgtype.Int8
	requiredEndpoint     pgtype.Text
	sweepID              pgtype.Int8
	resetBaselineSweepID pgtype.Int8
	leaseOwner           string
	leaseToken           string
	leaseGeneration      int64
}

func (s *store) claimNext(
	ctx context.Context,
	owner string,
	pool capacityPool,
	now time.Time,
	leaseDuration time.Duration,
	leaseToken string,
) (*collectionJob, error) {
	if owner == "" || leaseToken == "" || leaseDuration <= 0 {
		return nil, errors.New("lease owner, token, and positive duration are required")
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return nil, fmt.Errorf("begin claim transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if _, err := transaction.Exec(ctx, `
		UPDATE collector_jobs AS job
		SET status = 'cancelled',
			cancel_reason = 'player_inactive',
			lease_owner = NULL,
			lease_token = NULL,
			lease_expires_at = NULL,
			updated_at = $1
		FROM players AS player
		WHERE job.player_id = player.id
			AND job.work_type = 'regular_poll'
			AND NOT player.active
			AND (
				job.status = 'pending'
				OR (job.status = 'leased' AND job.lease_expires_at <= $1)
			)
	`, now); err != nil {
		return nil, fmt.Errorf("cancel inactive regular jobs: %w", err)
	}

	if s.contractVersion >= 2 {
		if err := s.recoverExpiredAttemptsV2(ctx, transaction, string(pool), now); err != nil {
			return nil, err
		}
	}

	row := transaction.QueryRow(ctx, `
		WITH candidate AS (
			SELECT id
			FROM collector_jobs
			WHERE capacity_pool = $1
				AND due_at <= $2
				AND (
					status = 'pending'
					OR (status = 'leased' AND lease_expires_at <= $2)
				)
			ORDER BY (
				priority
				+ LEAST(1000, floor(extract(epoch FROM ($2 - created_at)) / 60)::integer * 10)
			) DESC,
				due_at,
				id
			FOR UPDATE SKIP LOCKED
			LIMIT 1
		)
		UPDATE collector_jobs AS job
		SET status = 'leased',
			lease_owner = $3,
			lease_token = $4,
			lease_expires_at = $2 + $5,
			updated_at = $2
		FROM candidate
		WHERE job.id = candidate.id
		RETURNING job.id,
			job.work_type,
			COALESCE(to_jsonb(job) ->> 'scope', 'player'),
			job.player_id,
			COALESCE(job.normalized_tag, ''),
			job.capacity_pool,
			job.parent_attempt_id,
			job.required_endpoint,
			job.sweep_id,
			(to_jsonb(job) ->> 'reset_baseline_sweep_id')::bigint,
			job.lease_owner,
			COALESCE((to_jsonb(job) ->> 'lease_generation')::bigint, 0),
			job.lease_token
	`, string(pool), now, owner, leaseToken, leaseDuration)

	var job collectionJob
	var poolName string
	if err := row.Scan(
		&job.id,
		&job.workType,
		&job.scope,
		&job.playerID,
		&job.normalizedTag,
		&poolName,
		&job.parentAttemptID,
		&job.requiredEndpoint,
		&job.sweepID,
		&job.resetBaselineSweepID,
		&job.leaseOwner,
		&job.leaseGeneration,
		&job.leaseToken,
	); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			if err := transaction.Commit(ctx); err != nil {
				return nil, fmt.Errorf("commit empty claim transaction: %w", err)
			}
			return nil, nil
		}
		return nil, fmt.Errorf("claim collector job: %w", err)
	}
	job.pool = capacityPool(poolName)
	job.leaseOwner = owner
	if s.contractVersion >= 2 {
		var generation int64
		if err := transaction.QueryRow(ctx, `
			SELECT lease_generation
			FROM collector_jobs
			WHERE id = $1
			FOR UPDATE
		`, job.id).Scan(&generation); err != nil {
			return nil, fmt.Errorf("read collector lease generation: %w", err)
		}
		command, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET lease_generation = lease_generation + 1
			WHERE id = $1
			AND lease_owner = $2
			AND lease_token = $3
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		`, job.id, owner, leaseToken)
		if err != nil {
			return nil, fmt.Errorf("advance collector lease generation: %w", err)
		}
		if command.RowsAffected() != 1 {
			return nil, errLeaseLost
		}
		job.leaseGeneration = generation + 1
	}

	if err := transaction.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit claim transaction: %w", err)
	}
	return &job, nil
}

const maxCollectorAttemptsV2 = 3

func (s *store) recoverExpiredAttemptsV2(ctx context.Context, transaction pgx.Tx, pool string, now time.Time) error {
	rows, err := transaction.Query(ctx, `
		SELECT job.id, job.lease_owner, job.lease_token, job.lease_generation,
			job.result_attempt_id
		FROM collector_jobs AS job
		WHERE job.capacity_pool = $1
			AND job.status = 'leased'
			AND job.lease_expires_at <= $2
		ORDER BY job.id
		FOR UPDATE OF job SKIP LOCKED
	`, pool, now)
	if err != nil {
		return fmt.Errorf("select expired collector leases: %w", err)
	}

	type expiredLease struct {
		jobID      int64
		leaseOwner string
		leaseToken string
		generation int64
		attemptID  pgtype.Int8
	}
	expired := make([]expiredLease, 0)
	for rows.Next() {
		var lease expiredLease
		if err := rows.Scan(
			&lease.jobID,
			&lease.leaseOwner,
			&lease.leaseToken,
			&lease.generation,
			&lease.attemptID,
		); err != nil {
			rows.Close()
			return fmt.Errorf("scan expired collector lease: %w", err)
		}
		expired = append(expired, lease)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("read expired collector leases: %w", err)
	}
	rows.Close()

	for _, lease := range expired {
		attemptNumber := 0
		attemptStatus := ""
		failureCategory := pgtype.Text{}
		if lease.attemptID.Valid {
			err := transaction.QueryRow(ctx, `
				SELECT status, attempt_number, failure_category
				FROM collector_attempts
				WHERE id = $1 AND job_id = $2
				FOR UPDATE
			`, lease.attemptID.Int64, lease.jobID).Scan(
				&attemptStatus,
				&attemptNumber,
				&failureCategory,
			)
			if errors.Is(err, pgx.ErrNoRows) {
				lease.attemptID = pgtype.Int8{}
			} else if err != nil {
				return fmt.Errorf("lock expired collector attempt: %w", err)
			}
		}
		if !lease.attemptID.Valid {
			if err := transaction.QueryRow(ctx, `
				SELECT COALESCE(MAX(attempt_number), 0)
				FROM collector_attempts
				WHERE job_id = $1
			`, lease.jobID).Scan(&attemptNumber); err != nil {
				return fmt.Errorf("read collector attempt bound: %w", err)
			}
		}

		if lease.attemptID.Valid && attemptStatus != "complete" {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_endpoint_results AS endpoint_result
				SET outcome = 'failed',
					next_retry_at = NULL,
					execution_token = NULL,
					failure_category = 'lease_expired'
				FROM collector_jobs AS job
				WHERE endpoint_result.attempt_id = $1
					AND job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
					AND endpoint_result.outcome <> 'observed'
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("fail expired collector endpoints: %w", err)
			}

			command, err := transaction.Exec(ctx, `
				UPDATE collector_attempts AS attempt
				SET status = 'failed',
					completed_at = $6,
					failure_category = 'lease_expired',
					lease_owner = NULL,
					lease_token = NULL
				FROM collector_jobs AS job
				WHERE attempt.id = $1
					AND attempt.job_id = $2
					AND job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
					AND attempt.status <> 'complete'
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now)
			if err != nil {
				return fmt.Errorf("terminalize expired collector attempt: %w", err)
			}
			if command.RowsAffected() != 1 {
				return errLeaseLost
			}

			if _, err := transaction.Exec(ctx, `
				INSERT INTO collector_attempt_events (
					job_id, attempt_id, event_type, from_status, to_status,
					lease_owner, lease_token, lease_generation, failure_category
				)
				SELECT job.id, $1, 'lease_expired', $2, 'failed',
					job.lease_owner, job.lease_token, job.lease_generation,
					'lease_expired'
				FROM collector_jobs AS job
				WHERE job.id = $3
					AND job.lease_owner = $4
					AND job.lease_token = $5
					AND job.lease_generation = $6
					AND job.status = 'leased'
					AND job.lease_expires_at <= $7
				ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
			`, lease.attemptID.Int64, attemptStatus, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("record expired collector attempt event: %w", err)
			}
		} else if lease.attemptID.Valid && failureCategory.Valid && failureCategory.String == "lease_expired" {
			if _, err := transaction.Exec(ctx, `
				INSERT INTO collector_attempt_events (
					job_id, attempt_id, event_type, from_status, to_status,
					lease_owner, lease_token, lease_generation, failure_category
				)
				SELECT job.id, $1, 'lease_expired', 'failed', 'failed',
					job.lease_owner, job.lease_token, job.lease_generation,
					'lease_expired'
				FROM collector_jobs AS job
				WHERE job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
				ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("record existing expired collector attempt event: %w", err)
			}
		}

		status := "pending"
		if attemptNumber >= maxCollectorAttemptsV2 {
			status = "failed"
		}
		command, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = $6,
				due_at = CASE WHEN $6 = 'pending' THEN $5 ELSE due_at END,
				cancel_reason = CASE WHEN $6 = 'failed' THEN 'lease_expired' ELSE NULL END,
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				result_attempt_id = NULL,
				updated_at = $5
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at <= $5
		`, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now, status)
		if err != nil {
			return fmt.Errorf("release expired collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
	}
	return nil
}

func (s *store) prepareAttempt(ctx context.Context, job *collectionJob, now time.Time) (int64, []endpointName, error) {
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, nil, fmt.Errorf("begin attempt transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var status string
	var existingAttempt pgtype.Int8
	leaseQuery := `
		SELECT status, result_attempt_id
		FROM collector_jobs
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`
	leaseArgs := []any{job.id, job.leaseToken}
	if s.contractVersion >= 2 {
		leaseQuery = `
			SELECT status, result_attempt_id
			FROM collector_jobs
			WHERE id = $1
				AND lease_owner = $3
				AND lease_token = $2
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
			FOR UPDATE
		`
		leaseArgs = []any{job.id, job.leaseToken, job.leaseOwner, job.leaseGeneration}
	}
	if err := transaction.QueryRow(ctx, leaseQuery, leaseArgs...).Scan(&status, &existingAttempt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, nil, errLeaseLost
		}
		return 0, nil, fmt.Errorf("lock claimed job: %w", err)
	}
	if status != "leased" {
		return 0, nil, errLeaseLost
	}

	requiresActivePlayer := job.workType == "regular_poll"
	eligibilityPlayerID := job.playerID
	eligibilityRootJobID := job.id
	eligibilityAttemptID := existingAttempt
	if job.workType == "endpoint_retry" && job.parentAttemptID.Valid {
		var parentWorkType string
		if err := transaction.QueryRow(ctx, `
			WITH RECURSIVE lineage (attempt_id, job_id, work_type, player_id, parent_attempt_id, depth) AS (
				SELECT parent_attempt.id, parent_job.id, parent_job.work_type,
					parent_job.player_id, parent_job.parent_attempt_id, 1
				FROM collector_attempts AS parent_attempt
				JOIN collector_jobs AS parent_job ON parent_job.id = parent_attempt.job_id
				WHERE parent_attempt.id = $1

				UNION ALL

				SELECT parent_attempt.id, parent_job.id, parent_job.work_type,
					parent_job.player_id, parent_job.parent_attempt_id, child.depth + 1
				FROM lineage AS child
				JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
				JOIN collector_jobs AS parent_job ON parent_job.id = parent_attempt.job_id
				WHERE child.work_type = 'endpoint_retry' AND child.depth < 32
			)
			SELECT work_type, player_id, job_id, attempt_id
			FROM lineage
			WHERE work_type <> 'endpoint_retry'
			ORDER BY depth
			LIMIT 1
		`, job.parentAttemptID.Int64).Scan(
			&parentWorkType,
			&eligibilityPlayerID,
			&eligibilityRootJobID,
			&eligibilityAttemptID,
		); err != nil {
			return 0, nil, fmt.Errorf("load endpoint retry eligibility: %w", err)
		}
		requiresActivePlayer = parentWorkType == "regular_poll"
	}
	if requiresActivePlayer {
		var active bool
		if !eligibilityPlayerID.Valid {
			return 0, nil, errors.New("regular collection work has no player ID")
		}
		if err := transaction.QueryRow(ctx, `SELECT active FROM players WHERE id = $1`, eligibilityPlayerID).Scan(&active); err != nil {
			return 0, nil, fmt.Errorf("check regular player eligibility: %w", err)
		}
		if !active {
			attemptNeedsResolution := false
			if eligibilityAttemptID.Valid {
				var attemptStatus string
				if err := transaction.QueryRow(ctx, `
					SELECT status FROM collector_attempts WHERE id = $1 FOR UPDATE
				`, eligibilityAttemptID).Scan(&attemptStatus); err != nil {
					return 0, nil, fmt.Errorf("lock inactive collection attempt: %w", err)
				}
				switch attemptStatus {
				case "running", "incomplete":
					attemptNeedsResolution = true
				case "failed":
				case "complete":
					return 0, nil, errors.New("inactive collection attempt was already complete")
				default:
					return 0, nil, fmt.Errorf("inactive collection attempt has unknown status %q", attemptStatus)
				}
			}
			command, err := transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET status = 'cancelled', cancel_reason = 'player_inactive',
					lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = $2
				WHERE id = $1
					AND lease_token = $3
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, now, job.leaseToken)
			if err != nil {
				return 0, nil, fmt.Errorf("cancel inactive claimed job: %w", err)
			}
			if command.RowsAffected() != 1 {
				return 0, nil, errLeaseLost
			}
			if eligibilityAttemptID.Valid {
				if _, err := transaction.Exec(ctx, `
					UPDATE collector_endpoint_results
					SET outcome = 'failed',
						next_retry_at = NULL,
						failure_category = 'player_inactive'
					WHERE attempt_id = $1 AND outcome <> 'observed'
				`, eligibilityAttemptID); err != nil {
					return 0, nil, fmt.Errorf("fail inactive attempt endpoints: %w", err)
				}
				if attemptNeedsResolution {
					command, err = transaction.Exec(ctx, `
						UPDATE collector_attempts
						SET status = 'failed', completed_at = $2
						WHERE id = $1 AND status IN ('running', 'incomplete')
					`, eligibilityAttemptID, now)
					if err != nil {
						return 0, nil, fmt.Errorf("fail inactive collection attempt: %w", err)
					}
					if command.RowsAffected() != 1 {
						return 0, nil, errors.New("inactive collection attempt changed while locked")
					}
				}
				if _, err := transaction.Exec(ctx, `
					UPDATE collector_jobs
					SET status = 'cancelled',
						cancel_reason = 'player_inactive',
						lease_owner = NULL,
						lease_token = NULL,
						lease_expires_at = NULL,
						updated_at = $2
					WHERE parent_attempt_id = $1
						AND status IN ('pending', 'waiting_retry')
				`, eligibilityAttemptID, now); err != nil {
					return 0, nil, fmt.Errorf("cancel inactive sibling retries: %w", err)
				}
			}
			if eligibilityRootJobID != job.id {
				command, err = transaction.Exec(ctx, `
					UPDATE collector_jobs
					SET status = 'cancelled',
						cancel_reason = 'player_inactive',
						lease_owner = NULL,
						lease_token = NULL,
						lease_expires_at = NULL,
						updated_at = $2
					WHERE id = $1 AND status = 'waiting_retry'
				`, eligibilityRootJobID, now)
				if err != nil {
					return 0, nil, fmt.Errorf("cancel inactive root job: %w", err)
				}
				if command.RowsAffected() != 1 {
					var rootStatus string
					if err := transaction.QueryRow(ctx, `
						SELECT status FROM collector_jobs WHERE id = $1
					`, eligibilityRootJobID).Scan(&rootStatus); err != nil {
						return 0, nil, fmt.Errorf("check inactive root job: %w", err)
					}
					if rootStatus != "cancelled" {
						return 0, nil, fmt.Errorf("inactive root job has status %q", rootStatus)
					}
				}
			}
			if err := transaction.Commit(ctx); err != nil {
				return 0, nil, fmt.Errorf("commit inactive job cancellation: %w", err)
			}
			return 0, nil, errJobCancelled
		}
	}

	attemptID := existingAttempt.Int64
	if existingAttempt.Valid && s.contractVersion >= 2 && job.workType != "endpoint_retry" {
		if err := lockCurrentAttemptV2(ctx, transaction, job, existingAttempt.Int64); err != nil {
			return 0, nil, err
		}
	}
	if !existingAttempt.Valid {
		if job.workType == "endpoint_retry" && job.parentAttemptID.Valid {
			attemptID = job.parentAttemptID.Int64
		} else if s.contractVersion >= 2 {
			var previousAttemptNumber int
			if err := transaction.QueryRow(ctx, `
				SELECT COALESCE(MAX(attempt_number), 0)
				FROM collector_attempts
				WHERE job_id = $1
			`, job.id).Scan(&previousAttemptNumber); err != nil {
				return 0, nil, fmt.Errorf("read collection attempt number: %w", err)
			}
			if previousAttemptNumber >= maxCollectorAttemptsV2 {
				return 0, nil, fmt.Errorf("collector job %d exhausted attempt limit", job.id)
			}
			if err := transaction.QueryRow(ctx, `
				INSERT INTO collector_attempts (
					job_id, attempt_number, lease_owner, lease_token,
					lease_generation, status, started_at
				)
				VALUES ($1, $2, $3, $4, $5, 'running', $6)
				RETURNING id
			`, job.id, previousAttemptNumber+1, job.leaseOwner, job.leaseToken,
				job.leaseGeneration, now).Scan(&attemptID); err != nil {
				return 0, nil, fmt.Errorf("create version-two collection attempt: %w", err)
			}
		} else if err := transaction.QueryRow(ctx, `
			INSERT INTO collector_attempts (job_id, status, started_at)
			VALUES ($1, 'running', $2)
			RETURNING id
		`, job.id, now).Scan(&attemptID); err != nil {
			return 0, nil, fmt.Errorf("create collection attempt: %w", err)
		}
		var command pgconn.CommandTag
		if s.contractVersion >= 2 {
			command, err = transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET result_attempt_id = $2
				WHERE id = $1
					AND lease_owner = $3
					AND lease_token = $4
					AND lease_generation = $5
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, attemptID, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		} else {
			command, err = transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET result_attempt_id = $2
				WHERE id = $1
					AND lease_token = $3
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, attemptID, job.leaseToken)
		}
		if err != nil {
			return 0, nil, fmt.Errorf("link collection attempt: %w", err)
		}
		if command.RowsAffected() != 1 {
			return 0, nil, errLeaseLost
		}
	}

	required := []endpointName{profileEndpoint, battleLogEndpoint}
	if job.workType == "legacy_reset_profile" || job.workType == "reset_profile" {
		required = []endpointName{profileEndpoint}
	}
	if job.workType == "global_player_rankings" {
		required = []endpointName{globalPlayerRankingsEndpoint}
	}
	if job.workType == "endpoint_retry" {
		required = []endpointName{endpointName(job.requiredEndpoint.String)}
	}
	for _, endpoint := range required {
		if _, err := transaction.Exec(ctx, `
			INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome)
			VALUES ($1, $2, 'pending')
			ON CONFLICT (attempt_id, endpoint) DO NOTHING
		`, attemptID, string(endpoint)); err != nil {
			return 0, nil, fmt.Errorf("create endpoint result %s: %w", endpoint, err)
		}
	}

	rows, err := transaction.Query(ctx, `
		SELECT endpoint
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
		ORDER BY endpoint
	`, attemptID)
	if err != nil {
		return 0, nil, fmt.Errorf("list required endpoint work: %w", err)
	}
	endpoints := make([]endpointName, 0, len(required))
	for rows.Next() {
		var endpoint string
		if err := rows.Scan(&endpoint); err != nil {
			rows.Close()
			return 0, nil, fmt.Errorf("scan required endpoint work: %w", err)
		}
		if job.workType == "endpoint_retry" && endpoint != job.requiredEndpoint.String {
			continue
		}
		endpoints = append(endpoints, endpointName(endpoint))
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, nil, fmt.Errorf("read required endpoint work: %w", err)
	}
	rows.Close()

	if s.contractVersion >= 2 {
		if _, err := transaction.Exec(ctx, `
			INSERT INTO collector_attempt_events (
				job_id, attempt_id, event_type, from_status, to_status,
				lease_owner, lease_token, lease_generation
			)
			SELECT job.id, $1, 'claimed', NULL, 'running',
				job.lease_owner, job.lease_token, job.lease_generation
			FROM collector_jobs AS job
			WHERE job.id = $2
				AND job.lease_owner = $3
				AND job.lease_token = $4
				AND job.lease_generation = $5
				AND job.status = 'leased'
				AND job.lease_expires_at > clock_timestamp()
				AND (job.result_attempt_id = $1 OR job.parent_attempt_id = $1)
			ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
		`, attemptID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration); err != nil {
			return 0, nil, fmt.Errorf("record claimed collection attempt event: %w", err)
		}
		if err := lockCurrentLeaseV2(ctx, transaction, job); err != nil {
			return 0, nil, err
		}
	} else if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return 0, nil, err
	}

	if err := transaction.Commit(ctx); err != nil {
		return 0, nil, fmt.Errorf("commit attempt transaction: %w", err)
	}
	return attemptID, endpoints, nil
}

func (s *store) beginEndpointRequest(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	startedAt time.Time,
) (int, error) {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return 0, err
	}
	if contractVersion >= 2 {
		return s.beginEndpointRequestV2(ctx, job, attemptID, endpoint, startedAt)
	}
	var requestCount int
	err = s.pool.QueryRow(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET request_count = request_count + 1,
			execution_token = $3,
			request_started_at = $4,
			key_label = NULL
		FROM collector_jobs AS job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND job.id = $5
			AND job.lease_token = $3
			AND job.status = 'leased'
			AND job.lease_expires_at > clock_timestamp()
		RETURNING endpoint_result.request_count
	`, attemptID, string(endpoint), job.leaseToken, startedAt, job.id).Scan(&requestCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, errLeaseLost
	}
	if err != nil {
		return 0, fmt.Errorf("begin endpoint request: %w", err)
	}
	return requestCount, nil
}

func (s *store) commitObservation(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	requestCount int,
	response officialResponse,
	hash string,
	archiveReference string,
	collectorVersion string,
	keyLabel string,
	outcome string,
	nextRetryAt *time.Time,
) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.commitObservationV2(
			ctx, job, attemptID, endpoint, requestCount, response, hash,
			archiveReference, collectorVersion, keyLabel, outcome, nextRetryAt,
		)
	}
	headers, err := json.Marshal(response.headers)
	if err != nil {
		return fmt.Errorf("encode evidence headers: %w", err)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin observation transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var leaseCurrent bool
	if err := transaction.QueryRow(ctx, `
		SELECT true
		FROM collector_jobs
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`, job.id, job.leaseToken).Scan(&leaseCurrent); err != nil {
		return errLeaseLost
	}

	occurrenceKey := strconv.FormatInt(attemptID, 10) + ":" + string(endpoint) + ":" + strconv.Itoa(requestCount)
	var observationID int64
	err = transaction.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key,
			collection_job_id,
			attempt_id,
			player_id,
			normalized_tag,
			endpoint,
			request_started_at,
			response_completed_at,
			http_status,
			response_hash,
			archive_reference,
			collector_version,
			key_label,
			evidence_headers
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
		ON CONFLICT (occurrence_key) DO NOTHING
		RETURNING id
	`,
		occurrenceKey,
		job.id,
		attemptID,
		job.playerID,
		job.normalizedTag,
		string(endpoint),
		response.requestStartedAt,
		response.responseCompletedAt,
		response.statusCode,
		hash,
		archiveReference,
		collectorVersion,
		keyLabel,
		headers,
	).Scan(&observationID)
	if errors.Is(err, pgx.ErrNoRows) {
		err = transaction.QueryRow(ctx, `
			SELECT id FROM collector_observations WHERE occurrence_key = $1
		`, occurrenceKey).Scan(&observationID)
	}
	if err != nil {
		return fmt.Errorf("insert observation occurrence: %w", err)
	}

	if _, err := transaction.Exec(ctx, `
		INSERT INTO python_processing_jobs (observation_id)
		VALUES ($1)
		ON CONFLICT (observation_id) DO NOTHING
	`, observationID); err != nil {
		return fmt.Errorf("insert Python processing job: %w", err)
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = $4,
			next_retry_at = $5,
			request_started_at = $6,
			response_completed_at = $7,
			http_status = $8,
			response_hash = $9,
			archive_reference = $10,
			observation_id = $11,
			failure_category = NULL,
			key_label = $12
		WHERE attempt_id = $1
			AND endpoint = $2
			AND execution_token = $3
			AND EXISTS (
				SELECT 1
				FROM collector_jobs AS job
				WHERE job.id = $13
					AND job.lease_token = $3
					AND job.status = 'leased'
					AND job.lease_expires_at > clock_timestamp()
			)
	`,
		attemptID,
		string(endpoint),
		job.leaseToken,
		outcome,
		nextRetryAt,
		response.requestStartedAt,
		response.responseCompletedAt,
		response.statusCode,
		hash,
		archiveReference,
		observationID,
		keyLabel,
		job.id,
	)
	if err != nil {
		return fmt.Errorf("update endpoint result: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}

	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit observation transaction: %w", err)
	}
	return nil
}

func (s *store) finishAttemptV2(ctx context.Context, job *collectionJob, attemptID int64, completedAt time.Time) error {
	preflightIntent := attemptCommitIntent{
		job:       *job,
		attemptID: attemptID,
		now:       completedAt,
	}
	if proofOutcome, proofErr := s.probeFreshConnection(ctx, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
		return s.proveTerminalCompletionCommit(proofCtx, connection, preflightIntent)
	}); proofErr == nil && proofOutcome == commitProofCommitted {
		return nil
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin version-two completion transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if err := lockCurrentAttemptV2(ctx, transaction, job, attemptID); err != nil {
		return err
	}
	var rootJobID int64
	if err := transaction.QueryRow(ctx, `
		SELECT job_id
		FROM collector_attempts
		WHERE id = $1
		FOR UPDATE
	`, attemptID).Scan(&rootJobID); err != nil {
		return fmt.Errorf("lock version-two completion attempt: %w", err)
	}
	preCommitSnapshot, snapshotPresent, err := readAttemptResolutionSnapshot(
		ctx, transaction, job.id, attemptID, rootJobID,
	)
	if err != nil {
		return fmt.Errorf("read version-two completion state: %w", err)
	}
	if !snapshotPresent {
		return errors.New("version-two completion state is missing")
	}
	commitIntent := attemptCommitIntent{
		job:               *job,
		attemptID:         attemptID,
		rootJobID:         rootJobID,
		now:               completedAt,
		completionOnly:    true,
		preCommitSnapshot: &preCommitSnapshot,
	}
	var incomplete int
	if err := transaction.QueryRow(ctx, `
		SELECT count(*)
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
	`, attemptID).Scan(&incomplete); err != nil {
		return fmt.Errorf("count version-two incomplete endpoints: %w", err)
	}

	if incomplete > 0 {
		command, err := transaction.Exec(ctx, `
			UPDATE collector_attempts AS attempt
			SET status = 'incomplete'
			FROM collector_jobs AS current_job
			WHERE attempt.id = $1
				AND attempt.status IN ('running', 'incomplete')
				AND current_job.id = $2
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		`, attemptID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		if err != nil {
			return fmt.Errorf("mark version-two attempt incomplete: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
		if err := s.commitTransaction(ctx, transaction); err != nil {
			return fmt.Errorf("commit version-two incomplete attempt: %w", err)
		}
		return nil
	}

	command, err := transaction.Exec(ctx, `
		UPDATE collector_attempts AS attempt
		SET status = 'complete', completed_at = $2
		FROM collector_jobs AS current_job
		WHERE attempt.id = $1
			AND attempt.job_id = $3
			AND attempt.status IN ('running', 'incomplete')
			AND current_job.id = $4
			AND current_job.lease_owner = $5
			AND current_job.lease_token = $6
			AND current_job.lease_generation = $7
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
	`, attemptID, completedAt, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("complete version-two attempt: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}

	if _, err := transaction.Exec(ctx, `
		UPDATE collector_reset_baseline_sweeps AS baseline
		SET state = 'complete', completed_at = $2
		FROM collector_jobs AS root_job
		JOIN collector_jobs AS current_job ON current_job.id = $3
		WHERE root_job.id = $1
			AND baseline.id = root_job.reset_baseline_sweep_id
			AND baseline.evidence_kind = 'paired_v2'
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $7 OR current_job.parent_attempt_id = $7)
	`, rootJobID, completedAt, job.id, job.leaseOwner, job.leaseToken,
		job.leaseGeneration, attemptID); err != nil {
		return fmt.Errorf("complete version-two reset baseline: %w", err)
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_attempt_events (
			job_id, attempt_id, event_type, from_status, to_status,
			lease_owner, lease_token, lease_generation
		)
		SELECT current_job.id, $1, 'completed', $2, 'complete',
			current_job.lease_owner, current_job.lease_token, current_job.lease_generation
		FROM collector_jobs AS current_job
		WHERE current_job.id = $3
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
	`, attemptID, preCommitSnapshot.attempt.status, job.id, job.leaseOwner,
		job.leaseToken, job.leaseGeneration); err != nil {
		return fmt.Errorf("record completed version-two attempt event: %w", err)
	}

	if rootJobID == job.id {
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'complete',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $5
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
				AND (result_attempt_id = $6 OR parent_attempt_id = $6)
		`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two collector job: %w", err)
		}
	} else {
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs AS root_job
			SET status = 'complete', updated_at = $6
			FROM collector_jobs AS current_job
			WHERE root_job.id = $1
				AND current_job.id = $2
				AND root_job.status IN ('waiting_retry', 'leased')
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $7 OR current_job.parent_attempt_id = $7)
		`, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two root collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'complete',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $5
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
				AND (result_attempt_id = $6 OR parent_attempt_id = $6)
			`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two retry collector job: %w", err)
		}
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveTerminalCompletionCommit(proofCtx, connection, commitIntent)
		})
	}
	return nil
}

func (s *store) finishAttempt(ctx context.Context, job *collectionJob, attemptID int64, completedAt time.Time) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.finishAttemptV2(ctx, job, attemptID, completedAt)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin completion transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var incomplete int
	if err := transaction.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
	`, attemptID).Scan(&incomplete); err != nil {
		return fmt.Errorf("count incomplete endpoints: %w", err)
	}
	if incomplete > 0 {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts SET status = 'incomplete' WHERE id = $1
		`, attemptID); err != nil {
			return fmt.Errorf("mark attempt incomplete: %w", err)
		}
		return transaction.Commit(ctx)
	}

	if _, err := transaction.Exec(ctx, `
		UPDATE collector_attempts
		SET status = 'complete', completed_at = $2
		WHERE id = $1
	`, attemptID, completedAt); err != nil {
		return fmt.Errorf("complete attempt: %w", err)
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'complete',
			lease_owner = NULL,
			lease_token = NULL,
			lease_expires_at = NULL,
			updated_at = $3
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
	`, job.id, job.leaseToken, completedAt)
	if err != nil {
		return fmt.Errorf("complete collector job: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if job.resetBaselineSweepID.Valid {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_reset_baseline_sweeps
			SET state = 'complete', completed_at = $2
			WHERE id = $1 AND evidence_kind = 'paired_v2'
		`, job.resetBaselineSweepID, completedAt); err != nil {
			return fmt.Errorf("complete reset baseline sweep: %w", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit completion transaction: %w", err)
	}
	return nil
}
