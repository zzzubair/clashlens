package collector

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestInteractiveIntentsCoalesceAndRecentSuccessSatisfiesRefresh(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	now := time.Now().UTC()
	results := make([]interactiveIntentResult, 2)
	errorsByIntent := make([]error, 2)
	var wait sync.WaitGroup
	for index := range results {
		index := index
		wait.Add(1)
		go func() {
			defer wait.Done()
			results[index], errorsByIntent[index] = store.enqueueInteractive(
				ctx,
				"live_refresh",
				"#2PP",
				now,
				30*time.Second,
				false,
			)
		}()
	}
	wait.Wait()
	for index, err := range errorsByIntent {
		if err != nil {
			t.Fatalf("intent %d returned an error: %v", index, err)
		}
	}
	if results[0].jobID != results[1].jobID {
		t.Fatalf("concurrent intents returned job IDs %d and %d", results[0].jobID, results[1].jobID)
	}
	if results[0].reused == results[1].reused {
		t.Fatalf("concurrent intent reuse flags = %v and %v, want one true and one false", results[0].reused, results[1].reused)
	}

	var jobs int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs`).Scan(&jobs); err != nil {
		t.Fatalf("count coalesced jobs: %v", err)
	}
	if jobs != 1 {
		t.Fatalf("coalesced job count = %d, want 1", jobs)
	}

	var attemptID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
		VALUES ($1, 'complete', $2, $2)
		RETURNING id
	`, results[0].jobID, now).Scan(&attemptID); err != nil {
		t.Fatalf("insert completed interactive attempt: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete', result_attempt_id = $2 WHERE id = $1
	`, results[0].jobID, attemptID); err != nil {
		t.Fatalf("complete interactive job: %v", err)
	}

	cooldownResult, err := store.enqueueInteractive(
		ctx,
		"live_refresh",
		"#2PP",
		now.Add(20*time.Second),
		30*time.Second,
		false,
	)
	if err != nil {
		t.Fatalf("cooldown intent returned an error: %v", err)
	}
	if !cooldownResult.reused || cooldownResult.jobID != results[0].jobID || cooldownResult.attemptID != attemptID {
		t.Fatalf("cooldown result = %+v, want prior job %d and attempt %d", cooldownResult, results[0].jobID, attemptID)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs`).Scan(&jobs); err != nil {
		t.Fatalf("count jobs after cooldown: %v", err)
	}
	if jobs != 1 {
		t.Fatalf("job count after cooldown = %d, want 1", jobs)
	}
}

func TestResetSweepFixesActiveMembershipAndCreatesProfileOnlyJobs(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 1)
	if err != nil {
		t.Fatalf("openStore returned an error: %v", err)
	}
	t.Cleanup(store.close)

	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES
			('#2PP', true, now()),
			('#2PQ', true, now()),
			('#2PR', false, now())
	`); err != nil {
		t.Fatalf("insert reset players: %v", err)
	}
	boundary := time.Date(2026, time.August, 3, 5, 0, 0, 0, time.UTC)
	sweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("scheduleResetSweep returned an error: %v", err)
	}
	if !created {
		t.Fatal("first reset sweep was not created")
	}

	if _, err := store.pool.Exec(ctx, `
		UPDATE players SET active = CASE normalized_tag
			WHEN '#2PP' THEN false
			WHEN '#2PR' THEN true
			ELSE active
		END
	`); err != nil {
		t.Fatalf("change eligibility after sweep: %v", err)
	}
	secondSweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("second scheduleResetSweep returned an error: %v", err)
	}
	if created || secondSweepID != sweepID {
		t.Fatalf("second sweep = id %d created %v, want existing id %d", secondSweepID, created, sweepID)
	}

	var members, jobs, nonProfileJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1
	`, sweepID).Scan(&members); err != nil {
		t.Fatalf("count reset members: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE sweep_id = $1
	`, sweepID).Scan(&jobs); err != nil {
		t.Fatalf("count reset jobs: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs
		WHERE sweep_id = $1 AND (work_type <> 'reset_profile' OR required_endpoint IS NOT NULL)
	`, sweepID).Scan(&nonProfileJobs); err != nil {
		t.Fatalf("count invalid reset jobs: %v", err)
	}
	if members != 2 || jobs != 2 || nonProfileJobs != 0 {
		t.Fatalf("reset outputs = %d members, %d jobs, %d invalid jobs; want 2, 2, 0", members, jobs, nonProfileJobs)
	}
}
