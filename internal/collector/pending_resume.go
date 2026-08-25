package collector

import (
	"context"
	"errors"
	"time"
)

func (w *worker) resumePendingEndpoint(ctx context.Context, archive rawEvidenceStore, job *collectionJob, attemptID int64, endpoint endpointName) (bool, error) {
	pending, err := w.store.pendingRemoteVerification(ctx, job, attemptID, endpoint)
	if err != nil {
		return true, err
	}
	if pending == nil {
		return false, nil
	}
	body, present, err := archive.readLocal(pending.ResponseHash, pending.ByteSize)
	if err != nil {
		return true, err
	}
	if !present {
		// Fedora loss before remote verification is explicitly permitted. Clear
		// only under the current lease, then the caller makes one fresh source
		// request; it never invents catalogue or observation evidence.
		if err := w.store.clearPendingRemoteVerification(ctx, job, attemptID, endpoint); err != nil {
			return true, err
		}
		return false, nil
	}
	if err := w.store.beginPendingRemoteVerification(ctx, job, attemptID, endpoint, time.Now().UTC()); err != nil {
		return true, err
	}
	reservation, err := archive.reserve(ctx)
	if err != nil {
		return true, err
	}
	response := pending.response()
	// The spool is the sole holder of the exact body; attach its verified
	// bytes so catalogue sizing derives the real length, not an empty one.
	response.body = body
	outcome := "observed"
	var nextRetryAt *time.Time
	if retryableHTTPStatus(response.statusCode) {
		retryAt := w.config.retryPolicy.nextRetryAt(response.responseCompletedAt, pending.RequestCount, response.headers["Retry-After"])
		nextRetryAt = &retryAt
		outcome = "retrying"
	}
	err = archive.secureAndCommit(ctx, reservation, body, func(commitCtx context.Context, reference, hash string, _ int64) error {
		return w.store.commitObservation(commitCtx, job, attemptID, endpoint, pending.RequestCount, response, hash, reference, w.config.collectorVersion, pending.KeyLabel, outcome, nextRetryAt)
	}, func(pendingCtx context.Context, hash, reference string, size int64, pendingResponse officialResponse, _ string) error {
		return w.store.setPendingRemoteVerification(pendingCtx, job, attemptID, endpoint, hash, reference, size, pending.RequestCount, pendingResponse, pending.KeyLabel)
	}, response)
	if err == nil {
		return true, nil
	}
	if errors.Is(err, errPendingRemoteVerification) {
		return true, err
	}
	// The body was locally verified but remote bytes contradicted it or the
	// provider returned a terminal configuration error. recordStorageFailure
	// clears the pending pointer under the lease fence so the v3 CHECK passes.
	return true, w.store.recordStorageFailure(ctx, job, attemptID, endpoint, response, archiveFailureCategory(err), pending.KeyLabel)
}
