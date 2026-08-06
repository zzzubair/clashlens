package collector

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func TestVersionTwoObservationReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, requestCount, response := prepareAmbiguousObservationFixture(t, ctx, store)
	commitErr := errors.New("injected ambiguous observation commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}

	hash := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	if err := store.commitObservation(ctx, job, attemptID, profileEndpoint, requestCount, response,
		hash, "archive/profile.json", "collector-v2", "normal", "observed", nil); err != nil {
		t.Fatalf("reconciled observation commit returned an error: %v", err)
	}

	if err := store.commitObservation(ctx, job, attemptID, profileEndpoint, requestCount, response,
		hash, "archive/profile.json", "collector-v2", "normal", "observed", nil); err != nil {
		t.Fatalf("repeated reconciled observation commit returned an error: %v", err)
	}

	var observations, processingJobs, observedEndpoints int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count Python processing jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = 'profile' AND outcome = 'observed'
	`, attemptID).Scan(&observedEndpoints); err != nil {
		t.Fatalf("count observed endpoint results: %v", err)
	}
	if observations != 1 || processingJobs != 1 || observedEndpoints != 1 {
		t.Fatalf("durable observation effects = %d observations, %d Python jobs, %d observed endpoints; want 1, 1, 1",
			observations, processingJobs, observedEndpoints)
	}
}

func prepareAmbiguousObservationFixture(t *testing.T, ctx context.Context, store *store) (*collectionJob, int64, int, officialResponse) {
	t.Helper()
	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert observation fixture player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule observation fixture job: %v", err)
	}
	job, err := store.claimNext(ctx, "ambiguous-observation-worker", normalPool, now, time.Minute, "ambiguous-observation-token")
	if err != nil {
		t.Fatalf("claim observation fixture job: %v", err)
	}
	if job == nil {
		t.Fatal("claim observation fixture returned no job")
	}
	attemptID, endpoints, err := store.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepare observation fixture attempt: %v", err)
	}
	if len(endpoints) != 2 {
		t.Fatalf("observation fixture endpoints = %v, want two endpoints", endpoints)
	}
	requestCount, err := store.beginEndpointRequest(ctx, job, attemptID, profileEndpoint, now)
	if err != nil {
		t.Fatalf("begin observation fixture request: %v", err)
	}
	response := officialResponse{
		requestStartedAt:    now,
		responseCompletedAt: now.Add(time.Second),
		statusCode:          200,
		headers:             map[string]string{"Content-Type": "application/json"},
		request: requestProvenance{
			method:               "GET",
			path:                 "/v1/players/%232PP",
			query:                "",
			sourceAdapterVersion: "player-profile-v1",
		},
		pagingEnvelopeState: "not_applicable",
	}
	return job, attemptID, requestCount, response
}

func TestVersionTwoObservationRollbackCommitErrorHasNoEffects(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, requestCount, response := prepareAmbiguousObservationFixture(t, ctx, store)
	commitErr := errors.New("injected rolled-back observation commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		_ = tx.Rollback(ctx)
		return commitErr
	}

	err := store.commitObservation(ctx, job, attemptID, profileEndpoint, requestCount, response,
		"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"archive/profile-rollback.json", "collector-v2", "normal", "observed", nil)
	if !errors.Is(err, commitErr) {
		t.Fatalf("rolled-back observation error = %v, want injected commit error", err)
	}
	if errors.Is(err, errCommitOutcomeUnknown) {
		t.Fatalf("rolled-back observation error = %v, want known no-commit outcome", err)
	}

	var observations, processingJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count rolled-back observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count rolled-back Python jobs: %v", err)
	}
	if observations != 0 || processingJobs != 0 {
		t.Fatalf("rolled-back observation effects = %d observations and %d Python jobs; want 0 and 0", observations, processingJobs)
	}
}

func TestVersionTwoCompletedResolutionReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, _, response := prepareAmbiguousObservationFixture(t, ctx, store)
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = 'observed', response_completed_at = $2
		WHERE attempt_id = $1
	`, attemptID, response.responseCompletedAt); err != nil {
		t.Fatalf("mark completion endpoints observed: %v", err)
	}

	commitErr := errors.New("injected ambiguous completed resolution commit")
	commitCalls := 0
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		commitCalls++
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}
	now := time.Now().UTC()
	if err := store.resolveAttempt(ctx, job, attemptID, now, 3); err != nil {
		t.Fatalf("reconciled completed resolution returned an error: %v", err)
	}
	if err := store.resolveAttempt(ctx, job, attemptID, now, 3); err != nil {
		t.Fatalf("repeated reconciled completed resolution returned an error: %v", err)
	}
	if commitCalls != 1 {
		t.Fatalf("commit hook calls = %d, want 1", commitCalls)
	}

	var jobStatus, attemptStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, job.id).Scan(&jobStatus); err != nil {
		t.Fatalf("read completed resolution job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts WHERE id = $1`, attemptID).Scan(&attemptStatus); err != nil {
		t.Fatalf("read completed resolution attempt status: %v", err)
	}
	if jobStatus != "complete" || attemptStatus != "complete" {
		t.Fatalf("completed resolution state = job %q, attempt %q; want complete, complete", jobStatus, attemptStatus)
	}
}

func TestVersionTwoTransportFailureReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, _, response := prepareAmbiguousObservationFixture(t, ctx, store)
	failedAt := response.responseCompletedAt
	nextRetryAt := failedAt.Add(time.Minute)
	commitErr := errors.New("injected ambiguous transport commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}
	if err := store.recordTransportFailure(ctx, job, attemptID, profileEndpoint,
		response.requestStartedAt, failedAt, nextRetryAt, "timeout", "normal"); err != nil {
		t.Fatalf("reconciled transport failure returned an error: %v", err)
	}
	if err := store.recordTransportFailure(ctx, job, attemptID, profileEndpoint,
		response.requestStartedAt, failedAt, nextRetryAt, "timeout", "normal"); err != nil {
		t.Fatalf("repeated reconciled transport failure returned an error: %v", err)
	}

	var failures, transportEndpoints int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_transport_failures`).Scan(&failures); err != nil {
		t.Fatalf("count transport failures: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = 'profile' AND outcome = 'transport_failed'
	`, attemptID).Scan(&transportEndpoints); err != nil {
		t.Fatalf("count transport endpoint results: %v", err)
	}
	if failures != 1 || transportEndpoints != 1 {
		t.Fatalf("durable transport effects = %d failures and %d endpoint results; want 1 and 1", failures, transportEndpoints)
	}
}

func TestVersionTwoStorageFailureReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, _, response := prepareAmbiguousObservationFixture(t, ctx, store)
	commitErr := errors.New("injected ambiguous storage failure commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}
	if err := store.recordStorageFailure(ctx, job, attemptID, profileEndpoint, response,
		"archive_write_failed", "normal"); err != nil {
		t.Fatalf("reconciled storage failure returned an error: %v", err)
	}
	if err := store.recordStorageFailure(ctx, job, attemptID, profileEndpoint, response,
		"archive_write_failed", "normal"); err != nil {
		t.Fatalf("repeated reconciled storage failure returned an error: %v", err)
	}

	var storageFailures, observations int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = 'profile' AND outcome = 'storage_failed'
	`, attemptID).Scan(&storageFailures); err != nil {
		t.Fatalf("count storage failure endpoint results: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count storage failure observations: %v", err)
	}
	if storageFailures != 1 || observations != 0 {
		t.Fatalf("durable storage effects = %d endpoint results and %d observations; want 1 and 0", storageFailures, observations)
	}
}

func TestVersionTwoRetryResolutionReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, _, response := prepareAmbiguousObservationFixture(t, ctx, store)
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = 'observed', response_completed_at = $2
		WHERE attempt_id = $1 AND endpoint = 'battle_log'
	`, attemptID, response.responseCompletedAt); err != nil {
		t.Fatalf("mark sibling endpoint observed: %v", err)
	}
	if err := store.recordTransportFailure(ctx, job, attemptID, profileEndpoint,
		response.requestStartedAt, response.responseCompletedAt,
		response.responseCompletedAt.Add(time.Minute), "timeout", "normal"); err != nil {
		t.Fatalf("seed retry endpoint failure: %v", err)
	}

	commitErr := errors.New("injected ambiguous retry resolution commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}
	now := time.Now().UTC()
	if err := store.resolveAttempt(ctx, job, attemptID, now, 3); err != nil {
		t.Fatalf("reconciled retry resolution returned an error: %v", err)
	}
	if err := store.resolveAttempt(ctx, job, attemptID, now, 3); err != nil {
		t.Fatalf("repeated reconciled retry resolution returned an error: %v", err)
	}

	var rootStatus, attemptStatus, retryingEndpoint string
	if err := store.pool.QueryRow(ctx, `
		SELECT job.status, attempt.status, endpoint_result.outcome
		FROM collector_jobs AS job
		JOIN collector_attempts AS attempt ON attempt.id = $2
		JOIN collector_endpoint_results AS endpoint_result
		  ON endpoint_result.attempt_id = attempt.id AND endpoint_result.endpoint = 'profile'
		WHERE job.id = $1
	`, job.id, attemptID).Scan(&rootStatus, &attemptStatus, &retryingEndpoint); err != nil {
		t.Fatalf("read reconciled retry state: %v", err)
	}
	var retryJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs
		WHERE parent_attempt_id = $1 AND work_type = 'endpoint_retry'
	`, attemptID).Scan(&retryJobs); err != nil {
		t.Fatalf("count retry jobs: %v", err)
	}
	if rootStatus != "waiting_retry" || attemptStatus != "incomplete" || retryingEndpoint != "retrying" || retryJobs != 1 {
		t.Fatalf("reconciled retry state = job %q, attempt %q, endpoint %q, retry jobs %d; want waiting_retry, incomplete, retrying, 1",
			rootStatus, attemptStatus, retryingEndpoint, retryJobs)
	}
}

func TestVersionTwoTerminalCompletionReconcilesCommittedCommitErrorAndIsIdempotent(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, _, response := prepareAmbiguousObservationFixture(t, ctx, store)
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = 'observed', response_completed_at = $2
		WHERE attempt_id = $1
	`, attemptID, response.responseCompletedAt); err != nil {
		t.Fatalf("mark completion endpoints observed: %v", err)
	}

	commitErr := errors.New("injected ambiguous terminal completion commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		if err := tx.Commit(ctx); err != nil {
			return err
		}
		return commitErr
	}
	now := time.Now().UTC()
	if err := store.finishAttempt(ctx, job, attemptID, now); err != nil {
		t.Fatalf("reconciled terminal completion returned an error: %v", err)
	}
	if err := store.finishAttempt(ctx, job, attemptID, now); err != nil {
		t.Fatalf("repeated reconciled terminal completion returned an error: %v", err)
	}

	var jobStatus, attemptStatus string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, job.id).Scan(&jobStatus); err != nil {
		t.Fatalf("read completed job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts WHERE id = $1`, attemptID).Scan(&attemptStatus); err != nil {
		t.Fatalf("read completed attempt status: %v", err)
	}
	if jobStatus != "complete" || attemptStatus != "complete" {
		t.Fatalf("reconciled terminal completion state = job %q, attempt %q; want complete, complete", jobStatus, attemptStatus)
	}
}

func TestVersionTwoObservationPartialCommitStateIsUnknown(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	job, attemptID, requestCount, response := prepareAmbiguousObservationFixture(t, ctx, store)
	hash := "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
	if err := store.commitObservation(ctx, job, attemptID, profileEndpoint, requestCount, response,
		hash, "archive/profile-partial.json", "collector-v2", "normal", "observed", nil); err != nil {
		t.Fatalf("seed observation commit: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		DELETE FROM python_processing_jobs
		WHERE observation_id = (SELECT id FROM collector_observations WHERE occurrence_key = $1)
	`, fmt.Sprintf("%d:%s:%d", attemptID, profileEndpoint, requestCount)); err != nil {
		t.Fatalf("create partial observation state: %v", err)
	}

	commitErr := errors.New("injected partial observation commit")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		_ = tx.Rollback(ctx)
		return commitErr
	}
	err := store.commitObservation(ctx, job, attemptID, profileEndpoint, requestCount, response,
		hash, "archive/profile-partial.json", "collector-v2", "normal", "observed", nil)
	if !errors.Is(err, errCommitOutcomeUnknown) || !errors.Is(err, commitErr) {
		t.Fatalf("partial observation error = %v, want unknown wrapping commit error", err)
	}
}
