package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
)

func (s *store) beginEndpointRequestV2(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	startedAt time.Time,
) (int, error) {
	provenance, _, err := officialRequest(endpoint, job.normalizedTag)
	if err != nil {
		return 0, err
	}
	pagingState := "not_applicable"
	if endpoint == globalPlayerRankingsEndpoint {
		pagingState = "malformed"
	}
	var requestCount int
	err = s.pool.QueryRow(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET request_count = request_count + 1,
			execution_token = $3,
			request_started_at = $4,
			key_label = NULL,
			request_method = $6,
			request_path = $7,
			request_query = $8,
			paging_envelope_state = $9,
			source_adapter_version = $10
		FROM collector_jobs AS job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND job.id = $5
			AND job.lease_owner = $12
			AND job.lease_token = $3
			AND job.lease_generation = $11
			AND job.status = 'leased'
			AND job.lease_expires_at > clock_timestamp()
		RETURNING endpoint_result.request_count
	`, attemptID, string(endpoint), job.leaseToken, startedAt, job.id,
		provenance.method, provenance.path, provenance.query, pagingState,
		provenance.sourceAdapterVersion, job.leaseGeneration, job.leaseOwner).Scan(&requestCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, errLeaseLost
	}
	if err != nil {
		return 0, fmt.Errorf("begin version-two endpoint request: %w", err)
	}
	return requestCount, nil
}

func (s *store) commitObservationV2(
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
	headers, err := json.Marshal(response.headers)
	if err != nil {
		return fmt.Errorf("encode version-two evidence headers: %w", err)
	}
	intent := newObservationCommitIntent(
		job, attemptID, endpoint, requestCount, response, headers, hash,
		archiveReference, collectorVersion, keyLabel, outcome, nextRetryAt,
	)
	poolStartedAt := time.Now()
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if s.metrics != nil {
		s.metrics.recordStageDuration("database_pool_acquire", time.Since(poolStartedAt))
	}
	if err != nil {
		return fmt.Errorf("begin version-two observation transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var leaseCurrent bool
	lockStartedAt := time.Now()
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
	`, job.id, job.leaseToken, job.leaseOwner, job.leaseGeneration).Scan(&leaseCurrent); err != nil {
		if s.metrics != nil {
			s.metrics.recordStageDuration("observation_job_lock", time.Since(lockStartedAt))
		}
		if errors.Is(err, pgx.ErrNoRows) {
			return errLeaseLost
		}
		return fmt.Errorf("lock version-two observation lease: %w", err)
	}
	if s.metrics != nil {
		s.metrics.recordStageDuration("observation_job_lock", time.Since(lockStartedAt))
	}
	if s.contractVersion >= 3 {
		if err := s.insertCatalogue(ctx, transaction, hash, archiveReference, int64(len(response.body))); err != nil {
			return fmt.Errorf("insert verified archive catalogue row: %w", err)
		}
		var catalogueHash, catalogueReference, catalogueInstance string
		var catalogueSize int64
		if err := transaction.QueryRow(ctx, `SELECT response_hash, archive_reference, byte_size, archive_instance_id FROM archive_catalogue WHERE response_hash = $1`, hash).Scan(&catalogueHash, &catalogueReference, &catalogueSize, &catalogueInstance); err != nil {
			return fmt.Errorf("read verified archive catalogue row: %w", err)
		}
		if catalogueHash != hash || catalogueReference != archiveReference || catalogueSize != int64(len(response.body)) || catalogueInstance != s.archiveInstanceID {
			return fmt.Errorf("%w: hash=%q reference=%q size=%d instance=%q vs row reference=%q size=%d instance=%q",
				errArchiveCatalogueContradiction, hash, archiveReference,
				int64(len(response.body)), s.archiveInstanceID,
				catalogueReference, catalogueSize, catalogueInstance)
		}
	}

	var normalizedTag any = job.normalizedTag
	if job.scope == "global" {
		normalizedTag = nil
	}
	occurrenceKey := strconv.FormatInt(attemptID, 10) + ":" + string(endpoint) + ":" + strconv.Itoa(requestCount)
	var observationID int64
	observationQuery := `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, player_id,
			normalized_tag, endpoint, request_method, request_path, request_query,
			request_started_at, response_completed_at, http_status, response_hash,
			archive_reference, paging_envelope_state, collector_version,
			source_adapter_version, key_label, evidence_headers
		)
		SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
			$11, $12, $13, $14, $15, $16, $17, $18, $19, $20
		FROM collector_jobs AS current_job
		WHERE current_job.id = $2
			AND current_job.lease_owner = $21
			AND current_job.lease_token = $22
			AND current_job.lease_generation = $23
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
		ON CONFLICT (occurrence_key) DO NOTHING
		RETURNING id`
	if s.contractVersion >= 3 {
		observationQuery = `
		INSERT INTO collector_observations (
			occurrence_key, collection_job_id, attempt_id, scope, player_id,
			normalized_tag, endpoint, request_method, request_path, request_query,
			request_started_at, response_completed_at, http_status, response_hash,
			archive_reference, archive_catalogue_hash, paging_envelope_state, collector_version,
			source_adapter_version, key_label, evidence_headers
		)
		SELECT $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$14,$16,$17,$18,$19,$20
		FROM collector_jobs AS current_job
		WHERE current_job.id = $2 AND current_job.lease_owner = $21
			AND current_job.lease_token = $22 AND current_job.lease_generation = $23
			AND current_job.status = 'leased' AND current_job.lease_expires_at > clock_timestamp()
		ON CONFLICT (occurrence_key) DO NOTHING RETURNING id`
	}
	err = transaction.QueryRow(ctx, observationQuery, occurrenceKey, job.id, attemptID, job.scope, job.playerID, normalizedTag,
		string(endpoint), response.request.method, response.request.path,
		response.request.query, response.requestStartedAt, response.responseCompletedAt,
		response.statusCode, hash, archiveReference, response.pagingEnvelopeState,
		collectorVersion, response.request.sourceAdapterVersion, keyLabel, headers,
		job.leaseOwner, job.leaseToken, job.leaseGeneration).Scan(&observationID)
	if errors.Is(err, pgx.ErrNoRows) {
		if leaseErr := lockCurrentLeaseV2(ctx, transaction, job); leaseErr != nil {
			return leaseErr
		}
		err = transaction.QueryRow(ctx, `
			SELECT id FROM collector_observations WHERE occurrence_key = $1
		`, occurrenceKey).Scan(&observationID)
	}
	if err != nil {
		return fmt.Errorf("insert version-two observation occurrence: %w", err)
	}

	command, err := transaction.Exec(ctx, `
		INSERT INTO python_processing_jobs (observation_id, parser_version)
		SELECT $1, $2
		FROM collector_jobs AS current_job
		WHERE current_job.id = $3
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
		ON CONFLICT (observation_id) DO NOTHING
	`, observationID, intent.parserVersion, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("insert version-two Python processing job: %w", err)
	}
	if command.RowsAffected() == 0 {
		if err := lockCurrentLeaseV2(ctx, transaction, job); err != nil {
			return err
		}
	}
	// Contract v3 forbids any non-pending outcome while a pending handoff is
	// attached, so the verified commit clears the pointer in this same
	// statement instead of a follow-up update.
	pendingClear := ""
	if s.contractVersion >= 3 {
		pendingClear = "\n\t\t\tpending_remote_verification = NULL,"
	}
	endpointUpdate := `
		UPDATE collector_endpoint_results
		SET outcome = $4,` + pendingClear + `
			next_retry_at = $5,
			request_started_at = $6,
			response_completed_at = $7,
			http_status = $8,
			response_hash = $9,
			archive_reference = $10,
			observation_id = $11,
			failure_category = NULL,
			key_label = $12,
			request_method = $14,
			request_path = $15,
			request_query = $16,
			paging_envelope_state = $17,
			source_adapter_version = $18
		WHERE attempt_id = $1
			AND endpoint = $2
			AND execution_token = $3
			AND EXISTS (
				SELECT 1
				FROM collector_jobs AS current_job
				WHERE current_job.id = $13
					AND current_job.lease_owner = $19
					AND current_job.lease_token = $3
					AND current_job.lease_generation = $20
					AND current_job.status = 'leased'
					AND current_job.lease_expires_at > clock_timestamp()
					AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
			)
	`
	command, err = transaction.Exec(ctx, endpointUpdate, attemptID, string(endpoint), job.leaseToken, outcome, nextRetryAt,
		response.requestStartedAt, response.responseCompletedAt, response.statusCode,
		hash, archiveReference, observationID, keyLabel, job.id,
		response.request.method, response.request.path, response.request.query,
		response.pagingEnvelopeState, response.request.sourceAdapterVersion,
		job.leaseOwner, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("update version-two endpoint result: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveObservationCommit(proofCtx, connection, intent)
		})
	}
	return nil
}

func (s *store) recordTransportFailureV2(
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
	provenance, _, err := officialRequest(endpoint, job.normalizedTag)
	if err != nil {
		return err
	}
	requestCount, err := s.readEndpointRequestCount(ctx, job, attemptID, endpoint)
	if err != nil {
		return err
	}
	failureCategory := category
	keyLabelValue := keyLabel
	intent := endpointMutationIntent{
		job:                  *job,
		jobID:                job.id,
		attemptID:            attemptID,
		scope:                job.scope,
		playerID:             job.playerID,
		normalizedTag:        job.normalizedTag,
		leaseOwner:           job.leaseOwner,
		leaseToken:           job.leaseToken,
		leaseGeneration:      job.leaseGeneration,
		endpoint:             endpoint,
		requestCount:         requestCount,
		requestMethod:        provenance.method,
		requestPath:          provenance.path,
		requestQuery:         provenance.query,
		pagingEnvelopeState:  "unknown_no_response",
		sourceAdapterVersion: provenance.sourceAdapterVersion,
		requestStartedAt:     startedAt,
		outcome:              "transport_failed",
		nextRetryAt:          &nextRetryAt,
		failureCategory:      &failureCategory,
		keyLabel:             &keyLabelValue,
	}
	evidenceKey := fmt.Sprintf("transport-v2:%d:%s:%d", attemptID, endpoint, requestCount)
	if proofOutcome, proofErr := s.probeFreshConnection(ctx, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
		return s.proveTransportFailureCommit(proofCtx, connection, intent, evidenceKey, failedAt, "retryable")
	}); proofErr == nil && proofOutcome == commitProofCommitted {
		return nil
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin version-two transport failure transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	if err := lockCurrentLeaseV2(ctx, transaction, job); err != nil {
		return err
	}
	var normalizedTag any = job.normalizedTag
	if job.scope == "global" {
		normalizedTag = nil
	}
	command, err := transaction.Exec(ctx, `
		INSERT INTO collector_transport_failures (
			collection_job_id, attempt_id, scope, player_id, normalized_tag,
			endpoint, request_method, request_path, request_query,
			paging_envelope_state, source_adapter_version, request_started_at,
			failed_at, failure_category, retry_state, key_label, evidence_key
		)
		SELECT $1, $2, $3, $4, $5, $6, $8, $9, $10,
			'unknown_no_response', $11, $12, $13, $14, 'retryable', $15, $18
		FROM collector_jobs AS current_job
		WHERE current_job.id = $1
			AND current_job.lease_owner = $16
			AND current_job.lease_token = $7
			AND current_job.lease_generation = $17
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $2 OR current_job.parent_attempt_id = $2)
		ON CONFLICT (evidence_key) DO NOTHING
	`, job.id, attemptID, job.scope, job.playerID, normalizedTag, string(endpoint),
		job.leaseToken, provenance.method, provenance.path, provenance.query,
		provenance.sourceAdapterVersion, startedAt, failedAt, category, keyLabel,
		job.leaseOwner, job.leaseGeneration, evidenceKey)
	if err != nil {
		return fmt.Errorf("insert version-two transport failure: %w", err)
	}
	command, err = transaction.Exec(ctx, `
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
			key_label = $6,
			request_method = $9,
			request_path = $10,
			request_query = $11,
			paging_envelope_state = 'unknown_no_response',
			source_adapter_version = $12
		FROM collector_jobs AS current_job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND endpoint_result.execution_token = $7
			AND current_job.id = $8
			AND current_job.lease_owner = $13
			AND current_job.lease_token = $7
			AND current_job.lease_generation = $14
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
			AND endpoint_result.request_count = $15
	`, attemptID, string(endpoint), startedAt, nextRetryAt, category, keyLabel,
		job.leaseToken, job.id, provenance.method, provenance.path,
		provenance.query, provenance.sourceAdapterVersion,
		job.leaseOwner, job.leaseGeneration, intent.requestCount)
	if err != nil {
		return fmt.Errorf("update version-two transport-failed endpoint: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveTransportFailureCommit(proofCtx, connection, intent, evidenceKey, failedAt, "retryable")
		})
	}
	return nil
}

func (s *store) recordStorageFailureV2(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	response officialResponse,
	category string,
	keyLabel string,
) error {
	requestCount, err := s.readEndpointRequestCount(ctx, job, attemptID, endpoint)
	if err != nil {
		return err
	}
	failureCategory := category
	keyLabelValue := keyLabel
	statusCode := response.statusCode
	intent := endpointMutationIntent{
		job:                  *job,
		jobID:                job.id,
		attemptID:            attemptID,
		scope:                job.scope,
		playerID:             job.playerID,
		normalizedTag:        job.normalizedTag,
		leaseOwner:           job.leaseOwner,
		leaseToken:           job.leaseToken,
		leaseGeneration:      job.leaseGeneration,
		endpoint:             endpoint,
		requestCount:         requestCount,
		requestMethod:        response.request.method,
		requestPath:          response.request.path,
		requestQuery:         response.request.query,
		pagingEnvelopeState:  response.pagingEnvelopeState,
		sourceAdapterVersion: response.request.sourceAdapterVersion,
		requestStartedAt:     response.requestStartedAt,
		responseCompletedAt:  &response.responseCompletedAt,
		httpStatus:           &statusCode,
		outcome:              "storage_failed",
		failureCategory:      &failureCategory,
		keyLabel:             &keyLabelValue,
	}
	if proofOutcome, proofErr := s.probeFreshConnection(ctx, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
		return s.proveStorageFailureCommit(proofCtx, connection, intent)
	}); proofErr == nil && proofOutcome == commitProofCommitted {
		return nil
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin version-two storage failure transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()
	if err := lockCurrentLeaseV2(ctx, transaction, job); err != nil {
		return err
	}
	// Contract v3 forbids a non-pending outcome while a pending handoff is
	// attached. A terminal storage failure clears the pointer in this same
	// lease-fenced statement so the CHECK passes and no requeue resumes it.
	pendingClear := ""
	if s.contractVersion >= 3 {
		pendingClear = "\n\t\t\tpending_remote_verification = NULL,"
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET outcome = 'storage_failed',`+pendingClear+`
			request_started_at = $4,
			response_completed_at = $5,
			http_status = $6,
			response_hash = NULL,
			archive_reference = NULL,
			observation_id = NULL,
			failure_category = $7,
			key_label = $8,
			request_method = $10,
			request_path = $11,
			request_query = $12,
			paging_envelope_state = $13,
			source_adapter_version = $14
		FROM collector_jobs AS current_job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND endpoint_result.execution_token = $3
			AND current_job.id = $9
			AND current_job.lease_owner = $15
			AND current_job.lease_token = $3
			AND current_job.lease_generation = $16
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
			AND endpoint_result.request_count = $17
	`, attemptID, string(endpoint), job.leaseToken, response.requestStartedAt,
		response.responseCompletedAt, response.statusCode, category, keyLabel,
		job.id, response.request.method, response.request.path, response.request.query,
		response.pagingEnvelopeState, response.request.sourceAdapterVersion,
		job.leaseOwner, job.leaseGeneration, intent.requestCount)
	if err != nil {
		return fmt.Errorf("record version-two storage-failed endpoint: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveStorageFailureCommit(proofCtx, connection, intent)
		})
	}
	return nil
}
