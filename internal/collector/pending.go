package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// pendingRemoteVerification is operational state only. It intentionally has
// no body, credential, authorization, or request-only secret fields. The
// response metadata is bounded and is enough to finish the original handoff.
type pendingRemoteVerification struct {
	ResponseHash      string            `json:"response_hash"`
	ArchiveReference  string            `json:"archive_reference"`
	ByteSize          int64             `json:"byte_size"`
	ArchiveInstanceID string            `json:"archive_instance_id"`
	Endpoint          string            `json:"endpoint"`
	AttemptID         int64             `json:"attempt_id"`
	RequestCount      int               `json:"request_count"`
	StatusCode        int               `json:"status_code"`
	RequestStartedAt  time.Time         `json:"request_started_at"`
	CompletedAt       time.Time         `json:"completed_at"`
	Headers           map[string]string `json:"headers,omitempty"`
	PagingState       string            `json:"paging_state,omitempty"`
	KeyLabel          string            `json:"key_label,omitempty"`
	NormalizedTag     string            `json:"normalized_tag,omitempty"`
}

func (p pendingRemoteVerification) response() officialResponse {
	provenance, _, _ := officialRequest(endpointName(p.Endpoint), p.NormalizedTag)
	return officialResponse{
		requestStartedAt:    p.RequestStartedAt,
		responseCompletedAt: p.CompletedAt,
		statusCode:          p.StatusCode,
		headers:             p.Headers,
		request:             provenance,
		pagingEnvelopeState: p.PagingState,
	}
}

func (s *store) pendingRemoteVerification(ctx context.Context, job *collectionJob, attemptID int64, endpoint endpointName) (*pendingRemoteVerification, error) {
	var payload []byte
	err := s.pool.QueryRow(ctx, `
		SELECT pending_remote_verification
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = $2 AND outcome = 'pending_remote_verification'
	`, attemptID, string(endpoint)).Scan(&payload)
	if err != nil {
		if err == pgx.ErrNoRows {
			return nil, nil
		}
		return nil, fmt.Errorf("read pending remote verification: %w", err)
	}
	var pending pendingRemoteVerification
	if err := json.Unmarshal(payload, &pending); err != nil {
		return nil, fmt.Errorf("decode pending remote verification: %w", err)
	}
	if pending.ResponseHash == "" || pending.ArchiveReference == "" || pending.ByteSize < 0 ||
		pending.ArchiveInstanceID != s.archiveInstanceID || pending.AttemptID != attemptID ||
		pending.Endpoint != string(endpoint) || pending.RequestCount < 1 || pending.StatusCode < 100 {
		return nil, fmt.Errorf("pending remote verification is a terminal configuration contradiction")
	}
	return &pending, nil
}

func (s *store) beginPendingRemoteVerification(ctx context.Context, job *collectionJob, attemptID int64, endpoint endpointName, startedAt time.Time) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE collector_endpoint_results AS result
		SET execution_token = $3,
			key_label = COALESCE(key_label, $4),
			request_started_at = COALESCE(request_started_at, $5)
		FROM collector_jobs AS current_job
		WHERE result.attempt_id = $1 AND result.endpoint = $2
		  AND result.outcome = 'pending_remote_verification'
		  AND current_job.id = $6
		  AND current_job.lease_owner = $7 AND current_job.lease_token = $3
		  AND current_job.lease_generation = $8 AND current_job.status = 'leased'
		  AND current_job.lease_expires_at > clock_timestamp()
	`, attemptID, string(endpoint), job.leaseToken, "pending-resume", startedAt, job.id, job.leaseOwner, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("begin pending remote verification: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	return nil
}

func (s *store) clearPendingRemoteVerification(ctx context.Context, job *collectionJob, attemptID int64, endpoint endpointName) error {
	command, err := s.pool.Exec(ctx, `
		UPDATE collector_endpoint_results AS result
		SET outcome = 'pending', response_hash = NULL, archive_reference = NULL,
			pending_remote_verification = NULL, failure_category = NULL,
			execution_token = $3, next_retry_at = NULL
		FROM collector_jobs AS current_job
		WHERE result.attempt_id = $1 AND result.endpoint = $2
		  AND result.outcome = 'pending_remote_verification'
		  AND current_job.id = $4 AND current_job.lease_owner = $5
		  AND current_job.lease_token = $3 AND current_job.lease_generation = $6
		  AND current_job.status = 'leased' AND current_job.lease_expires_at > clock_timestamp()
	`, attemptID, string(endpoint), job.leaseToken, job.id, job.leaseOwner, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("clear pending remote verification: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	return nil
}

func (s *store) setPendingRemoteVerification(ctx context.Context, job *collectionJob, attemptID int64, endpoint endpointName, hash, reference string, size int64, requestCount int, response officialResponse, keyLabel string) error {
	payload, err := json.Marshal(pendingRemoteVerification{
		ResponseHash: hash, ArchiveReference: reference, ByteSize: size,
		ArchiveInstanceID: s.archiveInstanceID, Endpoint: string(endpoint),
		AttemptID: attemptID, RequestCount: requestCount, StatusCode: response.statusCode,
		RequestStartedAt: response.requestStartedAt, CompletedAt: response.responseCompletedAt,
		Headers: response.headers, PagingState: response.pagingEnvelopeState, KeyLabel: keyLabel, NormalizedTag: job.normalizedTag,
	})
	if err != nil {
		return err
	}
	command, err := s.pool.Exec(ctx, `
		UPDATE collector_endpoint_results AS result
		SET outcome = 'pending_remote_verification', response_hash = $4,
			archive_reference = $5, pending_remote_verification = $6::jsonb,
			request_started_at = $7, response_completed_at = $8, http_status = $9,
			failure_category = 'archive_network_uncertain', execution_token = $3,
			key_label = $10, next_retry_at = clock_timestamp() + interval '5 seconds'
		FROM collector_jobs AS current_job
		WHERE result.attempt_id = $1 AND result.endpoint = $2
		  AND current_job.id = $11 AND current_job.lease_owner = $12
		  AND current_job.lease_token = $3 AND current_job.lease_generation = $13
		  AND current_job.status = 'leased' AND current_job.lease_expires_at > clock_timestamp()
	`, attemptID, string(endpoint), job.leaseToken, hash, reference, string(payload), response.requestStartedAt, response.responseCompletedAt, response.statusCode, keyLabel, job.id, job.leaseOwner, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("persist pending remote verification: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	return nil
}
