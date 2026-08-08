package collector

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestResolveAttemptDoesNotCommitAfterLeaseExpiresDuringResolution(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	fixture := insertExpiredLeaseFixture(t, ctx, store, 100)
	if _, _, err := store.prepareAttempt(ctx, fixture.job, time.Now().UTC()); err != nil {
		t.Fatalf("prepare attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = 'transport_failed', next_retry_at = clock_timestamp()
		WHERE attempt_id = $1 AND endpoint = 'profile'
	`, fixture.attemptID); err != nil {
		t.Fatalf("seed failed endpoint: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		CREATE FUNCTION sleep_before_endpoint_retry() RETURNS trigger
		LANGUAGE plpgsql AS $$
		BEGIN
			PERFORM pg_sleep(0.1);
			RETURN NEW;
		END;
		$$
	`); err != nil {
		t.Fatalf("create expiry trigger function: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		CREATE TRIGGER sleep_before_endpoint_retry_trigger
		BEFORE UPDATE OF outcome ON collector_endpoint_results
		FOR EACH ROW EXECUTE FUNCTION sleep_before_endpoint_retry()
	`); err != nil {
		t.Fatalf("create expiry trigger: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_expires_at = clock_timestamp() + interval '50 milliseconds'
		WHERE id = $1
	`, fixture.job.id); err != nil {
		t.Fatalf("expire lease during resolution: %v", err)
	}

	err = store.resolveAttempt(ctx, fixture.job, fixture.attemptID, time.Now().UTC(), 3)
	if !errors.Is(err, errLeaseLost) {
		t.Fatalf("resolveAttempt error = %v, want errLeaseLost", err)
	}

	var jobStatus, attemptStatus, endpointOutcome string
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, fixture.job.id).Scan(&jobStatus); err != nil {
		t.Fatalf("read job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_attempts WHERE id = $1`, fixture.attemptID).Scan(&attemptStatus); err != nil {
		t.Fatalf("read attempt status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT outcome FROM collector_endpoint_results
		WHERE attempt_id = $1 AND endpoint = 'profile'
	`, fixture.attemptID).Scan(&endpointOutcome); err != nil {
		t.Fatalf("read endpoint outcome: %v", err)
	}
	if jobStatus != "leased" || attemptStatus != "running" || endpointOutcome != "transport_failed" {
		t.Fatalf("durable state = job %q, attempt %q, endpoint %q; want leased, running, transport_failed", jobStatus, attemptStatus, endpointOutcome)
	}
	var retryJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE parent_attempt_id = $1
	`, fixture.attemptID).Scan(&retryJobs); err != nil {
		t.Fatalf("count retry jobs: %v", err)
	}
	if retryJobs != 0 {
		t.Fatalf("retry jobs = %d, want 0", retryJobs)
	}
}

func TestVersionTwoLeaseGenerationFencesObservationCommit(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule regular work: %v", err)
	}
	job, err := store.claimNext(ctx, "generation-owner", normalPool, now, time.Minute, "generation-token")
	if err != nil {
		t.Fatalf("claim job: %v", err)
	}
	if job == nil {
		t.Fatal("claim returned no job")
	}
	attemptID, endpoints, err := store.prepareAttempt(ctx, job, now)
	if err != nil {
		t.Fatalf("prepare attempt: %v", err)
	}
	if len(endpoints) != 2 {
		t.Fatalf("prepared endpoints = %d, want 2", len(endpoints))
	}
	requestCount, err := store.beginEndpointRequest(ctx, job, attemptID, profileEndpoint, now)
	if err != nil {
		t.Fatalf("begin profile request: %v", err)
	}

	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_generation = lease_generation + 1
		WHERE id = $1
	`, job.id); err != nil {
		t.Fatalf("replace lease generation: %v", err)
	}

	err = store.commitObservation(
		ctx,
		job,
		attemptID,
		profileEndpoint,
		requestCount,
		officialResponse{
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
		},
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"archive/profile.json",
		"test",
		"normal",
		"observed",
		nil,
	)
	if !errors.Is(err, errLeaseLost) {
		t.Fatalf("commitObservation error = %v, want errLeaseLost", err)
	}

	var observations, processingJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count Python jobs: %v", err)
	}
	if observations != 0 || processingJobs != 0 {
		t.Fatalf("fenced outputs = %d observations and %d Python jobs, want 0 and 0", observations, processingJobs)
	}
}

func TestVersionTwoReclaimExpiresOldAttemptAndStartsBoundedFencedAttempt(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule regular work: %v", err)
	}

	expire := func(t *testing.T, job *collectionJob) {
		t.Helper()
		if _, err := store.pool.Exec(ctx, `
			UPDATE collector_jobs
			SET lease_expires_at = clock_timestamp() - interval '1 second'
			WHERE id = $1
		`, job.id); err != nil {
			t.Fatalf("expire lease for job %d: %v", job.id, err)
		}
	}
	attemptNumber := func(t *testing.T, attemptID int64) int {
		t.Helper()
		var number int
		if err := store.pool.QueryRow(ctx, `
			SELECT attempt_number FROM collector_attempts WHERE id = $1
		`, attemptID).Scan(&number); err != nil {
			t.Fatalf("read attempt number for %d: %v", attemptID, err)
		}
		return number
	}
	leaseGeneration := func(t *testing.T, attemptID int64) int64 {
		t.Helper()
		var generation int64
		if err := store.pool.QueryRow(ctx, `
			SELECT lease_generation FROM collector_attempts WHERE id = $1
		`, attemptID).Scan(&generation); err != nil {
			t.Fatalf("read attempt generation for %d: %v", attemptID, err)
		}
		return generation
	}

	firstJob, err := store.claimNext(ctx, "dead-worker", normalPool, now, time.Minute, "dead-token")
	if err != nil {
		t.Fatalf("claim first lease: %v", err)
	}
	if firstJob == nil {
		t.Fatal("first claim returned no job")
	}
	firstAttemptID, _, err := store.prepareAttempt(ctx, firstJob, now)
	if err != nil {
		t.Fatalf("prepare first attempt: %v", err)
	}
	if got := attemptNumber(t, firstAttemptID); got != 1 {
		t.Fatalf("first attempt number = %d, want 1", got)
	}
	if got := leaseGeneration(t, firstAttemptID); got != firstJob.leaseGeneration {
		t.Fatalf("first attempt generation = %d, want job generation %d", got, firstJob.leaseGeneration)
	}
	expire(t, firstJob)

	secondJob, err := store.claimNext(ctx, "reclaimer", normalPool, time.Now().UTC(), time.Minute, "reclaim-token")
	if err != nil {
		t.Fatalf("claim reclaimed lease: %v", err)
	}
	if secondJob == nil {
		t.Fatal("reclaimer did not claim expired job")
	}
	if secondJob.leaseGeneration <= firstJob.leaseGeneration {
		t.Fatalf("reclaimed lease generation = %d, want greater than %d", secondJob.leaseGeneration, firstJob.leaseGeneration)
	}
	var reclaimedStatus string
	var reclaimedLeaseCleared, reclaimedResultCleared bool
	if err := store.pool.QueryRow(ctx, `
		SELECT status, lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL,
		       result_attempt_id IS NULL
		FROM collector_jobs
		WHERE id = $1
	`, firstJob.id).Scan(&reclaimedStatus, &reclaimedLeaseCleared, &reclaimedResultCleared); err != nil {
		t.Fatalf("read reclaimed job identity: %v", err)
	}
	if reclaimedStatus != "leased" || reclaimedLeaseCleared || !reclaimedResultCleared {
		t.Fatalf("reclaimed job before new attempt = status %q, lease cleared %v, result cleared %v; want leased, false, true",
			reclaimedStatus, reclaimedLeaseCleared, reclaimedResultCleared)
	}

	secondAttemptID, _, err := store.prepareAttempt(ctx, secondJob, time.Now().UTC())
	if err != nil {
		t.Fatalf("prepare reclaimed attempt: %v", err)
	}
	if secondAttemptID == firstAttemptID {
		t.Fatalf("reclaimed attempt id = %d, want a new attempt after lease loss", secondAttemptID)
	}
	if got := attemptNumber(t, secondAttemptID); got != 2 {
		t.Fatalf("reclaimed attempt number = %d, want 2", got)
	}
	if got := leaseGeneration(t, secondAttemptID); got != secondJob.leaseGeneration {
		t.Fatalf("reclaimed attempt generation = %d, want job generation %d", got, secondJob.leaseGeneration)
	}

	var oldStatus, oldCategory string
	var oldLeaseCleared bool
	if err := store.pool.QueryRow(ctx, `
		SELECT status, failure_category, lease_owner IS NULL AND lease_token IS NULL
		FROM collector_attempts
		WHERE id = $1
	`, firstAttemptID).Scan(&oldStatus, &oldCategory, &oldLeaseCleared); err != nil {
		t.Fatalf("read expired attempt: %v", err)
	}
	if oldStatus != "failed" || oldCategory != "lease_expired" || !oldLeaseCleared {
		t.Fatalf("expired attempt = status %q category %q lease cleared %v; want failed, lease_expired, true", oldStatus, oldCategory, oldLeaseCleared)
	}

	var newStatus string
	if err := store.pool.QueryRow(ctx, `
		SELECT status FROM collector_attempts WHERE id = $1
	`, secondAttemptID).Scan(&newStatus); err != nil {
		t.Fatalf("read reclaimed attempt: %v", err)
	}
	if newStatus != "running" {
		t.Fatalf("reclaimed attempt status = %q, want running", newStatus)
	}

	var expiryEvents, claimEvents int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FILTER (WHERE event_type = 'lease_expired'),
		       count(*) FILTER (WHERE event_type = 'claimed')
		FROM collector_attempt_events
		WHERE job_id = $1
	`, firstJob.id).Scan(&expiryEvents, &claimEvents); err != nil {
		t.Fatalf("read attempt event history: %v", err)
	}
	if expiryEvents != 1 || claimEvents < 1 {
		t.Fatalf("attempt events = %d lease_expired and %d claimed, want 1 and at least 1", expiryEvents, claimEvents)
	}

	staleResponse := officialResponse{
		requestStartedAt:    now,
		responseCompletedAt: now.Add(time.Second),
		statusCode:          200,
		headers:             map[string]string{"Content-Type": "application/json"},
		request: requestProvenance{
			method:               "GET",
			path:                 "/v1/players/%232PP",
			sourceAdapterVersion: "player-profile-v1",
		},
		pagingEnvelopeState: "not_applicable",
	}
	staleCalls := []struct {
		name string
		call func() error
	}{
		{
			name: "begin endpoint request",
			call: func() error {
				_, err := store.beginEndpointRequest(ctx, firstJob, firstAttemptID, profileEndpoint, time.Now().UTC())
				return err
			},
		},
		{
			name: "transport failure",
			call: func() error {
				return store.recordTransportFailure(ctx, firstJob, firstAttemptID, profileEndpoint,
					now, now.Add(time.Second), now.Add(time.Minute), "stale", "normal")
			},
		},
		{
			name: "storage failure",
			call: func() error {
				return store.recordStorageFailure(ctx, firstJob, firstAttemptID, profileEndpoint, staleResponse, "stale", "normal")
			},
		},
		{
			name: "observation",
			call: func() error {
				return store.commitObservation(ctx, firstJob, firstAttemptID, profileEndpoint, 1, staleResponse,
					"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
					"archive/stale.json", "test", "normal", "observed", nil)
			},
		},
		{
			name: "resolve old attempt",
			call: func() error {
				return store.resolveAttempt(ctx, firstJob, firstAttemptID, time.Now().UTC(), 3)
			},
		},
		{
			name: "resolve new attempt with stale lease",
			call: func() error {
				return store.resolveAttempt(ctx, firstJob, secondAttemptID, time.Now().UTC(), 3)
			},
		},
		{
			name: "finish new attempt with stale lease",
			call: func() error {
				return store.finishAttempt(ctx, firstJob, secondAttemptID, time.Now().UTC())
			},
		},
	}
	for _, staleCall := range staleCalls {
		t.Run(staleCall.name, func(t *testing.T) {
			if err := staleCall.call(); !errors.Is(err, errLeaseLost) {
				t.Fatalf("stale call error = %v, want errLeaseLost", err)
			}
		})
	}
	var observations, processingJobs, transportFailures int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count stale observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count stale Python jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_transport_failures`).Scan(&transportFailures); err != nil {
		t.Fatalf("count stale transport failures: %v", err)
	}
	if observations != 0 || processingJobs != 0 || transportFailures != 0 {
		t.Fatalf("stale outputs = %d observations, %d Python jobs, %d transport failures; want 0, 0, 0", observations, processingJobs, transportFailures)
	}

	expire(t, secondJob)
	thirdJob, err := store.claimNext(ctx, "third-worker", normalPool, time.Now().UTC(), time.Minute, "third-token")
	if err != nil {
		t.Fatalf("claim third lease: %v", err)
	}
	if thirdJob == nil || thirdJob.leaseGeneration <= secondJob.leaseGeneration {
		t.Fatalf("third lease = %#v, want a new higher generation lease", thirdJob)
	}
	thirdAttemptID, _, err := store.prepareAttempt(ctx, thirdJob, time.Now().UTC())
	if err != nil {
		t.Fatalf("prepare third attempt: %v", err)
	}
	if got := attemptNumber(t, thirdAttemptID); got != 3 {
		t.Fatalf("third attempt number = %d, want 3", got)
	}
	expire(t, thirdJob)
	terminalJob, err := store.claimNext(ctx, "terminal-worker", normalPool, time.Now().UTC(), time.Minute, "terminal-token")
	if err != nil {
		t.Fatalf("recover bounded terminal attempt: %v", err)
	}
	if terminalJob != nil {
		t.Fatalf("bounded terminal recovery returned job %#v, want no job", terminalJob)
	}

	var finalJobStatus string
	var finalLeaseCleared, finalResultCleared bool
	if err := store.pool.QueryRow(ctx, `
		SELECT status, lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL,
		       result_attempt_id IS NULL
		FROM collector_jobs
		WHERE id = $1
	`, firstJob.id).Scan(&finalJobStatus, &finalLeaseCleared, &finalResultCleared); err != nil {
		t.Fatalf("read bounded terminal job: %v", err)
	}
	if finalJobStatus != "failed" || !finalLeaseCleared || !finalResultCleared {
		t.Fatalf("bounded terminal job = status %q, lease cleared %v, result cleared %v; want failed, true, true",
			finalJobStatus, finalLeaseCleared, finalResultCleared)
	}
	var totalExpiryEvents int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_attempt_events
		WHERE job_id = $1 AND event_type = 'lease_expired'
	`, firstJob.id).Scan(&totalExpiryEvents); err != nil {
		t.Fatalf("count repeated recovery events: %v", err)
	}
	if totalExpiryEvents != 3 {
		t.Fatalf("lease_expired events = %d, want one for each bounded attempt", totalExpiryEvents)
	}
	if _, err := store.claimNext(ctx, "repeat-worker", normalPool, time.Now().UTC(), time.Minute, "repeat-token"); err != nil {
		t.Fatalf("repeat terminal recovery: %v", err)
	}
	var repeatedExpiryEvents int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_attempt_events
		WHERE job_id = $1 AND event_type = 'lease_expired'
	`, firstJob.id).Scan(&repeatedExpiryEvents); err != nil {
		t.Fatalf("count idempotent recovery events: %v", err)
	}
	if repeatedExpiryEvents != totalExpiryEvents {
		t.Fatalf("repeated recovery events = %d, want %d", repeatedExpiryEvents, totalExpiryEvents)
	}
}

func TestVersionTwoRetryLifecycleFencesAttemptAndJobTerminalWrites(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	store := startVersionTwoStore(t, ctx)

	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PR', true, $1)
	`, now.Add(-time.Hour)); err != nil {
		t.Fatalf("insert retry player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 1); err != nil {
		t.Fatalf("schedule retry work: %v", err)
	}
	rootJob, err := store.claimNext(ctx, "retry-root", normalPool, now, time.Minute, "retry-root-token")
	if err != nil {
		t.Fatalf("claim retry root job: %v", err)
	}
	if rootJob == nil {
		t.Fatal("retry root claim returned no job")
	}
	attemptID, endpoints, err := store.prepareAttempt(ctx, rootJob, now)
	if err != nil {
		t.Fatalf("prepare retry root attempt: %v", err)
	}
	if len(endpoints) != 2 {
		t.Fatalf("retry root endpoints = %d, want 2", len(endpoints))
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = 'observed', response_completed_at = $2
		WHERE attempt_id = $1 AND endpoint = 'battle_log'
	`, attemptID, now); err != nil {
		t.Fatalf("seed observed battle log: %v", err)
	}
	requestCount, err := store.beginEndpointRequest(ctx, rootJob, attemptID, profileEndpoint, now)
	if err != nil {
		t.Fatalf("begin retry root profile request: %v", err)
	}
	if requestCount != 1 {
		t.Fatalf("retry root request count = %d, want 1", requestCount)
	}
	if err := store.recordTransportFailure(ctx, rootJob, attemptID, profileEndpoint,
		now.Add(-time.Second), now, now.Add(-time.Second), "upstream_timeout", "normal"); err != nil {
		t.Fatalf("record retry root transport failure: %v", err)
	}
	if err := store.resolveAttempt(ctx, rootJob, attemptID, time.Now().UTC(), 3); err != nil {
		t.Fatalf("resolve retry root attempt: %v", err)
	}

	var rootStatus, attemptStatus, endpointOutcome string
	var rootLeaseCleared bool
	if err := store.pool.QueryRow(ctx, `
		SELECT job.status, attempt.status, endpoint_result.outcome,
		       job.lease_owner IS NULL AND job.lease_token IS NULL AND job.lease_expires_at IS NULL
		FROM collector_jobs AS job
		JOIN collector_attempts AS attempt ON attempt.id = job.result_attempt_id
		JOIN collector_endpoint_results AS endpoint_result ON endpoint_result.attempt_id = attempt.id
		WHERE job.id = $1 AND endpoint_result.endpoint = 'profile'
	`, rootJob.id).Scan(&rootStatus, &attemptStatus, &endpointOutcome, &rootLeaseCleared); err != nil {
		t.Fatalf("read retry root state: %v", err)
	}
	if rootStatus != "waiting_retry" || attemptStatus != "incomplete" || endpointOutcome != "retrying" || !rootLeaseCleared {
		t.Fatalf("retry root state = job %q, attempt %q, endpoint %q, lease cleared %v; want waiting_retry, incomplete, retrying, true",
			rootStatus, attemptStatus, endpointOutcome, rootLeaseCleared)
	}

	var retryJobID int64
	if err := store.pool.QueryRow(ctx, `
		SELECT id
		FROM collector_jobs
		WHERE parent_attempt_id = $1 AND work_type = 'endpoint_retry' AND status = 'pending'
	`, attemptID).Scan(&retryJobID); err != nil {
		t.Fatalf("read scheduled endpoint retry: %v", err)
	}
	retryJob, err := store.claimNext(ctx, "retry-worker", normalPool, time.Now().UTC(), time.Minute, "retry-token")
	if err != nil {
		t.Fatalf("claim endpoint retry: %v", err)
	}
	if retryJob == nil || retryJob.id != retryJobID {
		t.Fatalf("claimed retry job = %#v, want job %d", retryJob, retryJobID)
	}
	if retryJob.leaseGeneration <= rootJob.leaseGeneration {
		t.Fatalf("retry job generation = %d, want greater than root generation %d", retryJob.leaseGeneration, rootJob.leaseGeneration)
	}
	retryAttemptID, retryEndpoints, err := store.prepareAttempt(ctx, retryJob, time.Now().UTC())
	if err != nil {
		t.Fatalf("prepare endpoint retry attempt: %v", err)
	}
	if retryAttemptID != attemptID || len(retryEndpoints) != 1 || retryEndpoints[0] != profileEndpoint {
		t.Fatalf("retry attempt = id %d endpoints %v; want id %d and profile only", retryAttemptID, retryEndpoints, attemptID)
	}
	retryRequestCount, err := store.beginEndpointRequest(ctx, retryJob, retryAttemptID, profileEndpoint, time.Now().UTC())
	if err != nil {
		t.Fatalf("begin endpoint retry request: %v", err)
	}
	retryNow := time.Now().UTC()
	if err := store.commitObservation(ctx, retryJob, retryAttemptID, profileEndpoint, retryRequestCount, officialResponse{
		requestStartedAt:    retryNow.Add(-time.Second),
		responseCompletedAt: retryNow,
		statusCode:          200,
		headers:             map[string]string{"Content-Type": "application/json"},
		request: requestProvenance{
			method:               "GET",
			path:                 "/v1/players/%232PR",
			sourceAdapterVersion: "player-profile-v1",
		},
		pagingEnvelopeState: "not_applicable",
	}, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
		"archive/retry-profile.json", "test", "normal", "observed", nil); err != nil {
		t.Fatalf("commit endpoint retry observation: %v", err)
	}
	if err := store.resolveAttempt(ctx, retryJob, retryAttemptID, time.Now().UTC(), 3); err != nil {
		t.Fatalf("resolve completed endpoint retry: %v", err)
	}

	var completedAttemptStatus, completedRootStatus, completedRetryStatus string
	if err := store.pool.QueryRow(ctx, `
		SELECT attempt.status, root_job.status, retry_job.status
		FROM collector_attempts AS attempt
		JOIN collector_jobs AS root_job ON root_job.id = $1
		JOIN collector_jobs AS retry_job ON retry_job.id = $2
		WHERE attempt.id = $3
	`, rootJob.id, retryJob.id, attemptID).Scan(&completedAttemptStatus, &completedRootStatus, &completedRetryStatus); err != nil {
		t.Fatalf("read completed retry lifecycle: %v", err)
	}
	if completedAttemptStatus != "complete" || completedRootStatus != "complete" || completedRetryStatus != "complete" {
		t.Fatalf("completed retry lifecycle = attempt %q, root %q, retry %q; want complete for all",
			completedAttemptStatus, completedRootStatus, completedRetryStatus)
	}
	var observations, processingJobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_observations`).Scan(&observations); err != nil {
		t.Fatalf("count retry observations: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM python_processing_jobs`).Scan(&processingJobs); err != nil {
		t.Fatalf("count retry Python jobs: %v", err)
	}
	if observations != 1 || processingJobs != 1 {
		t.Fatalf("retry outputs = %d observations and %d Python jobs; want 1 and 1", observations, processingJobs)
	}
}
