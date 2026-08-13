package collector

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"
)

type archiveStore interface {
	store(ctx context.Context, hash string, body []byte) (string, error)
}

type workerConfig struct {
	owner               string
	leaseDuration       time.Duration
	collectorVersion    string
	maximumRetries      int
	retryPolicy         retryPolicy
	dependenciesReady   func(context.Context) error
	metrics             *collectorMetrics
	logger              *slog.Logger
	disableLeaseRenewal bool
}

type worker struct {
	store   *store
	archive archiveStore
	api     *officialAPIClient
	keys    *keyPool
	config  workerConfig
}

func newWorker(
	store *store,
	archive archiveStore,
	api *officialAPIClient,
	keys *keyPool,
	config workerConfig,
) *worker {
	return &worker{
		store:   store,
		archive: archive,
		api:     api,
		keys:    keys,
		config:  config,
	}
}

func (w *worker) runOnce(ctx context.Context, pool capacityPool) (bool, error) {
	readinessStartedAt := time.Now()
	if w.config.dependenciesReady != nil {
		if err := w.config.dependenciesReady(ctx); err != nil {
			w.config.metrics.recordStageDuration("dependency_readiness", time.Since(readinessStartedAt))
			return false, err
		}
	}
	w.config.metrics.recordStageDuration("dependency_readiness", time.Since(readinessStartedAt))
	keyPool := pool
	if pool == recoveryPool {
		keyPool = interactivePool
	}
	if err := w.keys.readyForPool(keyPool); err != nil {
		return false, err
	}
	leaseToken, err := randomToken()
	if err != nil {
		return false, err
	}
	claimStartedAt := time.Now()
	job, err := w.store.claimNext(
		ctx,
		w.config.owner,
		pool,
		time.Now().UTC(),
		w.config.leaseDuration,
		leaseToken,
	)
	w.config.metrics.recordStageDuration("claim", time.Since(claimStartedAt))
	if err != nil || job == nil {
		return false, err
	}
	w.config.metrics.recordJob(job.workType, string(job.pool), "claimed")
	jobStartedAt := time.Now()
	defer func() {
		w.config.metrics.recordStageDuration("job", time.Since(jobStartedAt))
	}()
	if w.config.logger != nil {
		w.config.logger.InfoContext(
			ctx,
			"collector job claimed",
			"job_id", job.id,
			"work_type", job.workType,
			"pool", job.pool,
		)
	}
	jobContext, stopHeartbeat := w.startLeaseHeartbeat(ctx, job)

	prepareStartedAt := time.Now()
	attemptID, endpoints, err := w.store.prepareAttempt(jobContext, job, time.Now().UTC())
	w.config.metrics.recordStageDuration("prepare_attempt", time.Since(prepareStartedAt))
	if errors.Is(err, errJobCancelled) {
		_ = stopHeartbeat()
		return true, nil
	}
	if err != nil {
		heartbeatError := stopHeartbeat()
		if heartbeatError != nil {
			return true, errors.Join(err, heartbeatError)
		}
		return true, err
	}

	var wait sync.WaitGroup
	errorsByEndpoint := make(chan error, len(endpoints))
	for _, endpoint := range endpoints {
		endpoint := endpoint
		wait.Add(1)
		go func() {
			defer wait.Done()
			if err := w.collectEndpoint(jobContext, job, attemptID, endpoint); err != nil {
				errorsByEndpoint <- fmt.Errorf("collect %s: %w", endpoint, err)
			}
		}()
	}
	wait.Wait()
	close(errorsByEndpoint)

	var endpointErrors []error
	for endpointError := range errorsByEndpoint {
		endpointErrors = append(endpointErrors, endpointError)
	}
	resolutionStartedAt := time.Now()
	finishError := w.store.resolveAttempt(
		jobContext,
		job,
		attemptID,
		time.Now().UTC(),
		w.config.maximumRetries,
	)
	w.config.metrics.recordStageDuration("attempt_resolution", time.Since(resolutionStartedAt))
	if finishError != nil {
		endpointErrors = append(endpointErrors, finishError)
	}
	if heartbeatError := stopHeartbeat(); heartbeatError != nil {
		endpointErrors = append(endpointErrors, heartbeatError)
	}
	if len(endpointErrors) > 0 {
		w.config.metrics.recordJob(job.workType, string(job.pool), "error")
		if w.config.logger != nil {
			w.config.logger.WarnContext(
				ctx,
				"collector job handling failed",
				"job_id", job.id,
				"work_type", job.workType,
				"pool", job.pool,
				"error_count", len(endpointErrors),
			)
		}
		return true, errors.Join(endpointErrors...)
	}
	w.config.metrics.recordJob(job.workType, string(job.pool), "handled")
	if w.config.logger != nil {
		w.config.logger.InfoContext(
			ctx,
			"collector job handled",
			"job_id", job.id,
			"work_type", job.workType,
			"pool", job.pool,
		)
	}
	return true, nil
}

func (w *worker) collectEndpoint(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
) error {
	for {
		var key APIKey
		var err error
		if job.pool == interactivePool || job.retryClass == "recovery" {
			key, err = w.keys.sharedInteractiveKey()
		} else {
			key, err = w.keys.acquire(ctx, job.pool)
		}
		if err != nil {
			return err
		}
		if err := w.acquireSharedPermitForRequest(ctx, key, job); err != nil {
			return err
		}
		requestStartStartedAt := time.Now()
		requestCount, err := w.store.beginEndpointRequest(ctx, job, attemptID, endpoint, time.Now().UTC())
		w.config.metrics.recordStageDuration("request_start", time.Since(requestStartStartedAt))
		if err != nil {
			return err
		}
		w.config.metrics.recordAPIRequest(string(endpoint), string(job.pool))

		response, err := w.api.fetch(ctx, endpoint, job.normalizedTag, key.Secret)
		duration := time.Duration(0)
		if !response.requestStartedAt.IsZero() {
			duration = time.Since(response.requestStartedAt)
			w.config.metrics.recordAPIDuration(
				string(endpoint),
				string(job.pool),
				duration,
			)
		}
		w.config.metrics.recordStageDuration("official_api_"+string(endpoint), duration)
		if err != nil {
			failedAt := time.Now().UTC()
			nextRetryAt := w.config.retryPolicy.nextRetryAt(failedAt, requestCount, "")
			w.config.metrics.recordAPIOutcome(string(endpoint), transportFailureCategory(err))
			w.config.metrics.recordRetry(string(endpoint))
			if w.config.logger != nil {
				w.config.logger.WarnContext(
					ctx,
					"official API transport failed",
					"job_id", job.id,
					"work_type", job.workType,
					"endpoint", endpoint,
					"pool", job.pool,
					"key_label", key.Label,
					"duration", duration,
					"category", transportFailureCategory(err),
				)
			}
			return w.store.recordTransportFailure(
				ctx,
				job,
				attemptID,
				endpoint,
				response.requestStartedAt,
				failedAt,
				nextRetryAt,
				transportFailureCategory(err),
				key.Label,
			)
		}
		w.config.metrics.recordAPIOutcome(string(endpoint), HTTPStatusClass(response.statusCode))
		authenticationFailure := authenticationFailureStatus(response.statusCode)
		if key.Pool == interactivePool {
			authenticationFailure = sharedCredentialAuthenticationFailure(response)
		}
		if authenticationFailure {
			if key.Pool == interactivePool {
				if err := w.store.quarantineSharedCredential(
					ctx,
					bearerTokenFingerprint(key.Secret),
					"collector:worker",
					"official_api_authentication_failure",
				); err != nil {
					return err
				}
			}
			if err := w.keys.quarantine(key.Label); err != nil {
				return err
			}
			w.config.metrics.recordQuarantine(key.Label, string(key.Pool))
		}
		if response.statusCode == http.StatusTooManyRequests && key.Pool == interactivePool {
			cooldown := time.Second
			if parsed, ok := parseRetryAfter(response.responseCompletedAt, response.headers["Retry-After"]); ok && parsed > 0 {
				cooldown = parsed
			}
			if err := w.store.cooldownSharedCredential(
				ctx,
				bearerTokenFingerprint(key.Secret),
				cooldown,
				"collector:worker",
				"official_api_429",
			); err != nil {
				return err
			}
		}
		digest := sha256.Sum256(response.body)
		hash := hex.EncodeToString(digest[:])
		archiveStartedAt := time.Now()
		archiveReference, err := w.archive.store(ctx, hash, response.body)
		w.config.metrics.recordStageDuration("archive_write", time.Since(archiveStartedAt))
		if err != nil {
			failureCategory := "archive_write_failed"
			if errors.Is(err, errArchiveChecksumMismatch) {
				failureCategory = "archive_checksum_mismatch"
			}
			w.config.metrics.recordStorageError(failureCategory)
			w.config.metrics.recordRetry(string(endpoint))
			if w.config.logger != nil {
				w.config.logger.WarnContext(
					ctx,
					"archive write failed",
					"job_id", job.id,
					"work_type", job.workType,
					"endpoint", endpoint,
					"pool", job.pool,
					"key_label", key.Label,
					"status", response.statusCode,
					"duration", duration,
					"category", failureCategory,
				)
			}
			return w.store.recordStorageFailure(
				ctx,
				job,
				attemptID,
				endpoint,
				response,
				failureCategory,
				key.Label,
			)
		}

		outcome := "observed"
		var nextRetryAt *time.Time
		if retryableHTTPStatus(response.statusCode) {
			w.config.metrics.recordRetry(string(endpoint))
			retryAt := w.config.retryPolicy.nextRetryAt(
				response.responseCompletedAt,
				requestCount,
				response.headers["Retry-After"],
			)
			nextRetryAt = &retryAt
			outcome = "retrying"
		}
		if authenticationFailure {
			w.config.metrics.recordRetry(string(endpoint))
			retryAt := response.responseCompletedAt
			nextRetryAt = &retryAt
			outcome = "retrying"
		}
		observationCommitStartedAt := time.Now()
		err = w.store.commitObservation(
			ctx,
			job,
			attemptID,
			endpoint,
			requestCount,
			response,
			hash,
			archiveReference,
			w.config.collectorVersion,
			key.Label,
			outcome,
			nextRetryAt,
		)
		w.config.metrics.recordStageDuration("observation_commit", time.Since(observationCommitStartedAt))
		if err != nil {
			w.config.metrics.recordStorageError("database_transaction_failed")
			if w.config.logger != nil {
				w.config.logger.WarnContext(
					ctx,
					"observation transaction failed",
					"job_id", job.id,
					"work_type", job.workType,
					"endpoint", endpoint,
					"pool", job.pool,
					"key_label", key.Label,
					"status", response.statusCode,
					"duration", duration,
				)
			}
			if recordError := w.store.recordStorageFailure(
				ctx,
				job,
				attemptID,
				endpoint,
				response,
				"database_transaction_failed",
				key.Label,
			); recordError != nil {
				return errors.Join(err, recordError)
			}
			return nil
		}
		if w.config.logger != nil {
			w.config.logger.InfoContext(
				ctx,
				"official API response archived",
				"job_id", job.id,
				"work_type", job.workType,
				"endpoint", endpoint,
				"pool", job.pool,
				"key_label", key.Label,
				"status", response.statusCode,
				"duration", duration,
				"outcome", outcome,
			)
		}
		if authenticationFailure {
			continue
		}
		return nil
	}
}

func (w *worker) acquireSharedPermitForRequest(ctx context.Context, key APIKey, job *collectionJob) error {
	if key.Pool != interactivePool {
		return nil
	}
	contractVersion, err := w.store.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion < 2 {
		return nil
	}
	fingerprint := bearerTokenFingerprint(key.Secret)
	if err := w.store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "collector:worker"); err != nil {
		return err
	}
	caller := sharedPermitCaller(job, w.store.recoveryRetrySupported)
	for {
		permit, err := w.store.acquireSharedPermit(ctx, fingerprint, caller)
		if err != nil {
			return err
		}
		if permit.granted {
			return nil
		}
		if permit.state == "quarantined" || permit.state == "retired" {
			_ = w.keys.quarantine(key.Label)
			return fmt.Errorf("%w: shared interactive credential is %s", errNoHealthyKey, permit.state)
		}
		wait := 10 * time.Millisecond
		if permit.nextEligibleAt != nil {
			wait = permit.nextEligibleAt.Sub(permit.databaseTime)
			if wait < 10*time.Millisecond {
				wait = 10 * time.Millisecond
			}
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				<-timer.C
			}
			return ctx.Err()
		case <-timer.C:
		}
	}
}

func sharedPermitCaller(job *collectionJob, recoverySupported bool) string {
	if recoverySupported && job != nil && job.retryClass == "recovery" {
		return "go_recovery"
	}
	return "go"
}

func retryableHTTPStatus(statusCode int) bool {
	return statusCode == http.StatusTooManyRequests || statusCode >= 500
}

func authenticationFailureStatus(statusCode int) bool {
	return statusCode == http.StatusUnauthorized || statusCode == http.StatusForbidden
}

func sharedCredentialAuthenticationFailure(response officialResponse) bool {
	if !authenticationFailureStatus(response.statusCode) {
		return false
	}
	var body map[string]string
	if err := json.Unmarshal(response.body, &body); err != nil || len(body) != 2 {
		return false
	}
	return (body["reason"] == "accessDenied" && body["message"] == "Invalid authorization") ||
		(body["reason"] == "accessDenied.invalidIp" && body["message"] == "Invalid IP address")
}

func HTTPStatusClass(statusCode int) string {
	return strconv.Itoa(statusCode/100) + "xx"
}

func transportFailureCategory(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, io.ErrUnexpectedEOF) {
		return "truncated_response"
	}
	var networkError net.Error
	if errors.As(err, &networkError) {
		return "network"
	}
	return "transport"
}

func (p *keyPool) acquire(ctx context.Context, requestedPool capacityPool) (APIKey, error) {
	for {
		key, wait, err := p.tryAcquire(time.Now().UTC(), requestedPool)
		if err == nil {
			return key, nil
		}
		if !errors.Is(err, errRateLimited) {
			return APIKey{}, err
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			if !timer.Stop() {
				<-timer.C
			}
			return APIKey{}, ctx.Err()
		case <-timer.C:
		}
	}
}

func randomToken() (string, error) {
	bytes := make([]byte, 16)
	if _, err := rand.Read(bytes); err != nil {
		return "", fmt.Errorf("generate lease token: %w", err)
	}
	return hex.EncodeToString(bytes), nil
}
