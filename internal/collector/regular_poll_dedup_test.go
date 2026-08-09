package collector

// Regression coverage for the one-active-regular-poll-per-player contract.
//
// The production scheduler enqueues one regular_poll job per player per
// five-minute cycle. The coalescing key embeds the cycle timestamp, so a
// backed-up queue accumulates one active job per cycle for the same player.
// These tests prove that after the fix, each player has at most one active
// regular_poll job (pending, leased, or waiting_retry) at any time, that the
// scheduler still advances next_due_at when the insert is suppressed, that a
// terminal job releases the slot for a later cycle, and that concurrent
// scheduler runs cannot create duplicate active jobs.

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/zzzubair/ClashLens/internal/testsupport"
)

// regularPollSchedulerStore opens a store on a real PostgreSQL instance with
// the production migrations applied. The store tests exercise the scheduler
// exactly as deployed.
func regularPollSchedulerStore(t *testing.T, ctx context.Context) *store {
	t.Helper()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))

	opened, err := openStore(ctx, databaseURL, 2)
	if err != nil {
		t.Fatalf("open store on migrated database: %v", err)
	}
	t.Cleanup(opened.close)
	return opened
}

func insertActiveDuePlayer(t *testing.T, ctx context.Context, store *store, tag string, now time.Time) int64 {
	t.Helper()
	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ($1, true, $2)
		RETURNING id
	`, tag, now.Add(-time.Hour)).Scan(&playerID); err != nil {
		t.Fatalf("insert active due player %s: %v", tag, err)
	}
	return playerID
}

func insertRegularPollJob(t *testing.T, ctx context.Context, store *store, playerID int64, tag, coalescingKey, status string, now time.Time) int64 {
	t.Helper()
	var jobID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		)
		VALUES ('regular_poll', $1, $2, 'normal', 100, $3, $4, $5)
		RETURNING id
	`, playerID, tag, now, coalescingKey, status).Scan(&jobID); err != nil {
		t.Fatalf("insert regular poll job for player %d: %v", playerID, err)
	}
	return jobID
}

func countActiveRegularPolls(t *testing.T, ctx context.Context, store *store, playerID int64) int {
	t.Helper()
	var count int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM collector_jobs
		WHERE work_type = 'regular_poll'
			AND player_id = $1
			AND status IN ('pending', 'leased', 'waiting_retry')
	`, playerID).Scan(&count); err != nil {
		t.Fatalf("count active regular polls for player %d: %v", playerID, err)
	}
	return count
}

func readNextDueAt(t *testing.T, ctx context.Context, store *store, playerID int64) time.Time {
	t.Helper()
	var nextDue time.Time
	if err := store.pool.QueryRow(ctx, `SELECT next_due_at FROM players WHERE id = $1`, playerID).Scan(&nextDue); err != nil {
		t.Fatalf("read next_due_at for player %d: %v", playerID, err)
	}
	return nextDue
}

func TestRegularPollSchedulerKeepsOneActiveJobAcrossLaterCycles(t *testing.T) {
	ctx := context.Background()
	store := regularPollSchedulerStore(t, ctx)

	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)
	playerID := insertActiveDuePlayer(t, ctx, store, "#2PP", now)

	created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("scheduleDueRegular at first cycle returned an error: %v", err)
	}
	if created != 1 {
		t.Fatalf("first cycle created %d jobs, want 1", created)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("first cycle did not leave exactly one active regular poll")
	}
	firstDue := readNextDueAt(t, ctx, store, playerID)
	if !firstDue.After(now) {
		t.Fatalf("first cycle next_due_at = %s, want after %s", firstDue, now)
	}

	secondCycle := now.Add(5 * time.Minute)
	created, err = store.scheduleDueRegular(ctx, secondCycle, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("scheduleDueRegular at second cycle returned an error: %v", err)
	}
	if created != 0 {
		t.Fatalf("second cycle created %d jobs while one is active, want 0", created)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("second cycle left more than one active regular poll")
	}
	secondDue := readNextDueAt(t, ctx, store, playerID)
	if !secondDue.After(firstDue) {
		t.Fatalf("second cycle next_due_at = %s, want after %s", secondDue, firstDue)
	}

	thirdCycle := secondCycle.Add(5 * time.Minute)
	created, err = store.scheduleDueRegular(ctx, thirdCycle, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("scheduleDueRegular at third cycle returned an error: %v", err)
	}
	if created != 0 {
		t.Fatalf("third cycle created %d jobs while one is active, want 0", created)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("third cycle left more than one active regular poll")
	}
	thirdDue := readNextDueAt(t, ctx, store, playerID)
	if !thirdDue.After(secondDue) {
		t.Fatalf("third cycle next_due_at = %s, want after %s", thirdDue, secondDue)
	}

	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'complete', updated_at = $2
		WHERE work_type = 'regular_poll' AND player_id = $1
	`, playerID, thirdCycle); err != nil {
		t.Fatalf("complete active regular poll: %v", err)
	}

	fourthCycle := thirdCycle.Add(5 * time.Minute)
	created, err = store.scheduleDueRegular(ctx, fourthCycle, 5*time.Minute, 100)
	if err != nil {
		t.Fatalf("scheduleDueRegular after terminal job returned an error: %v", err)
	}
	if created != 1 {
		t.Fatalf("cycle after terminal job created %d jobs, want 1", created)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("cycle after terminal job left more than one active regular poll")
	}

	var totalJobs int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE work_type = 'regular_poll' AND player_id = $1
	`, playerID).Scan(&totalJobs); err != nil {
		t.Fatalf("count total regular polls: %v", err)
	}
	if totalJobs != 2 {
		t.Fatalf("total regular poll rows = %d, want 2 (terminal history plus new active job)", totalJobs)
	}
}

func TestRegularPollSchedulerSkipsPlayersWithActiveJobInAnyActiveStatus(t *testing.T) {
	ctx := context.Background()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)

	for index, status := range []string{"pending", "leased", "waiting_retry"} {
		tag := "#2PP" + string(rune('A'+index))
		playerID := insertActiveDuePlayer(t, ctx, store, tag, now)
		insertRegularPollJob(t, ctx, store, playerID, tag, "seed-"+status, status, now)

		created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
		if err != nil {
			t.Fatalf("scheduleDueRegular with %s job returned an error: %v", status, err)
		}
		if created != 0 {
			t.Fatalf("scheduleDueRegular created %d jobs while a %s job is active, want 0", created, status)
		}
		if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
			t.Fatalf("player with %s job has %d active regular polls, want 1", status, countActiveRegularPolls(t, ctx, store, playerID))
		}
	}
}

func TestRegularPollSchedulerEnqueuesAfterTerminalStatus(t *testing.T) {
	ctx := context.Background()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)

	for index, status := range []string{"complete", "failed", "cancelled"} {
		tag := "#2PP" + string(rune('A'+index))
		playerID := insertActiveDuePlayer(t, ctx, store, tag, now)
		insertRegularPollJob(t, ctx, store, playerID, tag, "seed-"+status, status, now)

		created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
		if err != nil {
			t.Fatalf("scheduleDueRegular after %s job returned an error: %v", status, err)
		}
		if created != 1 {
			t.Fatalf("scheduleDueRegular created %d jobs after a %s job, want 1", created, status)
		}
		if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
			t.Fatalf("player with terminal %s job has %d active regular polls, want 1", status, countActiveRegularPolls(t, ctx, store, playerID))
		}
		var totalJobs int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*) FROM collector_jobs WHERE work_type = 'regular_poll' AND player_id = $1
		`, playerID).Scan(&totalJobs); err != nil {
			t.Fatalf("count total regular polls: %v", err)
		}
		if totalJobs != 2 {
			t.Fatalf("total regular poll rows after %s = %d, want 2 (terminal history plus new active job)", status, totalJobs)
		}
	}
}

func TestRegularPollActiveUniqueIndexRejectsDuplicateActiveInsert(t *testing.T) {
	ctx := context.Background()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)
	playerID := insertActiveDuePlayer(t, ctx, store, "#2PP", now)
	insertRegularPollJob(t, ctx, store, playerID, "#2PP", "first", "pending", now)

	_, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		)
		VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'second-active', 'pending')
	`, playerID, now)
	var pgErr *pgconn.PgError
	if !errors.As(err, &pgErr) || pgErr.Code != "23505" {
		t.Fatalf("second active insert error = %v, want unique violation 23505", err)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("unique violation left more than one active regular poll")
	}

	command, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		)
		VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'second-active-do-nothing', 'pending')
		ON CONFLICT DO NOTHING
	`, playerID, now)
	if err != nil {
		t.Fatalf("second active insert with ON CONFLICT DO NOTHING returned an error: %v", err)
	}
	if command.RowsAffected() != 0 {
		t.Fatalf("second active insert with ON CONFLICT DO NOTHING affected %d rows, want 0", command.RowsAffected())
	}

	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		)
		VALUES ('regular_poll', $1, '#2PP', 'normal', 100, $2, 'terminal-history', 'complete')
	`, playerID, now); err != nil {
		t.Fatalf("terminal history insert returned an error: %v", err)
	}
	if countActiveRegularPolls(t, ctx, store, playerID) != 1 {
		t.Fatal("terminal history insert changed the active regular poll count")
	}
}

func TestRegularPollSchedulerConcurrentRunsNeverCreateDuplicateActiveJobs(t *testing.T) {
	ctx := context.Background()
	store := regularPollSchedulerStore(t, ctx)
	base := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)

	const playerCount = 5
	playerIDs := make([]int64, playerCount)
	for index := 0; index < playerCount; index++ {
		tag := "#2PP" + string(rune('A'+index))
		playerIDs[index] = insertActiveDuePlayer(t, ctx, store, tag, base)
	}

	const schedulerRuns = 6
	var waitGroup sync.WaitGroup
	for run := 0; run < schedulerRuns; run++ {
		waitGroup.Add(1)
		go func(run int) {
			defer waitGroup.Done()
			// Each goroutine simulates one scheduler instance with its own
			// clock. The clocks are skewed by one minute and each instance
			// walks three later cycles, so instances race across cycle
			// boundaries while the first cycle's job is still active.
			for cycle := 0; cycle < 3; cycle++ {
				cycleNow := base.Add(time.Duration(run)*time.Minute + time.Duration(cycle)*5*time.Minute)
				if _, err := store.scheduleDueRegular(ctx, cycleNow, 5*time.Minute, 100); err != nil {
					t.Errorf("concurrent scheduleDueRegular at %s returned an error: %v", cycleNow, err)
					return
				}
			}
		}(run)
	}
	waitGroup.Wait()

	for _, playerID := range playerIDs {
		if active := countActiveRegularPolls(t, ctx, store, playerID); active != 1 {
			t.Fatalf("player %d has %d active regular polls after concurrent scheduling, want 1", playerID, active)
		}
	}
}

func TestRegularPollSchedulerDoesNotDeadlockLeaseAdvance(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	store := regularPollSchedulerStore(t, ctx)
	now := time.Now().UTC()
	playerID := insertActiveDuePlayer(t, ctx, store, "#2PP", now)
	insertRegularPollJob(t, ctx, store, playerID, "#2PP", "seed-pending", "pending", now)

	if _, err := store.pool.Exec(ctx, `
		CREATE FUNCTION test_wait_before_regular_insert() RETURNS trigger
		LANGUAGE plpgsql AS $$
		BEGIN
			PERFORM pg_advisory_xact_lock(734702);
			RETURN NEW;
		END
		$$;
		CREATE TRIGGER test_wait_before_regular_insert
		BEFORE INSERT ON collector_jobs
		FOR EACH ROW EXECUTE FUNCTION test_wait_before_regular_insert();
	`); err != nil {
		t.Fatalf("install scheduler ordering gate: %v", err)
	}

	gateConnection, err := pgx.Connect(ctx, store.pool.Config().ConnString())
	if err != nil {
		t.Fatalf("connect scheduler ordering gate: %v", err)
	}
	defer gateConnection.Close(context.Background())
	gateTransaction, err := gateConnection.Begin(ctx)
	if err != nil {
		t.Fatalf("begin scheduler ordering gate: %v", err)
	}
	defer func() { _ = gateTransaction.Rollback(context.Background()) }()
	if _, err := gateTransaction.Exec(ctx, `SELECT pg_advisory_xact_lock(734702)`); err != nil {
		t.Fatalf("lock scheduler ordering gate: %v", err)
	}

	workerConnection, err := pgx.Connect(ctx, store.pool.Config().ConnString())
	if err != nil {
		t.Fatalf("connect lease worker: %v", err)
	}
	defer workerConnection.Close(context.Background())

	type schedulerResult struct {
		created int
		err     error
	}
	schedulerResults := make(chan schedulerResult, 1)
	go func() {
		created, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 100)
		schedulerResults <- schedulerResult{created: created, err: err}
	}()

	gateWaiting := false
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); {
		if err := workerConnection.QueryRow(ctx, `
			SELECT EXISTS (
				SELECT 1
				FROM pg_stat_activity
				WHERE pid <> pg_backend_pid()
					AND query LIKE '%INSERT INTO collector_jobs (%'
					AND wait_event = 'advisory'
			)
		`).Scan(&gateWaiting); err != nil {
			t.Fatalf("observe scheduler ordering-gate wait: %v", err)
		}
		if gateWaiting {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !gateWaiting {
		t.Fatal("scheduler did not reach the ordering gate after locking the player")
	}

	type claimResult struct {
		job *collectionJob
		err error
	}
	claimResults := make(chan claimResult, 1)
	go func() {
		job, err := store.claimNext(ctx, "worker", normalPool, now, time.Minute, "token")
		claimResults <- claimResult{job: job, err: err}
	}()

	time.Sleep(100 * time.Millisecond)
	if err := gateTransaction.Commit(ctx); err != nil {
		t.Fatalf("release scheduler ordering gate: %v", err)
	}
	claimed := <-claimResults
	result := <-schedulerResults
	if claimed.err != nil {
		t.Fatalf("claim regular poll while scheduler waits: %v", claimed.err)
	}
	if claimed.job == nil {
		t.Fatal("claim regular poll returned no job")
	}
	if result.err != nil {
		t.Fatalf("schedule while lease generation advances: %v", result.err)
	}
	if result.created != 0 {
		t.Fatalf("scheduler created %d jobs while one is leased, want 0", result.created)
	}
}

func TestMigration0003CancelsDuplicateActiveRegularPollsKeepingNewest(t *testing.T) {
	ctx := context.Background()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	t.Cleanup(func() { _ = connection.Close(context.Background()) })
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0001_collector.sql"))
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0002_python_layer.sql"))

	now := time.Date(2026, time.August, 2, 12, 2, 0, 0, time.UTC)
	var playerA, playerB int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2PP', true, $1)
		RETURNING id
	`, now.Add(-time.Hour)).Scan(&playerA); err != nil {
		t.Fatalf("insert duplicate-state player A: %v", err)
	}
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#2QQ', true, $1)
		RETURNING id
	`, now.Add(-time.Hour)).Scan(&playerB); err != nil {
		t.Fatalf("insert duplicate-state player B: %v", err)
	}

	// Production-shaped duplicate state: player A has three active regular
	// polls from later cycles (pending, leased, waiting_retry) plus one
	// completed historical row. Player B has one active job and must be
	// untouched. Insertion order defines id order; the newest is the last
	// inserted active row.
	insertSeedJob := func(playerID int64, tag, key, status string) int64 {
		t.Helper()
		var jobID int64
		if err := connection.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status, lease_owner, lease_token, lease_expires_at
			)
			VALUES ('regular_poll', $1, $2, 'normal', 100, $3, $4, $5, 'seed-owner', 'seed-token', clock_timestamp() + interval '1 minute')
			RETURNING id
		`, playerID, tag, now, key, status).Scan(&jobID); err != nil {
			t.Fatalf("insert seed job %s: %v", key, err)
		}
		return jobID
	}
	olderPending := insertSeedJob(playerA, "#2PP", "seed-older-pending", "pending")
	olderLeased := insertSeedJob(playerA, "#2PP", "seed-older-leased", "leased")
	newestWaitingRetry := insertSeedJob(playerA, "#2PP", "seed-newest-waiting-retry", "waiting_retry")
	historicalComplete := insertSeedJob(playerA, "#2PP", "seed-historical-complete", "complete")
	playerBJob := insertSeedJob(playerB, "#2QQ", "seed-player-b", "pending")
	var supersededAttemptID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO collector_attempts (
			job_id, status, started_at, attempt_number,
			lease_owner, lease_token, lease_generation
		)
		VALUES ($1, 'running', $2, 1, 'seed-owner', 'seed-token', 0)
		RETURNING id
	`, olderLeased, now).Scan(&supersededAttemptID); err != nil {
		t.Fatalf("insert superseded running attempt: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome)
		VALUES ($1, 'profile', 'pending')
	`, supersededAttemptID); err != nil {
		t.Fatalf("insert superseded pending endpoint result: %v", err)
	}

	migrationPath := filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql")
	applySQLFile(t, ctx, connection, migrationPath)

	var activeA int
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs
		WHERE work_type = 'regular_poll' AND player_id = $1
			AND status IN ('pending', 'leased', 'waiting_retry')
	`, playerA).Scan(&activeA); err != nil {
		t.Fatalf("count player A active jobs after migration: %v", err)
	}
	if activeA != 1 {
		t.Fatalf("player A has %d active jobs after migration, want 1", activeA)
	}

	var keptJobID int64
	if err := connection.QueryRow(ctx, `
		SELECT id FROM collector_jobs
		WHERE work_type = 'regular_poll' AND player_id = $1
			AND status IN ('pending', 'leased', 'waiting_retry')
	`, playerA).Scan(&keptJobID); err != nil {
		t.Fatalf("read kept job: %v", err)
	}
	if keptJobID != newestWaitingRetry {
		t.Fatalf("kept job id = %d, want newest %d", keptJobID, newestWaitingRetry)
	}

	for _, jobID := range []int64{olderPending, olderLeased} {
		var status, reason string
		var owner, token *string
		var expires *time.Time
		if err := connection.QueryRow(ctx, `
			SELECT status, cancel_reason, lease_owner, lease_token, lease_expires_at
			FROM collector_jobs WHERE id = $1
		`, jobID).Scan(&status, &reason, &owner, &token, &expires); err != nil {
			t.Fatalf("read superseded job %d: %v", jobID, err)
		}
		if status != "cancelled" || reason != "superseded by newer active regular poll" {
			t.Fatalf("superseded job %d = status %q reason %q", jobID, status, reason)
		}
		if owner != nil || token != nil || expires != nil {
			t.Fatalf("superseded job %d still holds lease fields", jobID)
		}
	}
	var attemptStatus, attemptFailure, endpointOutcome, endpointFailure string
	if err := connection.QueryRow(ctx, `
		SELECT a.status, a.failure_category, r.outcome, r.failure_category
		FROM collector_attempts AS a
		JOIN collector_endpoint_results AS r ON r.attempt_id = a.id
		WHERE a.id = $1
	`, supersededAttemptID).Scan(
		&attemptStatus, &attemptFailure, &endpointOutcome, &endpointFailure,
	); err != nil {
		t.Fatalf("read superseded attempt state: %v", err)
	}
	if attemptStatus != "failed" || attemptFailure != "regular_poll_superseded" {
		t.Fatalf("superseded attempt = status %q failure %q", attemptStatus, attemptFailure)
	}
	if endpointOutcome != "failed" || endpointFailure != "regular_poll_superseded" {
		t.Fatalf("superseded endpoint = outcome %q failure %q", endpointOutcome, endpointFailure)
	}

	var completeStatus string
	if err := connection.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, historicalComplete).Scan(&completeStatus); err != nil {
		t.Fatalf("read historical complete job: %v", err)
	}
	if completeStatus != "complete" {
		t.Fatalf("historical complete job status = %q, want complete", completeStatus)
	}
	var playerBStatus string
	if err := connection.QueryRow(ctx, `SELECT status FROM collector_jobs WHERE id = $1`, playerBJob).Scan(&playerBStatus); err != nil {
		t.Fatalf("read player B job: %v", err)
	}
	if playerBStatus != "pending" {
		t.Fatalf("player B job status = %q, want pending", playerBStatus)
	}

	var totalA int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE player_id = $1`, playerA).Scan(&totalA); err != nil {
		t.Fatalf("count player A total jobs: %v", err)
	}
	if totalA != 4 {
		t.Fatalf("player A total rows = %d, want 4 (no destructive deletion)", totalA)
	}

	var indexCount int
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM pg_indexes WHERE indexname = 'collector_jobs_one_active_regular_poll_per_player'
	`).Scan(&indexCount); err != nil {
		t.Fatalf("count unique index: %v", err)
	}
	if indexCount != 1 {
		t.Fatal("partial unique index collector_jobs_one_active_regular_poll_per_player does not exist")
	}

	var contractVersion int
	if err := connection.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&contractVersion); err != nil {
		t.Fatalf("read contract version: %v", err)
	}
	if contractVersion != 2 {
		t.Fatalf("contract version = %d after migration 0003, want 2", contractVersion)
	}
	var migrationCount int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM clash_lens_schema_migrations WHERE version = 3`).Scan(&migrationCount); err != nil {
		t.Fatalf("read schema migrations: %v", err)
	}
	if migrationCount != 1 {
		t.Fatalf("schema migration version 3 rows = %d, want 1", migrationCount)
	}

	// Reapplication must be idempotent and must not change any state.
	applySQLFile(t, ctx, connection, migrationPath)
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs
		WHERE work_type = 'regular_poll' AND player_id = $1
			AND status IN ('pending', 'leased', 'waiting_retry')
	`, playerA).Scan(&activeA); err != nil {
		t.Fatalf("count player A active jobs after reapply: %v", err)
	}
	if activeA != 1 {
		t.Fatalf("player A has %d active jobs after migration reapply, want 1", activeA)
	}
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE player_id = $1`, playerA).Scan(&totalA); err != nil {
		t.Fatalf("count player A total jobs after reapply: %v", err)
	}
	if totalA != 4 {
		t.Fatalf("player A total rows after reapply = %d, want 4", totalA)
	}
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM pg_indexes WHERE indexname = 'collector_jobs_one_active_regular_poll_per_player'
	`).Scan(&indexCount); err != nil {
		t.Fatalf("count unique index after reapply: %v", err)
	}
	if indexCount != 1 {
		t.Fatal("unique index missing after migration reapply")
	}
}
