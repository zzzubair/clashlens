package collector

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

func (s *store) recordTransportFailure(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	startedAt time.Time,
	failedAt time.Time,
	nextRetryAt time.Time,
	category string,
	keyLabel string,
) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.recordTransportFailureV2(
			ctx, job, attemptID, endpoint, startedAt, failedAt,
			nextRetryAt, category, keyLabel,
		)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin transport failure transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return err
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_transport_failures (
			collection_job_id,
			attempt_id,
			player_id,
			normalized_tag,
			endpoint,
			request_started_at,
			failed_at,
			failure_category,
			retry_state,
			key_label
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'retryable', $9)
	`, job.id, attemptID, job.playerID, job.normalizedTag, string(endpoint), startedAt, failedAt, category, keyLabel); err != nil {
		return fmt.Errorf("insert transport failure: %w", err)
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET outcome = 'transport_failed',
			request_started_at = $3,
			next_retry_at = $4,
			response_completed_at = NULL,
			http_status = NULL,
			response_hash = NULL,
			archive_reference = NULL,
			observation_id = NULL,
			failure_category = $5,
			key_label = $6
		FROM collector_jobs AS job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND endpoint_result.execution_token = $7
			AND job.id = $8
			AND job.lease_token = $7
			AND job.status = 'leased'
			AND job.lease_expires_at > clock_timestamp()
	`, attemptID, string(endpoint), startedAt, nextRetryAt, category, keyLabel, job.leaseToken, job.id)
	if err != nil {
		return fmt.Errorf("update transport-failed endpoint: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit transport failure: %w", err)
	}
	return nil
}

func (s *store) recordStorageFailure(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	response officialResponse,
	category string,
	keyLabel string,
) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.recordStorageFailureV2(ctx, job, attemptID, endpoint, response, category, keyLabel)
	}
	command, err := s.pool.Exec(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET outcome = 'storage_failed',
			request_started_at = $4,
			response_completed_at = $5,
			http_status = $6,
			response_hash = NULL,
			archive_reference = NULL,
			observation_id = NULL,
			failure_category = $7,
			key_label = $8
		FROM collector_jobs AS job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND endpoint_result.execution_token = $3
			AND job.id = $9
			AND job.lease_token = $3
			AND job.status = 'leased'
			AND job.lease_expires_at > clock_timestamp()
	`,
		attemptID,
		string(endpoint),
		job.leaseToken,
		response.requestStartedAt,
		response.responseCompletedAt,
		response.statusCode,
		category,
		keyLabel,
		job.id,
	)
	if err != nil {
		return fmt.Errorf("record storage-failed endpoint: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	return nil
}

func lockCurrentLease(ctx context.Context, transaction pgx.Tx, job *collectionJob) error {
	var found bool
	if err := transaction.QueryRow(ctx, `
		SELECT true
		FROM collector_jobs
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`, job.id, job.leaseToken).Scan(&found); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errLeaseLost
		}
		return fmt.Errorf("lock current lease: %w", err)
	}
	return nil
}

func lockCurrentLeaseV2(ctx context.Context, transaction pgx.Tx, job *collectionJob) error {
	var found bool
	if err := transaction.QueryRow(ctx, `
		SELECT true
		FROM collector_jobs
		WHERE id = $1
			AND lease_owner = $3
			AND lease_token = $2
			AND lease_generation = $4
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`, job.id, job.leaseToken, job.leaseOwner, job.leaseGeneration).Scan(&found); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errLeaseLost
		}
		return fmt.Errorf("lock current version-two lease: %w", err)
	}
	return nil
}

func lockCurrentAttemptV2(ctx context.Context, transaction pgx.Tx, job *collectionJob, attemptID int64) error {
	var found bool
	if err := transaction.QueryRow(ctx, `
		SELECT true
		FROM collector_jobs AS current_job
		JOIN collector_attempts AS attempt ON attempt.id = $2
		WHERE current_job.id = $1
			AND current_job.lease_owner = $3
			AND current_job.lease_token = $4
			AND current_job.lease_generation = $5
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND attempt.status IN ('running', 'incomplete')
			AND (
				(
					current_job.result_attempt_id = $2
					AND attempt.job_id = current_job.id
					AND attempt.lease_owner = current_job.lease_owner
					AND attempt.lease_token = current_job.lease_token
					AND attempt.lease_generation = current_job.lease_generation
				)
				OR current_job.parent_attempt_id = $2
			)
		FOR UPDATE OF current_job, attempt
	`, job.id, attemptID, job.leaseOwner, job.leaseToken, job.leaseGeneration).Scan(&found); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return errLeaseLost
		}
		return fmt.Errorf("lock current version-two attempt: %w", err)
	}
	return nil
}

func (s *store) resolveAttempt(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	now time.Time,
	maximumRetries int,
) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.resolveAttemptV2(ctx, job, attemptID, now, maximumRetries)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin attempt resolution transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return err
	}
	var rootJobID int64
	if err := transaction.QueryRow(ctx, `
		SELECT job_id FROM collector_attempts WHERE id = $1 FOR UPDATE
	`, attemptID).Scan(&rootJobID); err != nil {
		return fmt.Errorf("lock collection attempt: %w", err)
	}

	rows, err := transaction.Query(ctx, `
		SELECT endpoint, retry_count, next_retry_at
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
		ORDER BY endpoint
		FOR UPDATE
	`, attemptID)
	if err != nil {
		return fmt.Errorf("select incomplete endpoints: %w", err)
	}
	type incompleteEndpoint struct {
		name        endpointName
		retryCount  int
		nextRetryAt pgtype.Timestamptz
	}
	var incomplete []incompleteEndpoint
	for rows.Next() {
		var endpoint incompleteEndpoint
		if err := rows.Scan(&endpoint.name, &endpoint.retryCount, &endpoint.nextRetryAt); err != nil {
			rows.Close()
			return fmt.Errorf("scan incomplete endpoint: %w", err)
		}
		incomplete = append(incomplete, endpoint)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("read incomplete endpoints: %w", err)
	}
	rows.Close()

	if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return err
	}

	if len(incomplete) == 0 {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts SET status = 'complete', completed_at = $2 WHERE id = $1
		`, attemptID, now); err != nil {
			return fmt.Errorf("complete attempt: %w", err)
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'complete', lease_owner = NULL, lease_token = NULL,
				lease_expires_at = NULL, updated_at = $2
			WHERE id IN ($1, $3)
		`, rootJobID, now, job.id); err != nil {
			return fmt.Errorf("complete collector jobs: %w", err)
		}
		if contractVersion >= 2 {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_reset_baseline_sweeps AS baseline
				SET state = 'complete', completed_at = $2
				FROM collector_jobs AS root_job
				WHERE root_job.id = $1
				  AND baseline.id = root_job.reset_baseline_sweep_id
				  AND baseline.evidence_kind = 'paired_v2'
			`, rootJobID, now); err != nil {
				return fmt.Errorf("complete retried reset baseline: %w", err)
			}
		}
		if err := transaction.Commit(ctx); err != nil {
			return fmt.Errorf("commit completed attempt: %w", err)
		}
		return nil
	}

	terminal := false
	for _, endpoint := range incomplete {
		if job.workType == "endpoint_retry" && string(endpoint.name) != job.requiredEndpoint.String {
			continue
		}
		if endpoint.retryCount >= maximumRetries {
			terminal = true
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_endpoint_results
				SET outcome = 'failed', next_retry_at = NULL
				WHERE attempt_id = $1 AND endpoint = $2
			`, attemptID, string(endpoint.name)); err != nil {
				return fmt.Errorf("mark endpoint terminal: %w", err)
			}
			continue
		}

		nextRetryCount := endpoint.retryCount + 1
		dueAt := now
		if endpoint.nextRetryAt.Valid && endpoint.nextRetryAt.Time.After(now) {
			dueAt = endpoint.nextRetryAt.Time
		}
		coalescingKey := "retry:" + strconv.FormatInt(attemptID, 10) + ":" + string(endpoint.name) + ":" + strconv.Itoa(nextRetryCount)
		if contractVersion >= 2 {
			var normalizedTag any = job.normalizedTag
			if job.scope == "global" {
				normalizedTag = nil
			}
			if _, err := transaction.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, scope, player_id, normalized_tag, capacity_pool,
					priority, due_at, coalescing_key, sweep_id,
					reset_baseline_sweep_id, parent_attempt_id,
					required_endpoint, status
				)
				VALUES (
					'endpoint_retry', $1, $2, $3, $4, 300, $5, $6,
					$7, $8, $9, $10, 'pending'
				)
				ON CONFLICT DO NOTHING
			`, job.scope, job.playerID, normalizedTag, string(job.pool), dueAt,
				coalescingKey, job.sweepID, job.resetBaselineSweepID, attemptID,
				string(endpoint.name)); err != nil {
				return fmt.Errorf("insert version-two endpoint retry: %w", err)
			}
		} else if _, err := transaction.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type,
				player_id,
				normalized_tag,
				capacity_pool,
				priority,
				due_at,
				coalescing_key,
				parent_attempt_id,
				required_endpoint,
				status
			)
			VALUES ('endpoint_retry', $1, $2, $3, 300, $4, $5, $6, $7, 'pending')
			ON CONFLICT DO NOTHING
		`, job.playerID, job.normalizedTag, string(job.pool), dueAt, coalescingKey, attemptID, string(endpoint.name)); err != nil {
			return fmt.Errorf("insert endpoint retry: %w", err)
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_endpoint_results
			SET outcome = 'retrying', retry_count = $3, next_retry_at = $4
			WHERE attempt_id = $1 AND endpoint = $2
		`, attemptID, string(endpoint.name), nextRetryCount, dueAt); err != nil {
			return fmt.Errorf("mark endpoint retrying: %w", err)
		}
	}

	if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return err
	}

	if terminal {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts SET status = 'failed', completed_at = $2 WHERE id = $1
		`, attemptID, now); err != nil {
			return fmt.Errorf("fail collection attempt: %w", err)
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'failed', lease_owner = NULL, lease_token = NULL,
				lease_expires_at = NULL, updated_at = $2
			WHERE id IN ($1, $3)
		`, rootJobID, now, job.id); err != nil {
			return fmt.Errorf("fail collector jobs: %w", err)
		}
		if contractVersion >= 2 {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_reset_baseline_sweeps AS baseline
				SET state = 'failed', completed_at = $2
				FROM collector_jobs AS root_job
				WHERE root_job.id = $1
				  AND baseline.id = root_job.reset_baseline_sweep_id
				  AND baseline.evidence_kind = 'paired_v2'
			`, rootJobID, now); err != nil {
				return fmt.Errorf("fail retried reset baseline: %w", err)
			}
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'cancelled',
				cancel_reason = 'attempt_terminal',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $2
			WHERE parent_attempt_id = $1
				AND status IN ('pending', 'leased', 'waiting_retry')
		`, attemptID, now); err != nil {
			return fmt.Errorf("cancel sibling retry jobs: %w", err)
		}
	} else {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts SET status = 'incomplete' WHERE id = $1
		`, attemptID); err != nil {
			return fmt.Errorf("mark collection attempt incomplete: %w", err)
		}
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'waiting_retry', lease_owner = NULL, lease_token = NULL,
				lease_expires_at = NULL, updated_at = $2
			WHERE id = $1
		`, rootJobID, now); err != nil {
			return fmt.Errorf("mark root job waiting for retry: %w", err)
		}
		if contractVersion >= 2 {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_reset_baseline_sweeps AS baseline
				SET state = 'incomplete', completed_at = NULL
				FROM collector_jobs AS root_job
				WHERE root_job.id = $1
				  AND baseline.id = root_job.reset_baseline_sweep_id
				  AND baseline.evidence_kind = 'paired_v2'
			`, rootJobID); err != nil {
				return fmt.Errorf("mark reset baseline incomplete: %w", err)
			}
		}
		if job.id != rootJobID {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET status = 'complete', lease_owner = NULL, lease_token = NULL,
					lease_expires_at = NULL, updated_at = $2
				WHERE id = $1
			`, job.id, now); err != nil {
				return fmt.Errorf("complete consumed retry job: %w", err)
			}
		}
	}

	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit attempt resolution: %w", err)
	}
	return nil
}

func (s *store) resolveAttemptV2(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	now time.Time,
	maximumRetries int,
) error {
	preflightIntent := attemptCommitIntent{
		job:            *job,
		attemptID:      attemptID,
		now:            now,
		maximumRetries: maximumRetries,
	}
	if proofOutcome, proofErr := s.probeFreshConnection(ctx, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
		return s.proveAttemptResolutionCommit(proofCtx, connection, preflightIntent)
	}); proofErr == nil && proofOutcome == commitProofCommitted {
		return nil
	}

	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin version-two attempt resolution transaction: %w", err)
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
		return fmt.Errorf("lock version-two collection attempt: %w", err)
	}
	preCommitSnapshot, snapshotPresent, err := readAttemptResolutionSnapshot(
		ctx, transaction, job.id, attemptID, rootJobID,
	)
	if err != nil {
		return fmt.Errorf("read version-two attempt resolution state: %w", err)
	}
	if !snapshotPresent {
		return errors.New("version-two attempt resolution state is missing")
	}
	commitIntent := attemptCommitIntent{
		job:               *job,
		attemptID:         attemptID,
		rootJobID:         rootJobID,
		now:               now,
		maximumRetries:    maximumRetries,
		preCommitSnapshot: &preCommitSnapshot,
	}

	rows, err := transaction.Query(ctx, `
		SELECT endpoint, retry_count, next_retry_at
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
		ORDER BY endpoint
		FOR UPDATE
	`, attemptID)
	if err != nil {
		return fmt.Errorf("select version-two incomplete endpoints: %w", err)
	}
	type incompleteEndpointV2 struct {
		name        endpointName
		retryCount  int
		nextRetryAt pgtype.Timestamptz
	}
	var incomplete []incompleteEndpointV2
	for rows.Next() {
		var endpoint incompleteEndpointV2
		if err := rows.Scan(&endpoint.name, &endpoint.retryCount, &endpoint.nextRetryAt); err != nil {
			rows.Close()
			return fmt.Errorf("scan version-two incomplete endpoint: %w", err)
		}
		incomplete = append(incomplete, endpoint)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("read version-two incomplete endpoints: %w", err)
	}
	rows.Close()

	setJobStatuses := func(rootStatus, currentStatus string) error {
		if rootJobID == job.id {
			command, err := transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET status = $6,
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
					AND (result_attempt_id = $7 OR parent_attempt_id = $7)
			`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, now, rootStatus, attemptID)
			if err != nil {
				return fmt.Errorf("update version-two terminal collector job: %w", err)
			}
			if command.RowsAffected() != 1 {
				return errLeaseLost
			}
			return nil
		}

		command, err := transaction.Exec(ctx, `
			UPDATE collector_jobs AS root_job
			SET status = $7,
				updated_at = $6
			FROM collector_jobs AS current_job
			WHERE root_job.id = $1
				AND current_job.id = $2
				AND root_job.status IN ('waiting_retry', 'leased')
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $8 OR current_job.parent_attempt_id = $8)
		`, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, now, rootStatus, attemptID)
		if err != nil {
			return fmt.Errorf("update version-two root collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}

		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = $6,
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
				AND (result_attempt_id = $7 OR parent_attempt_id = $7)
			`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, now, currentStatus, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two retry collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
		return nil
	}

	if len(incomplete) == 0 {
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
		`, attemptID, now, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		if err != nil {
			return fmt.Errorf("complete version-two collection attempt: %w", err)
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
		`, rootJobID, now, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, attemptID); err != nil {
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
		if err := setJobStatuses("complete", "complete"); err != nil {
			return err
		}
		if err := s.commitTransaction(ctx, transaction); err != nil {
			return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
				return s.proveAttemptResolutionCommit(proofCtx, connection, commitIntent)
			})
		}
		return nil
	}

	terminal := false
	for _, endpoint := range incomplete {
		if job.workType == "endpoint_retry" && string(endpoint.name) != job.requiredEndpoint.String {
			continue
		}
		if endpoint.retryCount >= maximumRetries {
			terminal = true
			command, err := transaction.Exec(ctx, `
				UPDATE collector_endpoint_results AS endpoint_result
				SET outcome = 'failed', next_retry_at = NULL
				FROM collector_jobs AS current_job
				WHERE endpoint_result.attempt_id = $1
					AND endpoint_result.endpoint = $2
					AND current_job.id = $3
					AND current_job.lease_owner = $4
					AND current_job.lease_token = $5
					AND current_job.lease_generation = $6
					AND current_job.status = 'leased'
					AND current_job.lease_expires_at > clock_timestamp()
					AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
			`, attemptID, string(endpoint.name), job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
			if err != nil {
				return fmt.Errorf("mark version-two endpoint terminal: %w", err)
			}
			if command.RowsAffected() != 1 {
				return errLeaseLost
			}
			continue
		}

		nextRetryCount := endpoint.retryCount + 1
		dueAt := now
		if endpoint.nextRetryAt.Valid && endpoint.nextRetryAt.Time.After(now) {
			dueAt = endpoint.nextRetryAt.Time
		}
		coalescingKey := "retry:" + strconv.FormatInt(attemptID, 10) + ":" + string(endpoint.name) + ":" + strconv.Itoa(nextRetryCount)
		var normalizedTag any = job.normalizedTag
		if job.scope == "global" {
			normalizedTag = nil
		}
		command, err := transaction.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type, scope, player_id, normalized_tag, capacity_pool,
				priority, due_at, coalescing_key, sweep_id,
				reset_baseline_sweep_id, parent_attempt_id,
				required_endpoint, lease_generation, status
			)
			SELECT 'endpoint_retry', $1, $2, $3, $4, 300, $5, $6,
				$7, $8, $9, $10, current_job.lease_generation, 'pending'
			FROM collector_jobs AS current_job
			WHERE current_job.id = $11
				AND current_job.lease_owner = $12
				AND current_job.lease_token = $13
				AND current_job.lease_generation = $14
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $9 OR current_job.parent_attempt_id = $9)
			ON CONFLICT DO NOTHING
		`, job.scope, job.playerID, normalizedTag, string(job.pool), dueAt,
			coalescingKey, job.sweepID, job.resetBaselineSweepID, attemptID,
			string(endpoint.name), job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		if err != nil {
			return fmt.Errorf("insert version-two endpoint retry: %w", err)
		}
		if command.RowsAffected() == 0 {
			if err := lockCurrentAttemptV2(ctx, transaction, job, attemptID); err != nil {
				return err
			}
		}

		command, err = transaction.Exec(ctx, `
			UPDATE collector_endpoint_results AS endpoint_result
			SET outcome = 'retrying', retry_count = $3, next_retry_at = $4
			FROM collector_jobs AS current_job
			WHERE endpoint_result.attempt_id = $1
				AND endpoint_result.endpoint = $2
				AND current_job.id = $5
				AND current_job.lease_owner = $6
				AND current_job.lease_token = $7
				AND current_job.lease_generation = $8
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		`, attemptID, string(endpoint.name), nextRetryCount, dueAt, job.id,
			job.leaseOwner, job.leaseToken, job.leaseGeneration)
		if err != nil {
			return fmt.Errorf("mark version-two endpoint retrying: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
	}

	command, err := transaction.Exec(ctx, `
		UPDATE collector_attempts AS attempt
		SET status = CASE WHEN $2 THEN 'failed' ELSE 'incomplete' END
		FROM collector_jobs AS current_job
		WHERE attempt.id = $1
			AND attempt.status IN ('running', 'incomplete')
			AND current_job.id = $3
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
	`, attemptID, terminal, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("update version-two collection attempt state: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}

	baselineState := "incomplete"
	if terminal {
		baselineState = "failed"
	}
	if _, err := transaction.Exec(ctx, `
		UPDATE collector_reset_baseline_sweeps AS baseline
		SET state = $2,
			completed_at = CASE WHEN $2 = 'failed' THEN $3::timestamptz ELSE NULL::timestamptz END
		FROM collector_jobs AS root_job
		JOIN collector_jobs AS current_job ON current_job.id = $4
		WHERE root_job.id = $1
			AND baseline.id = root_job.reset_baseline_sweep_id
			AND baseline.evidence_kind = 'paired_v2'
			AND current_job.lease_owner = $5
			AND current_job.lease_token = $6
			AND current_job.lease_generation = $7
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $8 OR current_job.parent_attempt_id = $8)
	`, rootJobID, baselineState, now, job.id, job.leaseOwner, job.leaseToken,
		job.leaseGeneration, attemptID); err != nil {
		return fmt.Errorf("update version-two reset baseline state: %w", err)
	}

	eventType := "retry_scheduled"
	eventToStatus := "incomplete"
	if terminal {
		eventType = "failed"
		eventToStatus = "failed"
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_attempt_events (
			job_id, attempt_id, event_type, from_status, to_status,
			lease_owner, lease_token, lease_generation
		)
		SELECT current_job.id, $1, $2, $3, $4,
			current_job.lease_owner, current_job.lease_token, current_job.lease_generation
		FROM collector_jobs AS current_job
		WHERE current_job.id = $5
			AND current_job.lease_owner = $6
			AND current_job.lease_token = $7
			AND current_job.lease_generation = $8
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
	`, attemptID, eventType, preCommitSnapshot.attempt.status, eventToStatus, job.id,
		job.leaseOwner, job.leaseToken, job.leaseGeneration); err != nil {
		return fmt.Errorf("record version-two attempt resolution event: %w", err)
	}

	if terminal {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs AS sibling
			SET status = 'cancelled',
				cancel_reason = 'attempt_terminal',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $6
			FROM collector_jobs AS current_job
			WHERE sibling.parent_attempt_id = $1
				AND sibling.id <> current_job.id
				AND sibling.status IN ('pending', 'leased', 'waiting_retry')
				AND current_job.id = $2
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		`, attemptID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, now); err != nil {
			return fmt.Errorf("cancel version-two sibling retry jobs: %w", err)
		}
		if err := lockCurrentAttemptV2(ctx, transaction, job, attemptID); err != nil {
			return err
		}
		if err := setJobStatuses("failed", "failed"); err != nil {
			return err
		}
	} else {
		if err := setJobStatuses("waiting_retry", "complete"); err != nil {
			return err
		}
	}

	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveAttemptResolutionCommit(proofCtx, connection, commitIntent)
		})
	}
	return nil
}
