package collector

import (
	"context"
	"testing"
	"time"
)

func TestLeaseHeartbeatPreventsReclaimWhileWorkerMakesProgress(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	// Keep the heartbeat connection independent from the competing claimant.
	store, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

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

	leaseDuration := 60 * time.Millisecond
	job, err := store.claimNext(ctx, "heartbeat-owner", normalPool, now, leaseDuration, "heartbeat-token")
	if err != nil {
		t.Fatalf("claim initial job: %v", err)
	}
	if job == nil {
		t.Fatal("initial claim returned no job")
	}
	worker := &worker{store: store, config: workerConfig{leaseDuration: leaseDuration}}
	heartbeatContext, stopHeartbeat := worker.startLeaseHeartbeat(ctx, job)
	if heartbeatContext == nil {
		t.Fatal("startLeaseHeartbeat returned a nil context")
	}
	t.Cleanup(func() { _ = stopHeartbeat() })

	time.Sleep(140 * time.Millisecond)
	reclaimed, err := store.claimNext(
		ctx,
		"second-owner",
		normalPool,
		time.Now().UTC(),
		leaseDuration,
		"second-token",
	)
	if err != nil {
		t.Fatalf("second claim returned an error: %v", err)
	}
	if reclaimed != nil {
		t.Fatalf("second worker reclaimed job %d while heartbeat was active", reclaimed.id)
	}
	if err := stopHeartbeat(); err != nil {
		t.Fatalf("stop heartbeat returned an error: %v", err)
	}
}
