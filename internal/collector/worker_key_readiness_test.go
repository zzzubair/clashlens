package collector

import (
	"context"
	"errors"
	"testing"
	"time"
)

func TestWorkerLeavesWorkUnclaimedWhenCapacityPoolHasNoHealthyKey(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
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

	keys, err := newKeyPool([]APIKey{
		{Label: "normal-a", Secret: "normal-secret", Pool: normalPool},
		{Label: "interactive-a", Secret: "interactive-secret", Pool: interactivePool},
	}, 30, false)
	if err != nil {
		t.Fatalf("newKeyPool returned an error: %v", err)
	}
	if err := keys.quarantine("normal-a"); err != nil {
		t.Fatalf("quarantine normal key: %v", err)
	}
	worker := &worker{
		store: store,
		keys:  keys,
		config: workerConfig{
			owner:         "no-capacity-worker",
			leaseDuration: time.Minute,
		},
	}

	claimed, err := worker.runOnce(ctx, normalPool)
	if claimed || !errors.Is(err, errNoHealthyKey) {
		t.Fatalf("worker result = claimed %v, error %v; want unclaimed no-healthy-key error", claimed, err)
	}
	var status string
	var attempts int
	if err := store.pool.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE work_type = 'regular_poll'`).Scan(&status); err != nil {
		t.Fatalf("read regular job status: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_attempts`).Scan(&attempts); err != nil {
		t.Fatalf("count attempts: %v", err)
	}
	if status != "pending" || attempts != 0 {
		t.Fatalf("durable work = status %q and %d attempts, want pending and 0", status, attempts)
	}
}
