package collector

import (
	"context"
	"sync"
	"testing"
	"time"
)

func TestInteractiveIntentsCoalesceAndRecentSuccessSatisfiesRefresh(t *testing.T) {
	databaseURL := startContractDatabase(t)
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
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

func TestResetSweepKeepsMembershipFrozenAcrossEligibilityChanges(t *testing.T) {
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
		t.Fatalf("reset outputs = %d members, %d jobs, %d invalid jobs; want original frozen population 2, 2, 0", members, jobs, nonProfileJobs)
	}
}

func TestVersionTwoResetSweepRetryRepairsLateOrMissingPairedWork(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, now())
	`); err != nil {
		t.Fatalf("insert initial reset player: %v", err)
	}

	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	sweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("schedule initial reset sweep: %v", err)
	}
	if !created {
		t.Fatal("initial reset sweep was not created")
	}

	if _, err := store.pool.Exec(ctx, `
		DELETE FROM collector_jobs
		WHERE sweep_id = $1
	`, sweepID); err != nil {
		t.Fatalf("remove one reset job to model interrupted scheduling: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		DELETE FROM collector_reset_baseline_sweeps
		WHERE reset_sweep_id = $1
	`, sweepID); err != nil {
		t.Fatalf("remove paired reset baselines to model interrupted scheduling: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PQ', true, now())
	`); err != nil {
		t.Fatalf("insert late active reset player: %v", err)
	}

	retriedSweepID, created, err := store.scheduleResetSweep(ctx, boundary)
	if err != nil {
		t.Fatalf("retry reset sweep scheduling: %v", err)
	}
	if created || retriedSweepID != sweepID {
		t.Fatalf("retry reset sweep = id %d created %v, want existing id %d and no new sweep", retriedSweepID, created, sweepID)
	}

	var members, baselines, jobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM collector_reset_baseline_sweeps
			 WHERE reset_sweep_id = $1 AND evidence_kind = 'paired_v2'),
			(SELECT count(*) FROM collector_jobs
			 WHERE sweep_id = $1 AND work_type = 'reset_baseline')
	`, sweepID).Scan(&members, &baselines, &jobs); err != nil {
		t.Fatalf("read repaired paired reset work: %v", err)
	}
	if members != 1 || baselines != 1 || jobs != 1 {
		t.Fatalf("repaired reset work = %d members, %d baselines, %d jobs; want original frozen population 1, 1, 1", members, baselines, jobs)
	}
	var duplicateGroups int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM (
			SELECT reset_baseline_sweep_id
			FROM collector_jobs
			WHERE sweep_id = $1 AND work_type = 'reset_baseline'
			GROUP BY reset_baseline_sweep_id
			HAVING count(*) > 1
		) AS duplicates
	`, sweepID).Scan(&duplicateGroups); err != nil {
		t.Fatalf("count duplicate reset jobs: %v", err)
	}
	if duplicateGroups != 0 {
		t.Fatalf("duplicate reset job groups = %d, want 0", duplicateGroups)
	}
}

func TestVersionTwoGoAndPrivateAPIIntentsShareOneCanonicalEnqueue(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	connection := migratedVersionTwoConnection(t, ctx)
	store, err := openStore(ctx, connection.Config().ConnString(), 2)
	if err != nil {
		t.Fatalf("open version-two store: %v", err)
	}
	t.Cleanup(store.close)

	start := make(chan struct{})
	var wait sync.WaitGroup
	var goResult interactiveIntentResult
	var goError error
	var apiJobID int64
	var apiError error
	wait.Add(2)
	go func() {
		defer wait.Done()
		<-start
		goResult, goError = store.enqueueInteractive(
			ctx,
			"initial_collection",
			"#2PP",
			time.Now().UTC(),
			300*time.Second,
			false,
		)
	}()
	go func() {
		defer wait.Done()
		<-start
		apiError = connection.QueryRow(ctx, `
			SELECT job_id
			FROM clashlens_enqueue_interactive('live_refresh', '#2PP', 300)
		`).Scan(&apiJobID)
	}()
	close(start)
	wait.Wait()

	if goError != nil {
		t.Fatalf("Go version-two enqueue: %v", goError)
	}
	if apiError != nil {
		t.Fatalf("private API enqueue: %v", apiError)
	}
	if goResult.jobID != apiJobID {
		t.Fatalf("cross-runtime enqueue job ids differ: Go=%d API=%d", goResult.jobID, apiJobID)
	}
	var activeJobs, intentEvents int
	if err := connection.QueryRow(ctx, `
		SELECT
			count(*) FILTER (
				WHERE capacity_pool = 'interactive'
				  AND status IN ('pending', 'leased', 'waiting_retry')
			),
			(SELECT count(*) FROM collector_interactive_intent_events WHERE normalized_tag = '#2PP')
		FROM collector_jobs
		WHERE normalized_tag = '#2PP'
	`).Scan(&activeJobs, &intentEvents); err != nil {
		t.Fatalf("read canonical enqueue results: %v", err)
	}
	if activeJobs != 1 || intentEvents != 2 {
		t.Fatalf("canonical enqueue produced active_jobs=%d intent_events=%d, want 1 and 2", activeJobs, intentEvents)
	}
}
