package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
)

var errCommitOutcomeUnknown = errors.New("collector transaction commit outcome unknown")

type commitProofOutcome uint8

const (
	commitProofNotCommitted commitProofOutcome = iota
	commitProofCommitted
	commitProofUnknown
)

type commitOutcomeNotCommittedError struct {
	cause error
}

func (e *commitOutcomeNotCommittedError) Error() string {
	return e.cause.Error()
}

func (e *commitOutcomeNotCommittedError) Unwrap() error {
	return e.cause
}

func commitOutcomeUnknownError(commitErr, evidenceErr error) error {
	if evidenceErr == nil {
		evidenceErr = errors.New("durable state is partial or contradictory")
	}
	return errors.Join(errCommitOutcomeUnknown, commitErr, evidenceErr)
}

func (s *store) commitTransaction(ctx context.Context, transaction pgx.Tx) error {
	if s.commitTx != nil {
		return s.commitTx(ctx, transaction)
	}
	return transaction.Commit(ctx)
}

func (s *store) reconcileCommitError(
	ctx context.Context,
	commitErr error,
	proof func(context.Context, *pgx.Conn) (commitProofOutcome, error),
) error {
	startedAt := time.Now()
	defer func() {
		if s.metrics != nil {
			s.metrics.recordStageDuration("ambiguous_commit_proof", time.Since(startedAt))
		}
	}()
	connection, err := s.pool.Acquire(ctx)
	if err != nil {
		return commitOutcomeUnknownError(commitErr, fmt.Errorf("acquire fresh reconciliation connection: %w", err))
	}
	defer connection.Release()

	outcome, err := proof(ctx, connection.Conn())
	if err != nil {
		return commitOutcomeUnknownError(commitErr, fmt.Errorf("read durable commit evidence: %w", err))
	}
	switch outcome {
	case commitProofCommitted:
		return nil
	case commitProofNotCommitted:
		return &commitOutcomeNotCommittedError{cause: commitErr}
	default:
		return commitOutcomeUnknownError(commitErr, nil)
	}
}

func (s *store) probeFreshConnection(
	ctx context.Context,
	proof func(context.Context, *pgx.Conn) (commitProofOutcome, error),
) (commitProofOutcome, error) {
	connection, err := s.pool.Acquire(ctx)
	if err != nil {
		return commitProofUnknown, fmt.Errorf("acquire fresh preflight connection: %w", err)
	}
	defer connection.Release()
	return proof(ctx, connection.Conn())
}

type observationCommitIntent struct {
	job              collectionJob
	jobID            int64
	attemptID        int64
	scope            string
	playerID         pgtype.Int8
	normalizedTag    string
	leaseOwner       string
	leaseToken       string
	leaseGeneration  int64
	endpoint         endpointName
	requestCount     int
	occurrenceKey    string
	response         officialResponse
	headers          []byte
	hash             string
	archiveReference string
	collectorVersion string
	keyLabel         string
	outcome          string
	nextRetryAt      *time.Time
	parserVersion    string
}

func newObservationCommitIntent(
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	requestCount int,
	response officialResponse,
	headers []byte,
	hash string,
	archiveReference string,
	collectorVersion string,
	keyLabel string,
	outcome string,
	nextRetryAt *time.Time,
) observationCommitIntent {
	return observationCommitIntent{
		job:              *job,
		jobID:            job.id,
		attemptID:        attemptID,
		scope:            job.scope,
		playerID:         job.playerID,
		normalizedTag:    job.normalizedTag,
		leaseOwner:       job.leaseOwner,
		leaseToken:       job.leaseToken,
		leaseGeneration:  job.leaseGeneration,
		endpoint:         endpoint,
		requestCount:     requestCount,
		occurrenceKey:    fmt.Sprintf("%d:%s:%d", attemptID, endpoint, requestCount),
		response:         response,
		headers:          append([]byte(nil), headers...),
		hash:             hash,
		archiveReference: archiveReference,
		collectorVersion: collectorVersion,
		keyLabel:         keyLabel,
		outcome:          outcome,
		nextRetryAt:      nextRetryAt,
		parserVersion:    parserVersionForEndpoint(endpoint),
	}
}

func parserVersionForEndpoint(endpoint endpointName) string {
	return "supercell-source-parser-v1"
}

type endpointResultRow struct {
	attemptID            int64
	endpoint             string
	outcome              string
	requestStartedAt     pgtype.Timestamptz
	responseCompletedAt  pgtype.Timestamptz
	httpStatus           pgtype.Int4
	responseHash         pgtype.Text
	archiveReference     pgtype.Text
	observationID        pgtype.Int8
	requestCount         int
	executionToken       pgtype.Text
	nextRetryAt          pgtype.Timestamptz
	failureCategory      pgtype.Text
	keyLabel             pgtype.Text
	requestMethod        pgtype.Text
	requestPath          pgtype.Text
	requestQuery         pgtype.Text
	pagingEnvelopeState  pgtype.Text
	sourceAdapterVersion pgtype.Text
}

func readEndpointResult(ctx context.Context, connection *pgx.Conn, attemptID int64, endpoint endpointName) (endpointResultRow, bool, error) {
	var row endpointResultRow
	err := connection.QueryRow(ctx, `
		SELECT attempt_id, endpoint, outcome, request_started_at,
		       response_completed_at, http_status, response_hash,
		       archive_reference, observation_id, request_count, execution_token,
		       next_retry_at, failure_category, key_label, request_method,
		       request_path, request_query, paging_envelope_state,
		       source_adapter_version
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = $2
	`, attemptID, string(endpoint)).Scan(
		&row.attemptID,
		&row.endpoint,
		&row.outcome,
		&row.requestStartedAt,
		&row.responseCompletedAt,
		&row.httpStatus,
		&row.responseHash,
		&row.archiveReference,
		&row.observationID,
		&row.requestCount,
		&row.executionToken,
		&row.nextRetryAt,
		&row.failureCategory,
		&row.keyLabel,
		&row.requestMethod,
		&row.requestPath,
		&row.requestQuery,
		&row.pagingEnvelopeState,
		&row.sourceAdapterVersion,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return endpointResultRow{}, false, nil
	}
	if err != nil {
		return endpointResultRow{}, false, err
	}
	return row, true, nil
}

type attemptFenceRow struct {
	jobID           int64
	leaseOwner      pgtype.Text
	leaseToken      pgtype.Text
	leaseGeneration int64
}

func readAttemptFence(ctx context.Context, connection *pgx.Conn, attemptID int64) (attemptFenceRow, bool, error) {
	var row attemptFenceRow
	err := connection.QueryRow(ctx, `
		SELECT job_id, lease_owner, lease_token, lease_generation
		FROM collector_attempts
		WHERE id = $1
	`, attemptID).Scan(&row.jobID, &row.leaseOwner, &row.leaseToken, &row.leaseGeneration)
	if errors.Is(err, pgx.ErrNoRows) {
		return attemptFenceRow{}, false, nil
	}
	if err != nil {
		return attemptFenceRow{}, false, err
	}
	return row, true, nil
}

type observationRow struct {
	id                   int64
	occurrenceKey        string
	collectionJobID      int64
	attemptID            int64
	scope                string
	playerID             pgtype.Int8
	normalizedTag        pgtype.Text
	endpoint             string
	requestMethod        string
	requestPath          string
	requestQuery         string
	requestStartedAt     time.Time
	responseCompletedAt  time.Time
	httpStatus           int
	responseHash         string
	archiveReference     string
	pagingEnvelopeState  string
	collectorVersion     string
	sourceAdapterVersion string
	keyLabel             string
	evidenceHeaders      []byte
}

func readObservation(ctx context.Context, connection *pgx.Conn, occurrenceKey string) (observationRow, bool, error) {
	var row observationRow
	err := connection.QueryRow(ctx, `
		SELECT id, occurrence_key, collection_job_id, attempt_id, scope,
		       player_id, normalized_tag, endpoint, request_method, request_path,
		       request_query, request_started_at, response_completed_at,
		       http_status, response_hash, archive_reference,
		       paging_envelope_state, collector_version, source_adapter_version,
		       key_label, evidence_headers
		FROM collector_observations
		WHERE occurrence_key = $1
	`, occurrenceKey).Scan(
		&row.id,
		&row.occurrenceKey,
		&row.collectionJobID,
		&row.attemptID,
		&row.scope,
		&row.playerID,
		&row.normalizedTag,
		&row.endpoint,
		&row.requestMethod,
		&row.requestPath,
		&row.requestQuery,
		&row.requestStartedAt,
		&row.responseCompletedAt,
		&row.httpStatus,
		&row.responseHash,
		&row.archiveReference,
		&row.pagingEnvelopeState,
		&row.collectorVersion,
		&row.sourceAdapterVersion,
		&row.keyLabel,
		&row.evidenceHeaders,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return observationRow{}, false, nil
	}
	if err != nil {
		return observationRow{}, false, err
	}
	return row, true, nil
}

type pythonBridgeRow struct {
	id                  int64
	observationID       pgtype.Int8
	replayObservationID pgtype.Int8
	workType            string
	parserVersion       string
	deduplicationKey    string
}

func readPythonBridge(ctx context.Context, connection *pgx.Conn, observationID int64, deduplicationKey string) (pythonBridgeRow, bool, bool, error) {
	rows, err := connection.Query(ctx, `
		SELECT id, observation_id, replay_observation_id, work_type,
		       parser_version, deduplication_key
		FROM python_processing_jobs
		WHERE observation_id = $1 OR deduplication_key = $2
		ORDER BY id
	`, observationID, deduplicationKey)
	if err != nil {
		return pythonBridgeRow{}, false, false, err
	}
	defer rows.Close()

	var row pythonBridgeRow
	count := 0
	conflict := false
	for rows.Next() {
		var candidate pythonBridgeRow
		if err := rows.Scan(
			&candidate.id,
			&candidate.observationID,
			&candidate.replayObservationID,
			&candidate.workType,
			&candidate.parserVersion,
			&candidate.deduplicationKey,
		); err != nil {
			return pythonBridgeRow{}, false, false, err
		}
		if count == 0 {
			row = candidate
		} else {
			conflict = true
		}
		count++
	}
	if err := rows.Err(); err != nil {
		return pythonBridgeRow{}, false, false, err
	}
	return row, count > 0, conflict, nil
}

func (s *store) proveObservationCommit(ctx context.Context, connection *pgx.Conn, intent observationCommitIntent) (commitProofOutcome, error) {
	if _, _, err := readCurrentMutationState(ctx, connection, &intent.job, intent.attemptID); err != nil {
		return commitProofUnknown, err
	}

	endpoint, endpointPresent, err := readEndpointResult(ctx, connection, intent.attemptID, intent.endpoint)
	if err != nil {
		return commitProofUnknown, err
	}
	observation, observationPresent, err := readObservation(ctx, connection, intent.occurrenceKey)
	if err != nil {
		return commitProofUnknown, err
	}
	var observationID int64
	if observationPresent {
		observationID = observation.id
	}
	bridge, bridgePresent, bridgeConflict, err := readPythonBridge(ctx, connection, observationID, "process-observation:"+fmt.Sprint(observationID))
	if err != nil {
		return commitProofUnknown, err
	}

	if !endpointPresent {
		return commitProofUnknown, errors.New("observation identity or endpoint state is contradictory")
	}
	if !observationPresent && !bridgePresent && endpoint.matchesRequestInFlight(intent) {
		return commitProofNotCommitted, nil
	}
	if !observationPresent || !bridgePresent || bridgeConflict {
		return commitProofUnknown, errors.New("observation transaction state is partial or contradictory")
	}
	if !observation.matches(intent) || !endpoint.matchesObservation(intent, observation.id) ||
		bridge.observationID.Int64 != observation.id || !bridge.observationID.Valid ||
		bridge.replayObservationID.Valid || bridge.workType != "process_observation" ||
		bridge.parserVersion != intent.parserVersion ||
		bridge.deduplicationKey != "process-observation:"+fmt.Sprint(observation.id) {
		return commitProofUnknown, errors.New("observation transaction state does not match intent")
	}
	return commitProofCommitted, nil
}

func (row endpointResultRow) matchesRequestInFlight(intent observationCommitIntent) bool {
	return row.attemptID == intent.attemptID &&
		row.endpoint == string(intent.endpoint) &&
		row.requestCount == intent.requestCount &&
		row.executionToken.Valid && row.executionToken.String == intent.leaseToken &&
		row.outcome != "observed" &&
		row.requestMethod.Valid && row.requestMethod.String == intent.response.request.method &&
		row.requestPath.Valid && row.requestPath.String == intent.response.request.path &&
		row.requestQuery.Valid && row.requestQuery.String == intent.response.request.query &&
		row.pagingEnvelopeState.Valid && row.pagingEnvelopeState.String == intent.response.pagingEnvelopeState &&
		row.sourceAdapterVersion.Valid && row.sourceAdapterVersion.String == intent.response.request.sourceAdapterVersion &&
		!row.responseCompletedAt.Valid && !row.httpStatus.Valid && !row.responseHash.Valid &&
		!row.archiveReference.Valid && !row.observationID.Valid && !row.keyLabel.Valid
}

func (row endpointResultRow) matchesObservation(intent observationCommitIntent, observationID int64) bool {
	return row.attemptID == intent.attemptID &&
		row.endpoint == string(intent.endpoint) &&
		row.executionToken.Valid && row.executionToken.String == intent.leaseToken &&
		row.outcome == intent.outcome &&
		sameTime(row.requestStartedAt, &intent.response.requestStartedAt) &&
		sameTime(row.responseCompletedAt, &intent.response.responseCompletedAt) &&
		sameInt(row.httpStatus, &intent.response.statusCode) &&
		sameText(row.responseHash, &intent.hash) &&
		sameText(row.archiveReference, &intent.archiveReference) &&
		row.observationID.Valid && row.observationID.Int64 == observationID &&
		sameTime(row.nextRetryAt, intent.nextRetryAt) &&
		!row.failureCategory.Valid &&
		sameText(row.keyLabel, &intent.keyLabel) &&
		sameText(row.requestMethod, &intent.response.request.method) &&
		sameText(row.requestPath, &intent.response.request.path) &&
		sameText(row.requestQuery, &intent.response.request.query) &&
		sameText(row.pagingEnvelopeState, &intent.response.pagingEnvelopeState) &&
		sameText(row.sourceAdapterVersion, &intent.response.request.sourceAdapterVersion)
}

func (row observationRow) matches(intent observationCommitIntent) bool {
	expectedScope := "player"
	if intent.endpoint == globalPlayerRankingsEndpoint {
		expectedScope = "global"
	}
	playerMatches := !row.playerID.Valid && !intent.playerID.Valid
	if intent.playerID.Valid {
		playerMatches = row.playerID.Valid && row.playerID.Int64 == intent.playerID.Int64
	}
	tagMatches := !row.normalizedTag.Valid && intent.normalizedTag == ""
	if intent.normalizedTag != "" {
		tagMatches = row.normalizedTag.Valid && row.normalizedTag.String == intent.normalizedTag
	}
	return row.occurrenceKey == intent.occurrenceKey &&
		row.collectionJobID == intent.jobID && row.attemptID == intent.attemptID &&
		row.scope == expectedScope && playerMatches && tagMatches &&
		row.endpoint == string(intent.endpoint) &&
		row.requestMethod == intent.response.request.method &&
		row.requestPath == intent.response.request.path &&
		row.requestQuery == intent.response.request.query &&
		sameTimeValue(row.requestStartedAt, intent.response.requestStartedAt) &&
		sameTimeValue(row.responseCompletedAt, intent.response.responseCompletedAt) &&
		row.httpStatus == intent.response.statusCode &&
		row.responseHash == intent.hash &&
		row.archiveReference == intent.archiveReference &&
		row.pagingEnvelopeState == intent.response.pagingEnvelopeState &&
		row.collectorVersion == intent.collectorVersion &&
		row.sourceAdapterVersion == intent.response.request.sourceAdapterVersion &&
		row.keyLabel == intent.keyLabel && jsonEqual(row.evidenceHeaders, intent.headers)
}

type endpointMutationIntent struct {
	job                  collectionJob
	jobID                int64
	attemptID            int64
	scope                string
	playerID             pgtype.Int8
	normalizedTag        string
	leaseOwner           string
	leaseToken           string
	leaseGeneration      int64
	endpoint             endpointName
	requestCount         int
	requestMethod        string
	requestPath          string
	requestQuery         string
	pagingEnvelopeState  string
	sourceAdapterVersion string
	requestStartedAt     time.Time
	responseCompletedAt  *time.Time
	httpStatus           *int
	outcome              string
	nextRetryAt          *time.Time
	responseHash         *string
	archiveReference     *string
	observationID        *int64
	failureCategory      *string
	keyLabel             *string
}

func (intent endpointMutationIntent) occurrenceKey() string {
	return fmt.Sprintf("%d:%s:%d", intent.attemptID, intent.endpoint, intent.requestCount)
}

func (intent endpointMutationIntent) matchesAttempt(row attemptFenceRow) bool {
	return row.jobID == intent.jobID &&
		row.leaseOwner.Valid && row.leaseOwner.String == intent.leaseOwner &&
		row.leaseToken.Valid && row.leaseToken.String == intent.leaseToken &&
		row.leaseGeneration == intent.leaseGeneration
}

func (row endpointResultRow) matchesRequestInFlightIntent(intent endpointMutationIntent) bool {
	return row.attemptID == intent.attemptID &&
		row.endpoint == string(intent.endpoint) &&
		row.requestCount == intent.requestCount &&
		row.executionToken.Valid && row.executionToken.String == intent.leaseToken &&
		row.outcome != "observed" &&
		row.requestMethod.Valid && row.requestMethod.String == intent.requestMethod &&
		row.requestPath.Valid && row.requestPath.String == intent.requestPath &&
		row.requestQuery.Valid && row.requestQuery.String == intent.requestQuery &&
		row.pagingEnvelopeState.Valid && row.pagingEnvelopeState.String == intent.pagingEnvelopeState &&
		row.sourceAdapterVersion.Valid && row.sourceAdapterVersion.String == intent.sourceAdapterVersion &&
		!row.responseCompletedAt.Valid && !row.httpStatus.Valid && !row.responseHash.Valid &&
		!row.archiveReference.Valid && !row.observationID.Valid && !row.keyLabel.Valid
}

func (row endpointResultRow) matchesMutation(intent endpointMutationIntent) bool {
	return row.attemptID == intent.attemptID &&
		row.endpoint == string(intent.endpoint) &&
		row.requestCount == intent.requestCount &&
		row.executionToken.Valid && row.executionToken.String == intent.leaseToken &&
		row.outcome == intent.outcome &&
		row.requestStartedAt.Valid && sameTimeValue(row.requestStartedAt.Time, intent.requestStartedAt) &&
		sameTime(row.responseCompletedAt, intent.responseCompletedAt) &&
		sameNullableInt(row.httpStatus, intent.httpStatus) &&
		sameNullableText(row.responseHash, intent.responseHash) &&
		sameNullableText(row.archiveReference, intent.archiveReference) &&
		sameNullableInt64(row.observationID, intent.observationID) &&
		sameTime(row.nextRetryAt, intent.nextRetryAt) &&
		sameNullableText(row.failureCategory, intent.failureCategory) &&
		sameNullableText(row.keyLabel, intent.keyLabel) &&
		sameText(row.requestMethod, &intent.requestMethod) &&
		sameText(row.requestPath, &intent.requestPath) &&
		sameText(row.requestQuery, &intent.requestQuery) &&
		sameText(row.pagingEnvelopeState, &intent.pagingEnvelopeState) &&
		sameText(row.sourceAdapterVersion, &intent.sourceAdapterVersion)
}

type transportFailureRow struct {
	id                   int64
	collectionJobID      int64
	attemptID            int64
	scope                string
	playerID             pgtype.Int8
	normalizedTag        pgtype.Text
	endpoint             string
	requestMethod        string
	requestPath          string
	requestQuery         string
	pagingEnvelopeState  string
	sourceAdapterVersion string
	requestStartedAt     time.Time
	failedAt             time.Time
	failureCategory      string
	retryState           string
	keyLabel             string
	evidenceKey          string
}

func readTransportFailure(ctx context.Context, connection *pgx.Conn, evidenceKey string) (transportFailureRow, bool, error) {
	var row transportFailureRow
	err := connection.QueryRow(ctx, `
		SELECT id, collection_job_id, attempt_id, scope, player_id,
		       normalized_tag, endpoint, request_method, request_path,
		       request_query, paging_envelope_state, source_adapter_version,
		       request_started_at, failed_at, failure_category, retry_state,
		       key_label, evidence_key
		FROM collector_transport_failures
		WHERE evidence_key = $1
	`, evidenceKey).Scan(
		&row.id,
		&row.collectionJobID,
		&row.attemptID,
		&row.scope,
		&row.playerID,
		&row.normalizedTag,
		&row.endpoint,
		&row.requestMethod,
		&row.requestPath,
		&row.requestQuery,
		&row.pagingEnvelopeState,
		&row.sourceAdapterVersion,
		&row.requestStartedAt,
		&row.failedAt,
		&row.failureCategory,
		&row.retryState,
		&row.keyLabel,
		&row.evidenceKey,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return transportFailureRow{}, false, nil
	}
	if err != nil {
		return transportFailureRow{}, false, err
	}
	return row, true, nil
}

func (row transportFailureRow) matches(intent endpointMutationIntent, evidenceKey string, failedAt time.Time, retryState string) bool {
	return row.collectionJobID == intent.jobID &&
		row.attemptID == intent.attemptID &&
		row.scope == intent.scope &&
		row.playerID.Valid == intent.playerID.Valid &&
		(!intent.playerID.Valid || row.playerID.Int64 == intent.playerID.Int64) &&
		row.normalizedTag.Valid == (intent.normalizedTag != "") &&
		(intent.normalizedTag == "" || row.normalizedTag.String == intent.normalizedTag) &&
		row.endpoint == string(intent.endpoint) &&
		row.requestMethod == intent.requestMethod &&
		row.requestPath == intent.requestPath &&
		row.requestQuery == intent.requestQuery &&
		row.pagingEnvelopeState == intent.pagingEnvelopeState &&
		row.sourceAdapterVersion == intent.sourceAdapterVersion &&
		sameTimeValue(row.requestStartedAt, intent.requestStartedAt) &&
		sameTimeValue(row.failedAt, failedAt) &&
		row.failureCategory == dereferenceText(intent.failureCategory) &&
		row.retryState == retryState &&
		row.keyLabel == dereferenceText(intent.keyLabel) &&
		row.evidenceKey == evidenceKey
}

func dereferenceText(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func sameInt64(actual pgtype.Int8, expected *int64) bool {
	return expected != nil && actual.Valid && actual.Int64 == *expected
}

func (s *store) readEndpointRequestCount(ctx context.Context, job *collectionJob, attemptID int64, endpoint endpointName) (int, error) {
	var requestCount int
	err := s.pool.QueryRow(ctx, `
		SELECT endpoint_result.request_count
		FROM collector_endpoint_results AS endpoint_result
		JOIN collector_jobs AS current_job ON current_job.id = $4
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND endpoint_result.execution_token = $3
			AND current_job.lease_owner = $5
			AND current_job.lease_token = $3
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
	`, attemptID, string(endpoint), job.leaseToken, job.id, job.leaseOwner, job.leaseGeneration).Scan(&requestCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, errLeaseLost
	}
	if err != nil {
		return 0, fmt.Errorf("read version-two endpoint request count: %w", err)
	}
	return requestCount, nil
}

func (s *store) proveTransportFailureCommit(ctx context.Context, connection *pgx.Conn, intent endpointMutationIntent, evidenceKey string, failedAt time.Time, retryState string) (commitProofOutcome, error) {
	attempt, attemptPresent, err := readAttemptFence(ctx, connection, intent.attemptID)
	if err != nil {
		return commitProofUnknown, err
	}
	if !attemptPresent || !intent.matchesAttempt(attempt) {
		return commitProofUnknown, errors.New("transport failure attempt identity is contradictory")
	}
	endpoint, endpointPresent, err := readEndpointResult(ctx, connection, intent.attemptID, intent.endpoint)
	if err != nil {
		return commitProofUnknown, err
	}
	failure, failurePresent, err := readTransportFailure(ctx, connection, evidenceKey)
	if err != nil {
		return commitProofUnknown, err
	}
	if !failurePresent && endpointPresent && endpoint.matchesRequestInFlightIntent(intent) {
		return commitProofNotCommitted, nil
	}
	if !failurePresent || !endpointPresent {
		return commitProofUnknown, errors.New("transport failure transaction state is partial")
	}
	if !failure.matches(intent, evidenceKey, failedAt, retryState) || !endpoint.matchesMutation(intent) {
		return commitProofUnknown, errors.New("transport failure transaction state is contradictory")
	}
	return commitProofCommitted, nil
}

func (s *store) proveStorageFailureCommit(ctx context.Context, connection *pgx.Conn, intent endpointMutationIntent) (commitProofOutcome, error) {
	attempt, attemptPresent, err := readAttemptFence(ctx, connection, intent.attemptID)
	if err != nil {
		return commitProofUnknown, err
	}
	if !attemptPresent || !intent.matchesAttempt(attempt) {
		return commitProofUnknown, errors.New("storage failure attempt identity is contradictory")
	}
	endpoint, endpointPresent, err := readEndpointResult(ctx, connection, intent.attemptID, intent.endpoint)
	if err != nil {
		return commitProofUnknown, err
	}
	_, observationPresent, err := readObservation(ctx, connection, intent.occurrenceKey())
	if err != nil {
		return commitProofUnknown, err
	}
	if !observationPresent && endpointPresent && endpoint.matchesRequestInFlightIntent(intent) {
		return commitProofNotCommitted, nil
	}
	if !endpointPresent || observationPresent {
		return commitProofUnknown, errors.New("storage failure transaction state is partial")
	}
	if !endpoint.matchesMutation(intent) {
		return commitProofUnknown, errors.New("storage failure transaction state is contradictory")
	}
	return commitProofCommitted, nil
}

func sameTime(actual pgtype.Timestamptz, expected *time.Time) bool {
	if expected == nil {
		return !actual.Valid
	}
	return actual.Valid && actual.Time.Equal(postgresTime(*expected))
}

func sameTimeValue(actual time.Time, expected time.Time) bool {
	return actual.Equal(postgresTime(expected))
}

func sameInt(actual pgtype.Int4, expected *int) bool {
	return expected != nil && actual.Valid && int(actual.Int32) == *expected
}

func sameNullableInt(actual pgtype.Int4, expected *int) bool {
	if expected == nil {
		return !actual.Valid
	}
	return actual.Valid && int(actual.Int32) == *expected
}

func sameText(actual pgtype.Text, expected *string) bool {
	return expected != nil && actual.Valid && actual.String == *expected
}

func sameNullableText(actual pgtype.Text, expected *string) bool {
	if expected == nil {
		return !actual.Valid
	}
	return actual.Valid && actual.String == *expected
}

func sameNullableInt64(actual pgtype.Int8, expected *int64) bool {
	if expected == nil {
		return !actual.Valid
	}
	return actual.Valid && actual.Int64 == *expected
}

func jsonEqual(left, right []byte) bool {
	var leftValue, rightValue any
	if json.Unmarshal(left, &leftValue) != nil || json.Unmarshal(right, &rightValue) != nil {
		return string(left) == string(right)
	}
	return reflect.DeepEqual(leftValue, rightValue)
}

func postgresTime(value time.Time) time.Time {
	return value.UTC().Truncate(time.Microsecond)
}

type pgxQueryer interface {
	Query(context.Context, string, ...any) (pgx.Rows, error)
	QueryRow(context.Context, string, ...any) pgx.Row
}

type collectorJobState struct {
	id                   int64
	workType             string
	scope                string
	playerID             pgtype.Int8
	normalizedTag        pgtype.Text
	capacityPool         string
	status               string
	parentAttemptID      pgtype.Int8
	resultAttemptID      pgtype.Int8
	requiredEndpoint     pgtype.Text
	sweepID              pgtype.Int8
	resetBaselineSweepID pgtype.Int8
	leaseOwner           pgtype.Text
	leaseToken           pgtype.Text
	leaseExpiresAt       pgtype.Timestamptz
	leaseGeneration      int64
	coalescingKey        string
	priority             int
	dueAt                pgtype.Timestamptz
	cancelReason         pgtype.Text
}

func readCollectorJobState(ctx context.Context, queryer pgxQueryer, jobID int64) (collectorJobState, bool, error) {
	var row collectorJobState
	err := queryer.QueryRow(ctx, `
		SELECT id, work_type, scope, player_id, normalized_tag, capacity_pool,
		       status, parent_attempt_id, result_attempt_id, required_endpoint,
		       sweep_id, reset_baseline_sweep_id, lease_owner, lease_token,
		       lease_expires_at, lease_generation, coalescing_key, priority,
		       due_at, cancel_reason
		FROM collector_jobs
		WHERE id = $1
	`, jobID).Scan(
		&row.id,
		&row.workType,
		&row.scope,
		&row.playerID,
		&row.normalizedTag,
		&row.capacityPool,
		&row.status,
		&row.parentAttemptID,
		&row.resultAttemptID,
		&row.requiredEndpoint,
		&row.sweepID,
		&row.resetBaselineSweepID,
		&row.leaseOwner,
		&row.leaseToken,
		&row.leaseExpiresAt,
		&row.leaseGeneration,
		&row.coalescingKey,
		&row.priority,
		&row.dueAt,
		&row.cancelReason,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return collectorJobState{}, false, nil
	}
	if err != nil {
		return collectorJobState{}, false, err
	}
	return row, true, nil
}

type collectorAttemptState struct {
	id              int64
	jobID           int64
	status          string
	leaseOwner      pgtype.Text
	leaseToken      pgtype.Text
	leaseGeneration int64
	completedAt     pgtype.Timestamptz
}

func readCollectorAttemptState(ctx context.Context, queryer pgxQueryer, attemptID int64) (collectorAttemptState, bool, error) {
	var row collectorAttemptState
	err := queryer.QueryRow(ctx, `
		SELECT id, job_id, status, lease_owner, lease_token,
		       lease_generation, completed_at
		FROM collector_attempts
		WHERE id = $1
	`, attemptID).Scan(
		&row.id,
		&row.jobID,
		&row.status,
		&row.leaseOwner,
		&row.leaseToken,
		&row.leaseGeneration,
		&row.completedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return collectorAttemptState{}, false, nil
	}
	if err != nil {
		return collectorAttemptState{}, false, err
	}
	return row, true, nil
}

type endpointResolutionState struct {
	endpoint    string
	outcome     string
	retryCount  int
	nextRetryAt pgtype.Timestamptz
}

func readEndpointResolutionStates(ctx context.Context, queryer pgxQueryer, attemptID int64) ([]endpointResolutionState, error) {
	rows, err := queryer.Query(ctx, `
		SELECT endpoint, outcome, retry_count, next_retry_at
		FROM collector_endpoint_results
		WHERE attempt_id = $1
		ORDER BY endpoint
	`, attemptID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	states := make([]endpointResolutionState, 0, 2)
	for rows.Next() {
		var state endpointResolutionState
		if err := rows.Scan(&state.endpoint, &state.outcome, &state.retryCount, &state.nextRetryAt); err != nil {
			return nil, err
		}
		states = append(states, state)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return states, nil
}

type retryJobState struct {
	collectorJobState
}

func readRetryJobStates(ctx context.Context, queryer pgxQueryer, attemptID int64) ([]retryJobState, error) {
	rows, err := queryer.Query(ctx, `
		SELECT id, work_type, scope, player_id, normalized_tag, capacity_pool,
		       status, parent_attempt_id, result_attempt_id, required_endpoint,
		       sweep_id, reset_baseline_sweep_id, lease_owner, lease_token,
		       lease_expires_at, lease_generation, coalescing_key, priority,
		       due_at, cancel_reason
		FROM collector_jobs
		WHERE parent_attempt_id = $1
	ORDER BY id
	`, attemptID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	jobs := make([]retryJobState, 0)
	for rows.Next() {
		var state retryJobState
		if err := rows.Scan(
			&state.id,
			&state.workType,
			&state.scope,
			&state.playerID,
			&state.normalizedTag,
			&state.capacityPool,
			&state.status,
			&state.parentAttemptID,
			&state.resultAttemptID,
			&state.requiredEndpoint,
			&state.sweepID,
			&state.resetBaselineSweepID,
			&state.leaseOwner,
			&state.leaseToken,
			&state.leaseExpiresAt,
			&state.leaseGeneration,
			&state.coalescingKey,
			&state.priority,
			&state.dueAt,
			&state.cancelReason,
		); err != nil {
			return nil, err
		}
		jobs = append(jobs, state)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return jobs, nil
}

type attemptEventState struct {
	id              int64
	jobID           int64
	attemptID       int64
	eventType       string
	fromStatus      pgtype.Text
	toStatus        string
	leaseOwner      pgtype.Text
	leaseToken      pgtype.Text
	leaseGeneration int64
	failureCategory pgtype.Text
}

func readAttemptEventStates(ctx context.Context, queryer pgxQueryer, attemptID int64) ([]attemptEventState, error) {
	rows, err := queryer.Query(ctx, `
		SELECT id, job_id, attempt_id, event_type, from_status, to_status,
		       lease_owner, lease_token, lease_generation, failure_category
		FROM collector_attempt_events
		WHERE attempt_id = $1
		ORDER BY id
	`, attemptID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	events := make([]attemptEventState, 0)
	for rows.Next() {
		var event attemptEventState
		if err := rows.Scan(
			&event.id,
			&event.jobID,
			&event.attemptID,
			&event.eventType,
			&event.fromStatus,
			&event.toStatus,
			&event.leaseOwner,
			&event.leaseToken,
			&event.leaseGeneration,
			&event.failureCategory,
		); err != nil {
			return nil, err
		}
		events = append(events, event)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return events, nil
}

type resetBaselineState struct {
	id           int64
	resetSweep   int64
	playerID     int64
	evidenceKind string
	state        string
	completedAt  pgtype.Timestamptz
}

func readResetBaselineState(ctx context.Context, queryer pgxQueryer, baselineID int64) (resetBaselineState, bool, error) {
	var row resetBaselineState
	err := queryer.QueryRow(ctx, `
		SELECT id, reset_sweep_id, player_id, evidence_kind, state, completed_at
		FROM collector_reset_baseline_sweeps
		WHERE id = $1
	`, baselineID).Scan(
		&row.id,
		&row.resetSweep,
		&row.playerID,
		&row.evidenceKind,
		&row.state,
		&row.completedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return resetBaselineState{}, false, nil
	}
	if err != nil {
		return resetBaselineState{}, false, err
	}
	return row, true, nil
}

type attemptResolutionSnapshot struct {
	current         collectorJobState
	root            collectorJobState
	attempt         collectorAttemptState
	endpoints       []endpointResolutionState
	retryJobs       []retryJobState
	events          []attemptEventState
	baselinePresent bool
	baseline        resetBaselineState
}

func readAttemptResolutionSnapshot(
	ctx context.Context,
	queryer pgxQueryer,
	currentJobID int64,
	attemptID int64,
	rootJobID int64,
) (attemptResolutionSnapshot, bool, error) {
	current, currentPresent, err := readCollectorJobState(ctx, queryer, currentJobID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}
	root, rootPresent, err := readCollectorJobState(ctx, queryer, rootJobID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}
	attempt, attemptPresent, err := readCollectorAttemptState(ctx, queryer, attemptID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}
	if !currentPresent || !rootPresent || !attemptPresent {
		return attemptResolutionSnapshot{}, false, nil
	}
	endpoints, err := readEndpointResolutionStates(ctx, queryer, attemptID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}
	retryJobs, err := readRetryJobStates(ctx, queryer, attemptID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}
	events, err := readAttemptEventStates(ctx, queryer, attemptID)
	if err != nil {
		return attemptResolutionSnapshot{}, false, err
	}

	snapshot := attemptResolutionSnapshot{
		current:   current,
		root:      root,
		attempt:   attempt,
		endpoints: endpoints,
		retryJobs: retryJobs,
		events:    events,
	}
	if root.resetBaselineSweepID.Valid {
		baseline, present, err := readResetBaselineState(ctx, queryer, root.resetBaselineSweepID.Int64)
		if err != nil {
			return attemptResolutionSnapshot{}, false, err
		}
		snapshot.baselinePresent = present
		snapshot.baseline = baseline
	}
	return snapshot, true, nil
}

func readCurrentMutationState(
	ctx context.Context,
	connection *pgx.Conn,
	job *collectionJob,
	attemptID int64,
) (collectorJobState, collectorAttemptState, error) {
	current, present, err := readCollectorJobState(ctx, connection, job.id)
	if err != nil {
		return collectorJobState{}, collectorAttemptState{}, err
	}
	if !present {
		return collectorJobState{}, collectorAttemptState{}, errors.New("current collection job identity is missing")
	}
	attempt, present, err := readCollectorAttemptState(ctx, connection, attemptID)
	if err != nil {
		return collectorJobState{}, collectorAttemptState{}, err
	}
	if !present {
		return collectorJobState{}, collectorAttemptState{}, errors.New("collection attempt identity is missing")
	}
	root, present, err := readCollectorJobState(ctx, connection, attempt.jobID)
	if err != nil {
		return collectorJobState{}, collectorAttemptState{}, err
	}
	if !present {
		return collectorJobState{}, collectorAttemptState{}, errors.New("collection root job identity is missing")
	}
	if !jobStateMatchesCollectionJob(current, *job) || current.status != "leased" ||
		!current.leaseOwner.Valid || current.leaseOwner.String != job.leaseOwner ||
		!current.leaseToken.Valid || current.leaseToken.String != job.leaseToken ||
		current.leaseGeneration != job.leaseGeneration || !current.leaseExpiresAt.Valid ||
		!current.leaseExpiresAt.Time.After(time.Now().UTC()) ||
		!attemptRelatesToCurrentJob(current, root, attempt, attemptID) {
		return collectorJobState{}, collectorAttemptState{}, errors.New("current collection lease or attempt identity is contradictory")
	}
	if current.id == root.id && (!attempt.leaseOwner.Valid || attempt.leaseOwner.String != job.leaseOwner ||
		!attempt.leaseToken.Valid || attempt.leaseToken.String != job.leaseToken ||
		attempt.leaseGeneration != job.leaseGeneration) {
		return collectorJobState{}, collectorAttemptState{}, errors.New("collection attempt lease fence is contradictory")
	}
	return current, attempt, nil
}

type attemptCommitIntent struct {
	job               collectionJob
	attemptID         int64
	rootJobID         int64
	now               time.Time
	maximumRetries    int
	completionOnly    bool
	preCommitSnapshot *attemptResolutionSnapshot
}

func (s *store) proveAttemptResolutionCommit(
	ctx context.Context,
	connection *pgx.Conn,
	intent attemptCommitIntent,
) (commitProofOutcome, error) {
	return s.proveAttemptCommit(ctx, connection, intent, false)
}

func (s *store) proveTerminalCompletionCommit(
	ctx context.Context,
	connection *pgx.Conn,
	intent attemptCommitIntent,
) (commitProofOutcome, error) {
	return s.proveAttemptCommit(ctx, connection, intent, true)
}

func (s *store) proveAttemptCommit(
	ctx context.Context,
	connection *pgx.Conn,
	intent attemptCommitIntent,
	completionOnly bool,
) (commitProofOutcome, error) {
	attempt, attemptPresent, err := readCollectorAttemptState(ctx, connection, intent.attemptID)
	if err != nil {
		return commitProofUnknown, err
	}
	if !attemptPresent {
		return commitProofUnknown, errors.New("collection attempt identity is missing")
	}
	rootJobID := attempt.jobID
	if intent.rootJobID != 0 && rootJobID != intent.rootJobID {
		return commitProofUnknown, errors.New("collection attempt root job identity changed")
	}
	actual, present, err := readAttemptResolutionSnapshot(
		ctx, connection, intent.job.id, intent.attemptID, rootJobID,
	)
	if err != nil {
		return commitProofUnknown, err
	}
	if !present {
		return commitProofUnknown, errors.New("collection commit state is missing")
	}

	committed, reason := validateAttemptCommitTarget(ctx, connection, intent, actual, completionOnly)
	if committed {
		return commitProofCommitted, nil
	}
	if intent.preCommitSnapshot != nil && reflect.DeepEqual(*intent.preCommitSnapshot, actual) {
		return commitProofNotCommitted, nil
	}
	if reason == "" {
		reason = "collection commit state is partial or contradictory"
	}
	return commitProofUnknown, errors.New(reason)
}

func validateAttemptCommitTarget(
	ctx context.Context,
	connection *pgx.Conn,
	intent attemptCommitIntent,
	snapshot attemptResolutionSnapshot,
	completionOnly bool,
) (bool, string) {
	if !jobStateMatchesCollectionJob(snapshot.current, intent.job) {
		return false, "current collection job identity does not match commit intent"
	}
	if snapshot.attempt.id != intent.attemptID || snapshot.attempt.jobID != snapshot.root.id {
		return false, "collection attempt identity does not match commit intent"
	}
	if !attemptRelatesToCurrentJob(snapshot.current, snapshot.root, snapshot.attempt, intent.attemptID) {
		return false, "collection attempt and job relationship is contradictory"
	}
	if len(snapshot.endpoints) == 0 {
		return false, "collection endpoint state is missing"
	}
	if snapshot.root.resetBaselineSweepID.Valid && !snapshot.baselinePresent {
		return false, "reset baseline state is missing"
	}

	kind := ""
	switch {
	case snapshot.attempt.status == "complete" &&
		snapshot.root.status == "complete" &&
		snapshot.current.status == "complete":
		kind = "complete"
	case !completionOnly && snapshot.attempt.status == "incomplete" &&
		snapshot.root.status == "waiting_retry" &&
		(snapshot.current.status == "complete" ||
			(snapshot.current.id == snapshot.root.id && snapshot.current.status == "waiting_retry")):
		kind = "retry"
	case !completionOnly && snapshot.attempt.status == "failed" &&
		snapshot.root.status == "failed" &&
		snapshot.current.status == "failed":
		kind = "failed"
	default:
		return false, "collection job or attempt status is not a committed target state"
	}

	if !allEndpointStatesMatch(kind, snapshot.endpoints, intent.maximumRetries) {
		return false, "collection endpoint terminal states are incomplete or contradictory"
	}
	if !jobLeasesMatchTarget(snapshot.current, kind) ||
		(kind != "retry" && !jobLeasesMatchTarget(snapshot.root, kind)) {
		return false, "collection job lease state does not match commit target"
	}
	if kind == "complete" {
		if !snapshot.attempt.completedAt.Valid {
			return false, "completed attempt timestamp is missing"
		}
		if intent.preCommitSnapshot != nil &&
			!sameTimeValue(snapshot.attempt.completedAt.Time, intent.now) {
			return false, "completed attempt timestamp does not match commit intent"
		}
	}
	if kind == "failed" && !allSiblingRetryJobsTerminal(snapshot.retryJobs, snapshot.current.id) {
		return false, "sibling retry jobs are not in terminal states"
	}
	if kind == "retry" && !retryJobsMatchTarget(snapshot.retryJobs, snapshot.endpoints, intent) {
		return false, "retry job identity is incomplete or contradictory"
	}
	if !baselineMatchesTarget(snapshot, kind) {
		return false, "reset baseline state does not match commit target"
	}
	if !attemptEventMatchesTarget(snapshot.events, snapshot.current, intent, kind) {
		return false, "attempt event history is incomplete or contradictory"
	}
	return true, ""
}

func jobStateMatchesCollectionJob(row collectorJobState, job collectionJob) bool {
	return row.id == job.id &&
		row.workType == job.workType &&
		row.scope == job.scope &&
		sameNullableInt64(row.playerID, nullableInt64(job.playerID)) &&
		sameNullableText(row.normalizedTag, nullableString(job.normalizedTag)) &&
		row.capacityPool == string(job.pool) &&
		row.parentAttemptID.Valid == job.parentAttemptID.Valid &&
		(!job.parentAttemptID.Valid || row.parentAttemptID.Int64 == job.parentAttemptID.Int64) &&
		row.requiredEndpoint.Valid == job.requiredEndpoint.Valid &&
		(!job.requiredEndpoint.Valid || row.requiredEndpoint.String == job.requiredEndpoint.String) &&
		row.sweepID.Valid == job.sweepID.Valid &&
		(!job.sweepID.Valid || row.sweepID.Int64 == job.sweepID.Int64) &&
		row.resetBaselineSweepID.Valid == job.resetBaselineSweepID.Valid &&
		(!job.resetBaselineSweepID.Valid || row.resetBaselineSweepID.Int64 == job.resetBaselineSweepID.Int64) &&
		row.leaseGeneration == job.leaseGeneration
}

func nullableInt64(value pgtype.Int8) *int64 {
	if !value.Valid {
		return nil
	}
	result := value.Int64
	return &result
}

func nullableString(value string) *string {
	if value == "" {
		return nil
	}
	result := value
	return &result
}

func attemptRelatesToCurrentJob(
	current collectorJobState,
	root collectorJobState,
	attempt collectorAttemptState,
	attemptID int64,
) bool {
	if root.id != attempt.jobID || current.resultAttemptID.Valid && current.resultAttemptID.Int64 != attemptID &&
		current.parentAttemptID.Valid && current.parentAttemptID.Int64 != attemptID {
		return false
	}
	if current.id == root.id {
		return current.resultAttemptID.Valid && current.resultAttemptID.Int64 == attemptID &&
			attempt.leaseOwner.Valid && attempt.leaseToken.Valid
	}
	return current.workType == "endpoint_retry" &&
		current.parentAttemptID.Valid && current.parentAttemptID.Int64 == attemptID
}

func jobLeasesMatchTarget(row collectorJobState, kind string) bool {
	if kind == "retry" && row.status == "waiting_retry" {
		return true
	}
	return !row.leaseOwner.Valid && !row.leaseToken.Valid && !row.leaseExpiresAt.Valid
}

func allEndpointStatesMatch(kind string, endpoints []endpointResolutionState, maximumRetries int) bool {
	if len(endpoints) == 0 {
		return false
	}
	hasRetrying := false
	hasFailed := false
	for _, endpoint := range endpoints {
		switch kind {
		case "complete":
			if endpoint.outcome != "observed" {
				return false
			}
		case "retry":
			switch endpoint.outcome {
			case "observed":
			case "retrying":
				if endpoint.retryCount < 1 || endpoint.retryCount > maximumRetries {
					return false
				}
				hasRetrying = true
			case "failed":
				if endpoint.retryCount < maximumRetries {
					return false
				}
				hasFailed = true
			default:
				return false
			}
		case "failed":
			switch endpoint.outcome {
			case "observed":
			case "failed":
				if endpoint.retryCount < maximumRetries {
					return false
				}
				hasFailed = true
			default:
				return false
			}
		default:
			return false
		}
	}
	if kind == "retry" {
		return hasRetrying
	}
	if kind == "failed" {
		return hasFailed
	}
	return true
}

func retryJobsMatchTarget(
	jobs []retryJobState,
	endpoints []endpointResolutionState,
	intent attemptCommitIntent,
) bool {
	for _, endpoint := range endpoints {
		if endpoint.outcome != "retrying" {
			continue
		}
		key := fmt.Sprintf("retry:%d:%s:%d", intent.attemptID, endpoint.endpoint, endpoint.retryCount)
		matches := 0
		for _, retryJob := range jobs {
			if retryJob.coalescingKey != key {
				continue
			}
			matches++
			if !retryJobIdentityMatches(retryJob.collectorJobState, intent.job, intent.attemptID, endpoint.endpoint, key) {
				return false
			}
			if retryJob.status == "cancelled" || retryJob.status == "failed" {
				return false
			}
		}
		if matches != 1 {
			return false
		}
	}
	return true
}

func retryJobIdentityMatches(
	row collectorJobState,
	job collectionJob,
	attemptID int64,
	endpoint string,
	coalescingKey string,
) bool {
	return row.workType == "endpoint_retry" &&
		row.scope == job.scope &&
		sameNullableInt64(row.playerID, nullableInt64(job.playerID)) &&
		sameNullableText(row.normalizedTag, nullableString(job.normalizedTag)) &&
		row.capacityPool == string(job.pool) &&
		row.priority == 300 &&
		row.parentAttemptID.Valid && row.parentAttemptID.Int64 == attemptID &&
		row.requiredEndpoint.Valid && row.requiredEndpoint.String == endpoint &&
		row.sweepID.Valid == job.sweepID.Valid &&
		(!job.sweepID.Valid || row.sweepID.Int64 == job.sweepID.Int64) &&
		row.resetBaselineSweepID.Valid == job.resetBaselineSweepID.Valid &&
		(!job.resetBaselineSweepID.Valid || row.resetBaselineSweepID.Int64 == job.resetBaselineSweepID.Int64) &&
		row.leaseGeneration == job.leaseGeneration &&
		row.coalescingKey == coalescingKey &&
		!row.resultAttemptID.Valid && !row.leaseOwner.Valid &&
		!row.leaseToken.Valid && !row.leaseExpiresAt.Valid
}

func allSiblingRetryJobsTerminal(jobs []retryJobState, currentJobID int64) bool {
	for _, job := range jobs {
		if job.id == currentJobID {
			if job.status != "failed" {
				return false
			}
			continue
		}
		if job.status != "cancelled" && job.status != "complete" && job.status != "failed" {
			return false
		}
	}
	return true
}

func baselineMatchesTarget(snapshot attemptResolutionSnapshot, kind string) bool {
	if !snapshot.root.resetBaselineSweepID.Valid {
		return true
	}
	if !snapshot.baselinePresent || snapshot.baseline.id != snapshot.root.resetBaselineSweepID.Int64 {
		return false
	}
	switch kind {
	case "complete":
		return snapshot.baseline.state == "complete" && snapshot.baseline.completedAt.Valid
	case "retry":
		return snapshot.baseline.state == "incomplete" && !snapshot.baseline.completedAt.Valid
	case "failed":
		return snapshot.baseline.state == "failed" && snapshot.baseline.completedAt.Valid
	default:
		return false
	}
}

func attemptEventMatchesTarget(
	events []attemptEventState,
	current collectorJobState,
	intent attemptCommitIntent,
	kind string,
) bool {
	eventType := "completed"
	toStatus := "complete"
	if kind == "retry" {
		eventType = "retry_scheduled"
		toStatus = "incomplete"
	} else if kind == "failed" {
		eventType = "failed"
		toStatus = "failed"
	}

	matches := 0
	for _, event := range events {
		if event.leaseGeneration != intent.job.leaseGeneration {
			continue
		}
		if event.eventType == "completed" || event.eventType == "retry_scheduled" || event.eventType == "failed" {
			if event.eventType != eventType {
				return false
			}
		}
		if event.eventType != eventType {
			continue
		}
		if event.jobID != current.id || event.attemptID != intent.attemptID ||
			event.toStatus != toStatus ||
			!event.leaseOwner.Valid || event.leaseOwner.String != intent.job.leaseOwner ||
			!event.leaseToken.Valid || event.leaseToken.String != intent.job.leaseToken ||
			event.failureCategory.Valid ||
			(!event.fromStatus.Valid || (event.fromStatus.String != "running" && event.fromStatus.String != "incomplete")) {
			return false
		}
		matches++
	}
	return matches == 1
}
