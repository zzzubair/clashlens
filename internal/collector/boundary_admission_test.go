package collector

import (
	"context"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/zzzubair/clashlens/internal/testsupport"
)

func startBoundaryAdmissionDatabase(t *testing.T) string {
	t.Helper()
	databaseURL := testsupport.StartPostgres(t)
	connection, err := pgx.Connect(context.Background(), databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	defer connection.Close(context.Background())
	paths, err := filepath.Glob(filepath.Join("..", "..", "deploy", "migrations", "*.sql"))
	if err != nil {
		t.Fatalf("find production migrations: %v", err)
	}
	sort.Strings(paths)
	for _, path := range paths {
		migration, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read migration %s: %v", path, err)
		}
		if _, err := connection.Exec(context.Background(), string(migration)); err != nil {
			t.Fatalf("apply migration %s: %v", path, err)
		}
	}
	return databaseURL
}

func TestResetSweepCapturesGenerationAtomicallyUnderConcurrentSchedulers(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 4)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	defer store.close()
	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#RACE1', true, now()), ('#RACE2', true, now())
	`); err != nil {
		t.Fatalf("insert race players: %v", err)
	}
	results := make(chan struct {
		id      int64
		created bool
		err     error
	}, 2)
	var wait sync.WaitGroup
	for range 2 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			id, created, err := store.scheduleResetSweep(ctx, boundary)
			results <- struct {
				id      int64
				created bool
				err     error
			}{id, created, err}
		}()
	}
	wait.Wait()
	close(results)
	var sweepID int64
	createdCount := 0
	for result := range results {
		if result.err != nil {
			t.Fatalf("concurrent reset scheduling: %v", result.err)
		}
		if result.created {
			createdCount++
		}
		if sweepID == 0 {
			sweepID = result.id
		} else if result.id != sweepID {
			t.Fatalf("concurrent sweep IDs = %d and %d", sweepID, result.id)
		}
	}
	if createdCount != 1 {
		t.Fatalf("created sweep count = %d, want 1", createdCount)
	}
	var members, generations, generationMembers int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM boundary_publication_generations WHERE boundary_at = $2),
			(SELECT count(*) FROM boundary_publication_generation_members AS member
			 JOIN boundary_publication_generations AS generation ON generation.id = member.generation_id
			 WHERE generation.boundary_at = $2)
	`, sweepID, boundary).Scan(&members, &generations, &generationMembers); err != nil {
		t.Fatalf("read atomic reset outputs: %v", err)
	}
	if members != 2 || generations != 1 || generationMembers != 2 {
		t.Fatalf("atomic reset outputs = %d members, %d generations, %d copied rows; want 2, 1, 2", members, generations, generationMembers)
	}
}

func TestResetSweepBaselineRowsStayScopedToCurrentSweep(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 4)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	defer store.close()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#SWEEP_A', true, now())
	`); err != nil {
		t.Fatalf("insert first-sweep player: %v", err)
	}
	firstBoundary := time.Date(2026, time.August, 4, 5, 0, 0, 0, time.UTC)
	firstSweep, created, err := store.scheduleResetSweep(ctx, firstBoundary)
	if err != nil || !created {
		t.Fatalf("schedule first reset sweep: id=%d created=%v err=%v", firstSweep, created, err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete' WHERE sweep_id = $1
	`, firstSweep); err != nil {
		t.Fatalf("complete first-sweep jobs: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE players SET active = false WHERE normalized_tag = '#SWEEP_A';
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#SWEEP_B', true, now())
	`); err != nil {
		t.Fatalf("rotate active players: %v", err)
	}
	secondBoundary := firstBoundary.Add(24 * time.Hour)
	secondSweep, created, err := store.scheduleResetSweep(ctx, secondBoundary)
	if err != nil || !created {
		t.Fatalf("schedule second reset sweep: id=%d created=%v err=%v", secondSweep, created, err)
	}
	var members, baselines, leaked int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM collector_reset_baseline_sweeps WHERE reset_sweep_id = $1),
			(SELECT count(*)
			 FROM collector_reset_baseline_sweeps AS baseline
			 JOIN collector_reset_sweep_members AS member
			   ON member.sweep_id = $1 AND member.player_id = baseline.player_id
			 WHERE baseline.reset_sweep_id = $2)
	`, secondSweep, secondSweep).Scan(&members, &baselines, &leaked); err != nil {
		t.Fatalf("read second-sweep baseline scope: %v", err)
	}
	if members != 1 || baselines != 1 || leaked != 1 {
		t.Fatalf("second-sweep scope = %d members, %d baselines, %d matching; want 1, 1, 1", members, baselines, leaked)
	}
}

func TestRegularAdmissionFailsClosedWithoutAdmissionRow(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 4)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	defer store.close()
	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	allowed, err := store.regularAdmissionAllowed(ctx, boundary)
	if err != nil {
		t.Fatalf("regular admission check: %v", err)
	}
	if allowed {
		t.Fatal("regular admission allowed without durable admission row")
	}
}

func TestCollectorRoleCanCaptureGenerationOneMembership(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect to test PostgreSQL: %v", err)
	}
	defer connection.Close(ctx)
	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	var playerID, sweepID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#ROLE-GEN1', true) RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert role test player: %v", err)
	}
	if err := connection.QueryRow(ctx,
		`INSERT INTO collector_reset_sweeps (boundary_at) VALUES ($1) RETURNING id`, boundary,
	).Scan(&sweepID); err != nil {
		t.Fatalf("insert role test sweep: %v", err)
	}
	if _, err := connection.Exec(ctx,
		`INSERT INTO collector_reset_sweep_members (sweep_id, player_id) VALUES ($1, $2)`, sweepID, playerID,
	); err != nil {
		t.Fatalf("insert role test member: %v", err)
	}
	if _, err := connection.Exec(ctx, "SET ROLE clashlens_collector"); err != nil {
		t.Fatalf("set collector role: %v", err)
	}
	tx, err := connection.Begin(ctx)
	if err != nil {
		t.Fatalf("begin collector transaction: %v", err)
	}
	if err := ensureBoundaryGeneration(ctx, tx, boundary, sweepID); err != nil {
		_ = tx.Rollback(ctx)
		t.Fatalf("collector generation capture: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		t.Fatalf("commit collector generation capture: %v", err)
	}
	secondTx, err := connection.Begin(ctx)
	if err != nil {
		t.Fatalf("begin second collector transaction: %v", err)
	}
	if err := ensureBoundaryGeneration(ctx, secondTx, boundary, sweepID); err != nil {
		_ = secondTx.Rollback(ctx)
		t.Fatalf("collector generation recapture: %v", err)
	}
	if err := secondTx.Commit(ctx); err != nil {
		t.Fatalf("commit collector generation recapture: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		UPDATE boundary_publication_generations
		SET snapshot_state = 'published'
		WHERE id = (SELECT id FROM boundary_publication_generations WHERE boundary_at = $1)
	`, boundary); err == nil {
		t.Fatal("collector role mutated publication state")
	}
	if _, err := connection.Exec(ctx, `
		UPDATE boundary_publication_generations
		SET membership_captured_at = NULL
		WHERE id = (SELECT id FROM boundary_publication_generations WHERE boundary_at = $1)
	`, boundary); err == nil {
		t.Fatal("collector role reset generation capture timestamp")
	}
	if _, err := connection.Exec(ctx, `
		UPDATE boundary_publication_generation_members
		SET player_id = player_id
		WHERE player_id = $1
	`, playerID); err == nil {
		t.Fatal("collector role mutated captured generation membership")
	}
	if _, err := connection.Exec(ctx, "RESET ROLE"); err != nil {
		t.Fatalf("reset role: %v", err)
	}
	var count int
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM boundary_publication_generation_members
		WHERE player_id = $1
	`, playerID).Scan(&count); err != nil {
		t.Fatalf("read role generation member: %v", err)
	}
	if count != 1 {
		t.Fatalf("collector generation members = %d, want 1", count)
	}
}

func TestBoundaryAdmissionExcludesRegularWorkUntilSafeHandoff(t *testing.T) {
	databaseURL := startBoundaryAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 4)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	defer store.close()

	boundary := time.Date(2026, time.August, 5, 5, 0, 0, 0, time.UTC)
	beforeReset := boundary.Add(-3 * time.Minute)
	if _, err := store.pool.Exec(ctx, `
        INSERT INTO players (normalized_tag, active, next_due_at)
        VALUES ('#GATE', true, $1)
    `, boundary.Add(-time.Hour)); err != nil {
		t.Fatalf("insert active player: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
        INSERT INTO collector_jobs (
            work_type, player_id, normalized_tag, capacity_pool, priority,
            due_at, coalescing_key, status, created_at, updated_at
        ) VALUES (
            'regular_poll', (SELECT id FROM players WHERE normalized_tag = '#GATE'),
            '#GATE', 'normal', 100, $1, 'gate-seed', 'pending', $2, $2
        )
    `, beforeReset, boundary.Add(-time.Minute)); err != nil {
		t.Fatalf("seed prior regular job: %v", err)
	}

	created, err := store.scheduleDueRegular(ctx, beforeReset, 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule before reset: %v", err)
	}
	if created != 0 {
		t.Fatalf("regular work admitted in reset window: %d", created)
	}

	if _, err := store.pool.Exec(ctx, `
        WITH parent_attempt AS (
            INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
            SELECT id, 'complete', clock_timestamp(), clock_timestamp()
            FROM collector_jobs WHERE coalescing_key = 'gate-seed'
            RETURNING id, job_id
        )
        INSERT INTO collector_jobs (
            work_type, player_id, normalized_tag, capacity_pool, priority,
            due_at, coalescing_key, parent_attempt_id, required_endpoint, status
        )
        SELECT 'endpoint_retry', player_id, normalized_tag, 'normal', 50,
               clock_timestamp(), 'gate-descendant', parent_attempt.id,
               'profile', 'pending'
        FROM collector_jobs AS parent
        JOIN parent_attempt ON parent_attempt.job_id = parent.id
        WHERE parent.coalescing_key = 'gate-seed'
    `); err != nil {
		t.Fatalf("seed regular descendant: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs SET status = 'waiting_dependency', updated_at = clock_timestamp()
        WHERE coalescing_key = 'gate-seed'
    `); err != nil {
		t.Fatalf("move prior regular job to dependency wait: %v", err)
	}
	waitingSweepID, waitingCreated, err := store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil {
		t.Fatalf("prepare boundary with waiting regular work: %v", err)
	}
	if waitingCreated || waitingSweepID != 0 {
		t.Fatalf("waiting regular work was treated as drained: sweep=%d created=%v", waitingSweepID, waitingCreated)
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
        WHERE coalescing_key = 'gate-seed'
    `); err != nil {
		t.Fatalf("drain prior regular job: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
        WHERE coalescing_key = 'gate-descendant'
    `); err != nil {
		t.Fatalf("drain regular descendant: %v", err)
	}
	sweepID, createdSweep, err := store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil {
		t.Fatalf("prepare reset boundary: %v", err)
	}
	if !createdSweep || sweepID == 0 {
		t.Fatalf("reset sweep = %d created=%v, want new sweep", sweepID, createdSweep)
	}
	var admissionState string
	if err := store.pool.QueryRow(ctx, `
		SELECT state FROM collector_boundary_admission WHERE boundary_at = $1
	`, boundary).Scan(&admissionState); err != nil {
		t.Fatalf("read reset admission state: %v", err)
	}
	if admissionState != "reset_draining" {
		t.Fatalf("reset admission state = %q, want reset_draining", admissionState)
	}
	created, err = store.scheduleDueRegular(ctx, boundary, 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule while reset runs: %v", err)
	}
	if created != 0 {
		t.Fatalf("regular work overlapped reset baseline: %d", created)
	}

	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs
        SET status = 'waiting_dependency', updated_at = clock_timestamp()
        WHERE id = (
            SELECT id FROM collector_jobs
            WHERE sweep_id = $1 AND work_type = 'reset_baseline'
            ORDER BY id LIMIT 1
        )
    `, sweepID); err != nil {
		t.Fatalf("move reset job to dependency wait: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(time.Minute)); err != nil {
		t.Fatalf("prepare boundary with waiting reset work: %v", err)
	}
	allowed, err := store.regularAdmissionAllowed(ctx, boundary.Add(time.Minute))
	if err != nil {
		t.Fatalf("check admission with waiting reset work: %v", err)
	}
	if allowed {
		t.Fatal("waiting reset work was treated as drained")
	}
	if _, err := store.pool.Exec(ctx, `
        UPDATE collector_jobs
        SET status = 'complete', updated_at = clock_timestamp()
        WHERE sweep_id = $1 AND work_type = 'reset_baseline'
    `, sweepID); err != nil {
		t.Fatalf("drain reset jobs: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(10*time.Minute)); err != nil {
		t.Fatalf("record reset safe handoff: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT state FROM collector_boundary_admission WHERE boundary_at = $1
	`, boundary).Scan(&admissionState); err != nil {
		t.Fatalf("read safe admission state: %v", err)
	}
	if admissionState != "safe_handoff" {
		t.Fatalf("safe admission state = %q, want safe_handoff", admissionState)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'waiting_dependency', updated_at = clock_timestamp()
		WHERE sweep_id = $1 AND work_type = 'reset_baseline'
	`, sweepID); err != nil {
		t.Fatalf("reopen reset descendant: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(11*time.Minute)); err != nil {
		t.Fatalf("reevaluate invalid safe handoff: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		SELECT state FROM collector_boundary_admission WHERE boundary_at = $1
	`, boundary).Scan(&admissionState); err != nil {
		t.Fatalf("read invalidated handoff: %v", err)
	}
	if admissionState != "reset_draining" {
		t.Fatalf("invalidated admission state = %q, want reset_draining", admissionState)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
		WHERE sweep_id = $1 AND work_type = 'reset_baseline'
	`, sweepID); err != nil {
		t.Fatalf("complete reopened reset descendant: %v", err)
	}
	if _, _, err := store.prepareBoundaryAdmission(ctx, boundary.Add(12*time.Minute)); err != nil {
		t.Fatalf("reestablish safe handoff: %v", err)
	}
	created, err = store.scheduleDueRegular(ctx, boundary.Add(12*time.Minute), 5*time.Minute, 10)
	if err != nil {
		t.Fatalf("schedule after safe handoff: %v", err)
	}
	if created != 1 {
		t.Fatalf("regular work after safe handoff = %d, want 1", created)
	}

	// A reset older than the immediately prior day still blocks the next
	// day's 00:01 regular admission; checking only boundary-24h misses this.
	oldBoundary := boundary.Add(-24 * time.Hour)
	var oldSweepID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_reset_sweeps (boundary_at)
		VALUES ($1) RETURNING id
	`, oldBoundary).Scan(&oldSweepID); err != nil {
		t.Fatalf("insert older reset sweep: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_reset_sweep_members (sweep_id, player_id)
		SELECT $1, id FROM players WHERE normalized_tag = '#GATE'
	`, oldSweepID); err != nil {
		t.Fatalf("insert older reset member: %v", err)
	}
	var oldBaselineID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_reset_baseline_sweeps (
			reset_sweep_id, player_id, boundary_at, evidence_kind
		) VALUES (
			$1, (SELECT id FROM players WHERE normalized_tag = '#GATE'),
			$2, 'paired_v2'
		) RETURNING id
	`, oldSweepID, oldBoundary).Scan(&oldBaselineID); err != nil {
		t.Fatalf("insert older reset baseline: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, sweep_id, reset_baseline_sweep_id, status
		) VALUES (
			'reset_baseline', (SELECT id FROM players WHERE normalized_tag = '#GATE'),
			'#GATE', 'normal', 400, $1, 'older-reset-lineage', $2, $3, 'pending'
		)
	`, oldBoundary, oldSweepID, oldBaselineID); err != nil {
		t.Fatalf("insert older reset job: %v", err)
	}
	allowed, err = store.regularAdmissionAllowed(ctx, boundary.Add(19*time.Hour+time.Minute))
	if err != nil {
		t.Fatalf("check next-day admission: %v", err)
	}
	if allowed {
		t.Fatal("older reset lineage was not blocking next-day admission")
	}
}
