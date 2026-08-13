package collector

import (
	"context"
	"testing"
	"time"
)

func TestRecoveryLeaseIsClassifiedAndOnlyRecoveryLaneCanClaimIt(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Now().UTC()
	playerID := insertActiveDuePlayer(t, ctx, store, "#RECOVERY", now)

	var jobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		) VALUES ('regular_poll', $1, '#RECOVERY', 'normal', 100, $2, 'recovery-classification', 'pending')
		RETURNING id
	`, playerID, now).Scan(&jobID); err != nil {
		t.Fatalf("insert recovery fixture: %v", err)
	}
	claimed, err := store.claimNext(ctx, "crashed-collector", normalPool, now, time.Minute, "crashed-token")
	if err != nil || claimed == nil {
		t.Fatalf("claim fixture = %#v, %v; want a job", claimed, err)
	}
	if _, _, err := store.prepareAttempt(ctx, claimed, now); err != nil {
		t.Fatalf("prepare fixture attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_expires_at = clock_timestamp() - interval '1 second'
		WHERE id = $1
	`, jobID); err != nil {
		t.Fatalf("expire fixture lease: %v", err)
	}

	// The normal and genuine interactive lanes must neither recover nor take
	// the abandoned job. Recovery cleanup belongs to its dedicated lane.
	for _, pool := range []capacityPool{normalPool, interactivePool} {
		job, err := store.claimNext(ctx, "non-recovery-worker", pool, time.Now().UTC(), time.Minute, "non-recovery-token-"+string(pool))
		if err != nil {
			t.Fatalf("%s claim returned an error: %v", pool, err)
		}
		if job != nil {
			t.Fatalf("%s lane claimed recovery job %#v", pool, job)
		}
	}

	var retryClass string
	var recoveryReason, recoveryOriginPool *string
	if err := store.pool.QueryRow(ctx, `
		SELECT retry_class, recovery_reason, recovery_origin_pool
		FROM collector_jobs WHERE id = $1
	`, jobID).Scan(&retryClass, &recoveryReason, &recoveryOriginPool); err != nil {
		t.Fatalf("read pre-recovery classification: %v", err)
	}
	if retryClass != "normal" || recoveryReason != nil || recoveryOriginPool != nil {
		t.Fatalf("non-recovery lanes changed classification = %q, %v, %v", retryClass, recoveryReason, recoveryOriginPool)
	}

	recovered, err := store.claimNext(ctx, "recovery-worker", recoveryPool, time.Now().UTC(), time.Minute, "recovery-token")
	if err != nil {
		t.Fatalf("recovery claim returned an error: %v", err)
	}
	if recovered == nil || recovered.id != jobID {
		t.Fatalf("recovery claim = %#v, want job %d", recovered, jobID)
	}
	if recovered.retryClass != "recovery" || recovered.pool != normalPool {
		t.Fatalf("recovery job identity = retry class %q, source pool %q", recovered.retryClass, recovered.pool)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT retry_class, recovery_reason, recovery_origin_pool
		FROM collector_jobs WHERE id = $1
	`, jobID).Scan(&retryClass, &recoveryReason, &recoveryOriginPool); err != nil {
		t.Fatalf("read recovery classification: %v", err)
	}
	if retryClass != "recovery" || recoveryReason == nil || *recoveryReason != "collector_lease_expired" ||
		recoveryOriginPool == nil || *recoveryOriginPool != "normal" {
		t.Fatalf("recovery classification = %q, %v, %v", retryClass, recoveryReason, recoveryOriginPool)
	}
}

func TestNormalLaneDoesNotBypassBoundedRecoveryClassification(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Now().UTC()

	for index, suffix := range "0289PYLQG" {
		tag := "#R" + string(suffix)
		playerID := insertActiveDuePlayer(t, ctx, store, tag, now)
		var jobID int64
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status, lease_owner, lease_token,
				lease_expires_at
			) VALUES (
				'regular_poll', $1, $2, 'normal', 100, $3, $4, 'leased',
				'crashed-collector', $5, $6
			)
			RETURNING id
		`, playerID, tag, now, "recovery-bound-"+string(suffix),
			"crashed-token-"+string(suffix), now.Add(-time.Second)).Scan(&jobID); err != nil {
			t.Fatalf("insert expired recovery fixture %d: %v", index, err)
		}
	}

	claimed, err := store.claimNext(
		ctx, "normal-worker", normalPool, now, time.Minute, "normal-token",
	)
	if err != nil {
		t.Fatalf("normal claim returned an error: %v", err)
	}
	if claimed != nil {
		t.Fatalf("normal lane bypassed recovery classification with job %#v", claimed)
	}
	recovered, err := store.claimNext(
		ctx, "recovery-worker", recoveryPool, now, time.Minute, "recovery-token",
	)
	if err != nil {
		t.Fatalf("recovery claim returned an error: %v", err)
	}
	if recovered == nil || recovered.retryClass != "recovery" {
		t.Fatalf("recovery lane claim = %#v, want classified recovery work", recovered)
	}

	var recoveryCount, stillExpiredCount int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			count(*) FILTER (WHERE retry_class = 'recovery'),
			count(*) FILTER (WHERE retry_class = 'normal' AND status = 'leased')
		FROM collector_jobs
	`).Scan(&recoveryCount, &stillExpiredCount); err != nil {
		t.Fatalf("count bounded recovery classification: %v", err)
	}
	if recoveryCount != collectorExpiredLeaseRecoveryLimit || stillExpiredCount != 1 {
		t.Fatalf(
			"recovery classification counts = %d recovery, %d expired; want %d and 1",
			recoveryCount, stillExpiredCount, collectorExpiredLeaseRecoveryLimit,
		)
	}
}

func TestSharedPermitCallerSeparatesRecoveryFromInteractiveWork(t *testing.T) {
	if got := sharedPermitCaller(&collectionJob{retryClass: "normal"}, true); got != "go" {
		t.Fatalf("normal permit caller = %q, want go", got)
	}
	if got := sharedPermitCaller(&collectionJob{retryClass: "recovery"}, true); got != "go_recovery" {
		t.Fatalf("recovery permit caller = %q, want go_recovery", got)
	}
	if got := sharedPermitCaller(&collectionJob{retryClass: "recovery"}, false); got != "go" {
		t.Fatalf("unsupported recovery permit caller = %q, want bridge go", got)
	}
}

func TestSharedPermitBudgetsKeepInteractiveCapacityWhenRecoveryBacklogs(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store := regularPollSchedulerStore(t, ctx)
	fingerprint := bearerTokenFingerprint("recovery-budget-secret")
	if err := store.registerSharedCredential(ctx, fingerprint, 29, 1, 30, "test:recovery-budget"); err != nil {
		t.Fatalf("register shared credential: %v", err)
	}

	for index := 0; index < 28; index++ {
		permit, err := store.acquireSharedPermit(ctx, fingerprint, "go")
		if err != nil || !permit.granted {
			t.Fatalf("interactive permit %d = %#v, %v; want granted", index+1, permit, err)
		}
	}
	if permit, err := store.acquireSharedPermit(ctx, fingerprint, "go"); err != nil || permit.granted {
		t.Fatalf("29th interactive permit = %#v, %v; want denied by its reserved 28/s lane", permit, err)
	}
	if permit, err := store.acquireSharedPermit(ctx, fingerprint, "go_recovery"); err != nil || !permit.granted {
		t.Fatalf("first recovery permit = %#v, %v; want granted", permit, err)
	}
	if permit, err := store.acquireSharedPermit(ctx, fingerprint, "go_recovery"); err != nil || permit.granted {
		t.Fatalf("second recovery permit = %#v, %v; want denied by its 1/s lane", permit, err)
	}
	if permit, err := store.acquireSharedPermit(ctx, fingerprint, "python"); err != nil || !permit.granted {
		t.Fatalf("Python reserved permit = %#v, %v; want granted", permit, err)
	}
}
