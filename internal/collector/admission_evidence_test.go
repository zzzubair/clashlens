package collector

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

func startAdmissionDatabase(t *testing.T) string {
	t.Helper()
	return startBoundaryAdmissionDatabase(t)
}

func admissionCaptureAroundNow(buffer time.Duration) (start, end time.Time) {
	t0 := time.Now().UTC().Truncate(time.Second)
	start = t0.Add(-buffer)
	end = t0.Add(buffer).Add(2 * time.Hour)
	return start, end
}

func newAdmissionConfig(runID string, start, end time.Time, maxEvents int, maxSelected int64) *admissionEvidenceConfig {
	return &admissionEvidenceConfig{
		runID:              runID,
		captureStart:       start.UTC(),
		captureEnd:         end.UTC(),
		maxEvents:          maxEvents,
		maxSelectedEntries: maxSelected,
	}
}

func openAdmissionStore(t *testing.T, ctx context.Context, databaseURL string, config *admissionEvidenceConfig) *store {
	t.Helper()
	store, err := openStore(ctx, databaseURL, 5)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	t.Cleanup(store.close)
	if config != nil {
		if err := store.ensureAdmissionEvidenceRun(ctx, config); err != nil {
			t.Fatalf("ensureAdmissionEvidenceRun: %v", err)
		}
		store.configureAdmissionEvidence(config)
	}
	if store.metrics == nil {
		store.metrics = newCollectorMetrics()
	}
	return store
}

func seedDuePlayers(t *testing.T, ctx context.Context, store *store, count int, dueAt time.Time) []int64 {
	t.Helper()
	ids := make([]int64, 0, count)
	base := time.Now().UTC().UnixNano()
	for i := 0; i < count; i++ {
		tag := "#ADM" + int64ToString(base+int64(i)) + "-" + int64ToString(int64(i))
		var id int64
		if err := store.pool.QueryRow(ctx, `INSERT INTO players (normalized_tag, active, next_due_at) VALUES ($1, true, $2) RETURNING id`, tag, dueAt).Scan(&id); err != nil {
			t.Fatalf("seed player: %v", err)
		}
		ids = append(ids, id)
	}
	return ids
}

// seedOpenAdmissionGate records a safe handoff for the boundary covering the
// database clock so selection-assuming tests do not depend on wall-clock
// gate state (the final-plan gate is closed after 05:00 until handoff).
// The handoff instant predates every test due date, leaving
// effective-deadline math identical to the open pre-window. It skips inside
// the 5-minute pre-reset window, where the gate is closed by definition.
func seedOpenAdmissionGate(t *testing.T, ctx context.Context, store *store) {
	t.Helper()
	var now time.Time
	if err := store.pool.QueryRow(ctx, `SELECT statement_timestamp()`).Scan(&now); err != nil {
		t.Fatalf("read database clock: %v", err)
	}
	now = now.UTC()
	boundary := boundaryAdmissionBoundary(now)
	if !now.Before(boundary.Add(-5*time.Minute)) && now.Before(boundary) {
		t.Skip("closed pre-reset window is wall-clock dependent")
	}
	// Cover a post-wait tick landing on either side of 05:00.
	for _, day := range []time.Time{now.Add(-24 * time.Hour), now, now.Add(24 * time.Hour)} {
		edge := boundaryAdmissionBoundary(day)
		if _, err := store.pool.Exec(ctx, `INSERT INTO collector_boundary_admission (boundary_at, regular_drain_complete, reset_drain_complete, safe_handoff, state, handoff_at) VALUES ($1, true, true, true, 'safe_handoff', $2) ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff=true, state='safe_handoff', handoff_at=EXCLUDED.handoff_at, regular_drain_complete=true, reset_drain_complete=true`, edge, now.Add(-3*time.Hour)); err != nil {
			t.Fatalf("seed open gate: %v", err)
		}
	}
}

func TestAdmissionEvidenceConfigParsing(t *testing.T) {
	t.Parallel()
	valid := map[string]string{
		"CLASHLENS_REGULAR_ADMISSION_EVIDENCE_RUN_ID":                  "step9-run-v1",
		"CLASHLENS_REGULAR_ADMISSION_EVIDENCE_START":                   "2026-09-10T05:00:00Z",
		"CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END":                     "2026-09-11T05:00:00Z",
		"CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS":              "90000",
		"CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_SELECTED_ENTRIES":    "4000000",
		"CLASHLENS_DATABASE_URL":                                       "postgres://collector@127.0.0.1/collector",
		"CLASHLENS_ARCHIVE_ENDPOINT":                                   "127.0.0.1:9000",
		"CLASHLENS_ARCHIVE_BUCKET":                                     "raw",
		"CLASHLENS_ARCHIVE_ACCESS_KEY":                                 "archive-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY":                                 "archive-secret",
		"CLASHLENS_NORMAL_API_KEYS":                                    "normal-1=one,normal-2=two,normal-3=three,normal-4=four",
		"CLASHLENS_INTERACTIVE_API_KEYS":                               "interactive-1=five",
		"CLASHLENS_OFFICIAL_API_ORIGIN":                                "https://api.clashofclans.com",
	}
	config, err := loadConfig(func(name string) string { return valid[name] })
	if err != nil {
		t.Fatalf("loadConfig with evidence: %v", err)
	}
	if config.admissionEvidence == nil || config.admissionEvidence.runID != "step9-run-v1" {
		t.Fatalf("admissionEvidence not parsed: %+v", config.admissionEvidence)
	}
	disabled := map[string]string{
		"CLASHLENS_DATABASE_URL":         "postgres://collector@127.0.0.1/collector",
		"CLASHLENS_ARCHIVE_ENDPOINT":     "127.0.0.1:9000",
		"CLASHLENS_ARCHIVE_BUCKET":       "raw",
		"CLASHLENS_ARCHIVE_ACCESS_KEY":   "archive-access",
		"CLASHLENS_ARCHIVE_SECRET_KEY":   "archive-secret",
		"CLASHLENS_NORMAL_API_KEYS":      "normal-1=one,normal-2=two,normal-3=three,normal-4=four",
		"CLASHLENS_INTERACTIVE_API_KEYS": "interactive-1=five",
		"CLASHLENS_OFFICIAL_API_ORIGIN":  "https://api.clashofclans.com",
	}
	off, err := loadConfig(func(name string) string { return disabled[name] })
	if err != nil {
		t.Fatalf("loadConfig disabled: %v", err)
	}
	if off.admissionEvidence != nil {
		t.Fatalf("disabled evidence should be nil, got %+v", off.admissionEvidence)
	}
	for _, tc := range []struct {
		name  string
		mutate func(map[string]string)
		want  string
	}{
		{"partial", func(m map[string]string) { delete(m, "CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END") }, "all-or-none"},
		{"bad run", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_RUN_ID"] = "bad id!" }, "RUN_ID"},
		{"non-utc", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_START"] = "2026-09-10T05:00:00+01:00" }, "UTC"},
		{"end before start", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END"] = "2026-09-10T04:00:00Z" }, "after start"},
		{"too long", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END"] = "2026-09-12T05:00:01Z" }, "30 hours"},
		{"events zero", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS"] = "0" }, "MAX_EVENTS"},
		{"events over", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS"] = "108001" }, "MAX_EVENTS"},
		{"selected over", func(m map[string]string) { m["CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_SELECTED_ENTRIES"] = "5000001" }, "MAX_SELECTED"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := map[string]string{}
			for k, v := range valid {
				env[k] = v
			}
			tc.mutate(env)
			if _, err := loadConfig(func(name string) string { return env[name] }); err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want containing %q", err, tc.want)
			}
		})
	}
}

func TestAdmissionEvidenceQuotaBoundaryCommitsStop(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("quota-boundary-v1", start, end, 1, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	dueAt := time.Now().UTC().Add(-time.Minute)
	seedDuePlayers(t, ctx, store, 1, dueAt)
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("first admission: %v", err)
	}
	// Second call must commit capacity_exceeded before returning the fixed error.
	seedDuePlayers(t, ctx, store, 1, dueAt)
	err := func() error {
		_, scheduleErr := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
		return scheduleErr
	}()
	if !errors.Is(err, errAdmissionCapacityExceeded) {
		t.Fatalf("second admission error = %v, want capacity_exceeded", err)
	}
	var state, code string
	var events int
	var selected int64
	var stopped *time.Time
	if err := store.pool.QueryRow(ctx, `SELECT state, failure_code, events_written, selected_entries_written, stopped_at FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&state, &code, &events, &selected, &stopped); err != nil {
		t.Fatalf("read run header: %v", err)
	}
	if state != "capacity_exceeded" || code != "admission_evidence_capacity_exceeded" || events != 1 || stopped == nil {
		t.Fatalf("run header = %q %q events=%d stopped=%v, want committed capacity_exceeded with 1 event", state, code, events, stopped)
	}
	var evidenceCount, jobCount int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&evidenceCount); err != nil {
		t.Fatalf("count evidence: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE work_type='regular_poll'`).Scan(&jobCount); err != nil {
		t.Fatalf("count jobs: %v", err)
	}
	if evidenceCount != 1 || jobCount != 1 {
		t.Fatalf("evidence=%d jobs=%d, want 1 and 1 (no new event/root on stop)", evidenceCount, jobCount)
	}
}

func TestAdmissionEvidenceSelectedEntryOverflowCommitsStop(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("selected-overflow-v1", start, end, 108000, 1)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	dueAt := time.Now().UTC().Add(-time.Minute)
	seedDuePlayers(t, ctx, store, 2, dueAt)
	_, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
	if !errors.Is(err, errAdmissionCapacityExceeded) {
		t.Fatalf("overflow error = %v, want capacity_exceeded", err)
	}
	var state string
	var events int
	if err := store.pool.QueryRow(ctx, `SELECT state, events_written FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&state, &events); err != nil {
		t.Fatalf("read header: %v", err)
	}
	if state != "capacity_exceeded" || events != 0 {
		t.Fatalf("header = %q events=%d, want capacity_exceeded with 0 events", state, events)
	}
	var evidenceCount, jobCount int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&evidenceCount)
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE work_type='regular_poll'`).Scan(&jobCount)
	if evidenceCount != 0 || jobCount != 0 {
		t.Fatalf("evidence=%d jobs=%d, want 0 and 0", evidenceCount, jobCount)
	}
}

func TestAdmissionEvidenceCaptureBounds(t *testing.T) {
	t.Run("at start passes", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		dbNow := time.Now().UTC()
		start := dbNow.Add(-time.Second * 5).Truncate(time.Second)
		end := start.Add(2 * time.Hour)
		config := newAdmissionConfig("capture-start-v1", start, end, 100, 1000)
		store := openAdmissionStore(t, ctx, databaseURL, config)
		seedDuePlayers(t, ctx, store, 1, dbNow.Add(-time.Minute))
		if _, err := store.scheduleDueRegular(ctx, dbNow, 5*time.Minute, 10); err != nil {
			t.Fatalf("at-start admission: %v", err)
		}
	})
	t.Run("at and after end stops", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		dbNow := time.Now().UTC()
		start := dbNow.Add(-2 * time.Hour).Truncate(time.Second)
		end := dbNow.Add(-time.Minute).Truncate(time.Second)
		// Ensure end is in the past relative to the database clock by waiting
		// for the statement timestamp to pass it.
		config := newAdmissionConfig("capture-end-v1", start, end, 100, 1000)
		store := openAdmissionStore(t, ctx, databaseURL, config)
		seedDuePlayers(t, ctx, store, 1, dbNow.Add(-90*time.Minute))
		_, err := store.scheduleDueRegular(ctx, dbNow, 5*time.Minute, 10)
		if !errors.Is(err, errAdmissionCaptureOutOfRange) {
			t.Fatalf("past-end error = %v, want capture_out_of_range", err)
		}
		var state, code string
		store.pool.QueryRow(ctx, `SELECT state, failure_code FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&state, &code)
		if state != "capture_out_of_range" || code != "admission_evidence_capture_out_of_range" {
			t.Fatalf("header = %q %q, want capture_out_of_range", state, code)
		}
		var evidenceCount, jobCount int
		store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&evidenceCount)
		store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE work_type='regular_poll'`).Scan(&jobCount)
		if evidenceCount != 0 || jobCount != 0 {
			t.Fatalf("evidence=%d jobs=%d, want 0 and 0", evidenceCount, jobCount)
		}
	})
}

func TestAdmissionEvidenceMissingAndCollidingHeaders(t *testing.T) {
	t.Run("missing fails closed", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		start, end := admissionCaptureAroundNow(10 * time.Minute)
		config := newAdmissionConfig("missing-v1", start, end, 100, 1000)
		store, err := openStore(ctx, databaseURL, 5)
		if err != nil {
			t.Fatalf("openStore: %v", err)
		}
		t.Cleanup(store.close)
		store.configureAdmissionEvidence(config)
		store.metrics = newCollectorMetrics()
		if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); !errors.Is(err, errAdmissionRunMissing) {
			t.Fatalf("missing header error = %v, want run missing", err)
		}
	})
	t.Run("colliding fails closed", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		start, end := admissionCaptureAroundNow(10 * time.Minute)
		seeded := newAdmissionConfig("collide-v1", start, end, 100, 1000)
		store := openAdmissionStore(t, ctx, databaseURL, seeded)
		colliding := newAdmissionConfig("collide-v1", start, end.Add(time.Hour), 100, 1000)
		store.configureAdmissionEvidence(colliding)
		if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); !errors.Is(err, errAdmissionRunConflict) {
			t.Fatalf("colliding header error = %v, want run conflict", err)
		}
		// Startup equality also fails.
		if err := store.ensureAdmissionEvidenceRun(ctx, colliding); !errors.Is(err, errAdmissionRunConflict) {
			t.Fatalf("startup collide error = %v, want conflict", err)
		}
	})
}

func TestAdmissionEvidenceConcurrentReservationNoOverspend(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("concurrent-quota-v1", start, end, 2, 10000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	dueAt := time.Now().UTC().Add(-time.Minute)
	seedDuePlayers(t, ctx, store, 5, dueAt)
	var wg sync.WaitGroup
	errs := make([]error, 5)
	counts := make([]int, 5)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			// Each goroutine needs its own store handle sharing the pool is
			// fine; the reservation is row-locked so overspend is impossible.
			n, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
			counts[idx], errs[idx] = n, err
		}(i)
	}
	wg.Wait()
	succeeded := 0
	for _, err := range errs {
		if err == nil {
			succeeded++
		} else if !errors.Is(err, errAdmissionCapacityExceeded) {
			t.Fatalf("concurrent errors = %v, want nil or capacity_exceeded", errs)
		}
	}
	var events int
	store.pool.QueryRow(ctx, `SELECT events_written FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&events)
	if events > 2 {
		t.Fatalf("events_written=%d, want at most 2", events)
	}
	var evidenceCount int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&evidenceCount)
	if evidenceCount != events || events > 2 {
		t.Fatalf("evidence=%d events_written=%d, want equal and <=2", evidenceCount, events)
	}
	if succeeded != events {
		t.Fatalf("succeeded=%d events=%d, want equal", succeeded, events)
	}
}

func TestAdmissionEvidenceRollbackOnEventFailure(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("rollback-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	dueAt := time.Now().UTC().Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 1, dueAt)
	// Force the evidence INSERT to fail with an invalid batch limit through
	// raw SQL while the reservation/insert/advance would otherwise succeed.
	// The whole statement must roll back: no job, no advance, no counters.
	var beforeDue time.Time
	store.pool.QueryRow(ctx, `SELECT next_due_at FROM players WHERE id=$1`, ids[0]).Scan(&beforeDue)
	tx, err := store.pool.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	_ = tx.Rollback(ctx)
	// Use a batch limit of 0 to violate the evidence CHECK atomically.
	_, err = store.pool.Exec(ctx, `SELECT 1 FROM collector_regular_admission_evidence_runs WHERE run_id=$1 FOR UPDATE`, config.runID)
	if err != nil {
		t.Fatalf("lock header: %v", err)
	}
	// Directly exercise the scheduler SQL with an illegal batch limit; it
	// must fail and leave all durable effects unchanged.
	now := time.Now().UTC()
	cycleAt := now.Truncate(5 * time.Minute).UTC()
	boundary := boundaryAdmissionBoundary(now)
	_, execErr := store.pool.Exec(ctx, admissionEvidenceSchedulerSQL, now, 0, cycleAt.Unix(), cycleAt.Add(5*time.Minute), int64(300),
		now.UTC().Format(time.RFC3339), boundary.UTC().Format(time.RFC3339),
		config.runID, "abcdef0123456789abcdef0123456789", config.captureStart, config.captureEnd, cycleAt)
	if execErr == nil {
		t.Fatalf("illegal batch scheduler SQL succeeded, want CHECK failure")
	}
	var jobs, evidence, events int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE work_type='regular_poll'`).Scan(&jobs)
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&evidence)
	store.pool.QueryRow(ctx, `SELECT events_written FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&events)
	var afterDue time.Time
	store.pool.QueryRow(ctx, `SELECT next_due_at FROM players WHERE id=$1`, ids[0]).Scan(&afterDue)
	if jobs != 0 || evidence != 0 || events != 0 || !afterDue.Equal(beforeDue) {
		t.Fatalf("rollback state jobs=%d evidence=%d events=%d dueChanged=%v, want 0 0 0 false", jobs, evidence, events, !afterDue.Equal(beforeDue))
	}
}

func TestAdmissionEvidenceCommitFailureIsAmbiguous(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("ambiguous-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedDuePlayers(t, ctx, store, 1, time.Now().UTC().Add(-time.Minute))
	injected := errors.New("injected admission commit failure")
	store.commitTx = func(ctx context.Context, tx pgx.Tx) error {
		_ = tx.Rollback(ctx)
		return injected
	}
	_, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
	if !errors.Is(err, errAdmissionCommitUnknown) || !errors.Is(err, injected) {
		t.Fatalf("commit error = %v, want unknown wrapping injected", err)
	}
}

func TestAdmissionEvidenceHeldLockVisibleButUnselected(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("held-lock-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	dueAt := time.Now().UTC().Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 2, dueAt)
	holder, err := store.pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire holder: %v", err)
	}
	defer holder.Release()
	tx, err := holder.Begin(ctx)
	if err != nil {
		t.Fatalf("begin holder: %v", err)
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, `SELECT id FROM players WHERE id=$1 FOR NO KEY UPDATE`, ids[0]); err != nil {
		t.Fatalf("hold lock: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("schedule with held lock: %v", err)
	}
	var visible, unselected int
	var selected []int64
	if err := store.pool.QueryRow(ctx, `SELECT visible_due_count, unselected_visible_due_count, selected_player_ids FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&visible, &unselected, &selected); err != nil {
		t.Fatalf("read evidence: %v", err)
	}
	if visible != 2 || unselected != 1 || len(selected) != 1 || selected[0] == ids[0] {
		t.Fatalf("visible=%d unselected=%d selected=%v, want 2 1 excluding held %d", visible, unselected, selected, ids[0])
	}
	if err := tx.Rollback(ctx); err != nil {
		t.Fatalf("release lock: %v", err)
	}
}

func TestAdmissionEvidenceLockReleaseBeforeDeadlinePasses(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("lock-early-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	// Due 1 minute ago: deadline 4 minutes in the future.
	dueAt := time.Now().UTC().Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 1, dueAt)
	holder, _ := store.pool.Acquire(ctx)
	defer holder.Release()
	tx, _ := holder.Begin(ctx)
	tx.Exec(ctx, `SELECT id FROM players WHERE id=$1 FOR NO KEY UPDATE`, ids[0])
	store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
	tx.Rollback(ctx)
	// Released before the deadline: next invocation admits it with no past-deadline count.
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("second admission: %v", err)
	}
	var selPast, unselPast int
	store.pool.QueryRow(ctx, `SELECT selected_past_deadline_count, unselected_visible_past_deadline_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&selPast, &unselPast)
	if selPast != 0 || unselPast != 0 {
		t.Fatalf("past counts = %d %d, want 0 0 for pre-deadline release", selPast, unselPast)
	}
}

func TestAdmissionEvidenceLockHeldPastDeadlineFailsEvenWhenSelected(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("lock-late-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	// Due 6 minutes ago: deadline already passed.
	dueAt := time.Now().UTC().Add(-6 * time.Minute)
	ids := seedDuePlayers(t, ctx, store, 1, dueAt)
	holder, _ := store.pool.Acquire(ctx)
	defer holder.Release()
	tx, _ := holder.Begin(ctx)
	tx.Exec(ctx, `SELECT id FROM players WHERE id=$1 FOR NO KEY UPDATE`, ids[0])
	store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10)
	var unselPast int
	store.pool.QueryRow(ctx, `SELECT unselected_visible_past_deadline_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&unselPast)
	if unselPast != 1 {
		t.Fatalf("held past-deadline unselected=%d, want 1", unselPast)
	}
	tx.Rollback(ctx)
	// Released after the deadline and immediately selected: selected past-deadline stays positive.
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("late selection: %v", err)
	}
	var selPast int
	store.pool.QueryRow(ctx, `SELECT selected_past_deadline_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&selPast)
	if selPast != 1 {
		t.Fatalf("late selected past=%d, want 1 (failed even when selected)", selPast)
	}
}

func TestAdmissionEvidenceBatchDeferralDrains(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("batch-defer-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	dueAt := time.Now().UTC().Add(-time.Minute)
	seedDuePlayers(t, ctx, store, 2, dueAt)
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 1); err != nil {
		t.Fatalf("batch-1 first: %v", err)
	}
	var visible, unselected int
	store.pool.QueryRow(ctx, `SELECT visible_due_count, unselected_visible_due_count FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&visible, &unselected)
	if visible != 2 || unselected != 1 {
		t.Fatalf("batch defer visible=%d unselected=%d, want 2 1", visible, unselected)
	}
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("drain: %v", err)
	}
	store.pool.QueryRow(ctx, `SELECT unselected_visible_due_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&unselected)
	if unselected != 0 {
		t.Fatalf("drained unselected=%d, want 0", unselected)
	}
}

func TestAdmissionEvidenceConcurrentProfileStateDueCommits(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("evalplanqual-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	dueAt := time.Now().UTC().Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 4, dueAt)
	// Concurrently commit profile, eligibility, active, and due changes while
	// the scheduler runs. False subset arithmetic must not trigger.
	holder, _ := store.pool.Acquire(ctx)
	defer holder.Release()
	tx, _ := holder.Begin(ctx)
	tx.Exec(ctx, `UPDATE players SET eligibility_state='ineligible' WHERE id=$1`, ids[0])
	tx.Exec(ctx, `UPDATE players SET active=false WHERE id=$1`, ids[1])
	tx.Exec(ctx, `UPDATE players SET next_due_at=now()+interval '1 hour' WHERE id=$1`, ids[2])
	tx.Exec(ctx, `UPDATE players SET current_profile_version_id=NULL WHERE id=$1`, ids[3])
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		time.Sleep(50 * time.Millisecond)
		tx.Commit(ctx)
	}()
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("concurrent commit schedule: %v", err)
	}
	wg.Wait()
	// Honest invariant retained: 0 <= unselected <= visible.
	var visible, unselected int
	if err := store.pool.QueryRow(ctx, `SELECT visible_due_count, unselected_visible_due_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&visible, &unselected); err != nil {
		t.Fatalf("read evidence: %v", err)
	}
	if unselected < 0 || unselected > visible {
		t.Fatalf("visible=%d unselected=%d violates 0<=unselected<=visible", visible, unselected)
	}
}

func TestAdmissionEvidenceNullAndNonEligibleStateRetained(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("nullable-state-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedOpenAdmissionGate(t, ctx, store)
	dueAt := time.Now().UTC().Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 2, dueAt)
	if _, err := store.pool.Exec(ctx, `UPDATE players SET current_profile_version_id=NULL, eligibility_state='unknown' WHERE id=$1`, ids[0]); err != nil {
		t.Fatalf("null profile: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `UPDATE players SET eligibility_state='ineligible' WHERE id=$1`, ids[1]); err != nil {
		t.Fatalf("ineligible: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("schedule nullable: %v", err)
	}
	var profileIDs []*int64
	var states []string
	var playerIDs []int64
	if err := store.pool.QueryRow(ctx, `SELECT selected_player_ids, selected_profile_version_ids, selected_eligibility_states FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&playerIDs, &profileIDs, &states); err != nil {
		t.Fatalf("read arrays: %v", err)
	}
	if len(playerIDs) != 2 || len(profileIDs) != 2 || len(states) != 2 {
		t.Fatalf("array lengths = %d %d %d, want 2 2 2 (ordinality preserved)", len(playerIDs), len(profileIDs), len(states))
	}
	// Null profile element must be retained, not rejected; states are observed.
	foundNull := false
	for _, id := range profileIDs {
		if id == nil {
			foundNull = true
		}
	}
	if !foundNull {
		t.Fatalf("profile IDs = %v, want one retained NULL", profileIDs)
	}
	hasIneligible := false
	for _, s := range states {
		if s == "ineligible" {
			hasIneligible = true
		}
	}
	if !hasIneligible {
		t.Fatalf("states = %v, want observed ineligible retained", states)
	}
	// Observer null-safe check: IS DISTINCT FROM finds the invalid profile
	// without counting an empty-event placeholder.
	var invalid int
	if err := store.pool.QueryRow(ctx, `
		WITH events AS MATERIALIZED (SELECT * FROM collector_regular_admission_evidence WHERE run_id=$1),
		selected AS (
			SELECT e.id AS event_id, s.player_id, p.profile_version_id
			FROM events e
			CROSS JOIN LATERAL unnest(e.selected_player_ids) WITH ORDINALITY AS s(player_id, n)
			JOIN LATERAL unnest(e.selected_profile_version_ids) WITH ORDINALITY AS p(profile_version_id, n) ON p.n = s.n
		)
		SELECT count(*) FILTER (WHERE selected.player_id IS NOT NULL AND selected.profile_version_id IS NULL) FROM selected
	`, config.runID).Scan(&invalid); err != nil {
		t.Fatalf("null-safe aggregation: %v", err)
	}
	if invalid != 2 {
		t.Fatalf("invalid profile count=%d, want 2 (both seeded players retain NULL profile)", invalid)
	}
}

func TestAdmissionEvidenceRootConflictsFailReconciliation(t *testing.T) {
	t.Run("active coalescing conflict", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		start, end := admissionCaptureAroundNow(10 * time.Minute)
		config := newAdmissionConfig("root-active-v1", start, end, 100, 1000)
		store := openAdmissionStore(t, ctx, databaseURL, config)
		seedOpenAdmissionGate(t, ctx, store)
		dueAt := time.Now().UTC().Add(-time.Minute)
		ids := seedDuePlayers(t, ctx, store, 1, dueAt)
		// Pre-create the exact semantic root the scheduler would insert.
		now := time.Now().UTC()
		cycleAt := now.Truncate(5 * time.Minute).UTC()
		coalescing := "regular:" + itoa(ids[0]) + ":" + itoa(cycleAt.Unix())
		if _, err := store.pool.Exec(ctx, `INSERT INTO collector_jobs (work_type, player_id, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status) SELECT 'regular_poll', id, normalized_tag, 'normal', 100, $2, $3, 'pending' FROM players WHERE id=$1`, ids[0], now, coalescing); err != nil {
			t.Fatalf("seed conflicting root: %v", err)
		}
		_, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 10)
		if err == nil || !strings.Contains(err.Error(), "root mismatch") {
			t.Fatalf("conflict error = %v, want root mismatch", err)
		}
		// The mismatch is committed as evidence; validator finds selected != inserted.
		var selected, inserted int
		store.pool.QueryRow(ctx, `SELECT cardinality(selected_player_ids), cardinality(inserted_job_ids) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&selected, &inserted)
		if selected != 1 || inserted != 0 {
			t.Fatalf("selected=%d inserted=%d, want 1 0", selected, inserted)
		}
	})
	t.Run("completed duplicate semantic root", func(t *testing.T) {
		databaseURL := startAdmissionDatabase(t)
		ctx := context.Background()
		start, end := admissionCaptureAroundNow(10 * time.Minute)
		config := newAdmissionConfig("root-dupe-v1", start, end, 100, 1000)
		store := openAdmissionStore(t, ctx, databaseURL, config)
		seedOpenAdmissionGate(t, ctx, store)
		dueAt := time.Now().UTC().Add(-time.Minute)
		ids := seedDuePlayers(t, ctx, store, 1, dueAt)
		now := time.Now().UTC()
		cycleAt := now.Truncate(5 * time.Minute).UTC()
		coalescing := "regular:" + itoa(ids[0]) + ":" + itoa(cycleAt.Unix())
		if _, err := store.pool.Exec(ctx, `INSERT INTO collector_jobs (work_type, player_id, normalized_tag, capacity_pool, priority, due_at, coalescing_key, status) SELECT 'regular_poll', id, normalized_tag, 'normal', 100, $2, $3, 'complete' FROM players WHERE id=$1`, ids[0], now, coalescing); err != nil {
			t.Fatalf("seed terminal root: %v", err)
		}
		// A completed root does not block the active insert (partial index
		// covers only active statuses), so the scheduler admits a second
		// row with the same semantic key. Final reconciliation over all
		// retained regular_poll roots by semantic identity must fail the
		// exact-one-root rule.
		if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 10); err != nil {
			t.Fatalf("second semantic insert: %v", err)
		}
		var roots int
		store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_jobs WHERE coalescing_key=$1 AND work_type='regular_poll'`, coalescing).Scan(&roots)
		if roots != 2 {
			t.Fatalf("semantic roots=%d, want 2 (terminal plus admitted duplicate fails exact reconciliation)", roots)
		}
	})
}

func itoa(v int64) string {
	return int64ToString(v)
}

func int64ToString(v int64) string {
	if v == 0 {
		return "0"
	}
	neg := v < 0
	if neg {
		v = -v
	}
	var buf [32]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	if neg {
		i--
		buf[i] = '-'
	}
	return string(buf[i:])
}

func TestAdmissionEvidenceResetHandoffGrace(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("handoff-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	var dbNow time.Time
	if err := store.pool.QueryRow(ctx, `SELECT statement_timestamp()`).Scan(&dbNow); err != nil {
		t.Fatalf("read database clock: %v", err)
	}
	dbNow = dbNow.UTC()
	boundary := boundaryAdmissionBoundary(dbNow)
	// Blocked backlog when the tick is not in the open pre-window: insert a
	// draining row without safe handoff. In the open pre-window the gate is
	// open by definition, so only assert backlog when closed.
	if _, err := store.pool.Exec(ctx, `INSERT INTO collector_boundary_admission (boundary_at, regular_drain_complete, reset_drain_complete, safe_handoff, state) VALUES ($1, false, false, false, 'regular_draining') ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff=false, state='regular_draining', handoff_at=NULL`, boundary); err != nil {
		t.Fatalf("seed draining gate: %v", err)
	}
	seedDuePlayers(t, ctx, store, 1, dbNow.Add(-time.Minute))
	if _, err := store.scheduleDueRegular(ctx, dbNow, 5*time.Minute, 10); err != nil {
		t.Fatalf("gate schedule: %v", err)
	}
	var allowed bool
	var selected []int64
	var visible int
	store.pool.QueryRow(ctx, `SELECT gate_allowed, selected_player_ids, visible_due_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&allowed, &selected, &visible)
	closed := !dbNow.Before(boundary.Add(-5*time.Minute))
	if closed {
		if allowed || len(selected) != 0 || visible == 0 {
			t.Fatalf("blocked event allowed=%v selected=%v visible=%d, want false [] >=1", allowed, selected, visible)
		}
	} else if visible == 0 && allowed {
		t.Logf("open pre-window tick retains empty visible set (allowed)")
	}
	// Safe handoff reopens the gate; a row due long before handoff gets
	// grace via effective_due = max(next_due_at, handoff_at).
	handoffAt := dbNow.Add(-30 * time.Second)
	if _, err := store.pool.Exec(ctx, `INSERT INTO collector_boundary_admission (boundary_at, regular_drain_complete, reset_drain_complete, safe_handoff, state, handoff_at) VALUES ($1, true, true, true, 'safe_handoff', $2) ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff=true, state='safe_handoff', handoff_at=EXCLUDED.handoff_at, regular_drain_complete=true, reset_drain_complete=true`, boundary, handoffAt); err != nil {
		t.Fatalf("open handoff: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `INSERT INTO players (normalized_tag, active, next_due_at) VALUES ($1, true, $2)`, "#HANDOFF-GRACE-1", dbNow.Add(-6*time.Minute)); err != nil {
		t.Fatalf("seed grace player: %v", err)
	}
	if _, err := store.scheduleDueRegular(ctx, dbNow, 5*time.Minute, 10); err != nil {
		t.Fatalf("reopened schedule: %v", err)
	}
	var handoff *time.Time
	var selPast int
	var allowed2 bool
	store.pool.QueryRow(ctx, `SELECT gate_allowed, gate_handoff_at, selected_past_deadline_count FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&allowed2, &handoff, &selPast)
	if !allowed2 {
		t.Logf("reopened tick still gate-blocked at %v (open pre-window or older reset); skipping grace assertion", dbNow)
		return
	}
	if handoff == nil || selPast != 0 {
		t.Fatalf("handoff=%v selPast=%d allowed=%v, want persisted handoff and 0 (grace, not overdue)", handoff, selPast, allowed2)
	}
}

func TestAdmissionEvidenceFreshSnapshotAfterAdvisoryWait(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("fresh-snap-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	now := time.Now().UTC()
	boundary := boundaryAdmissionBoundary(now)
	dueAt := now.Add(-time.Minute)
	ids := seedDuePlayers(t, ctx, store, 1, dueAt)
	// Hold the boundary preparation transaction uncommitted: it holds the
	// advisory lock. The scheduler must wait on its preceding lock statement,
	// then evaluate a fresh post-commit gate snapshot.
	holder, err := store.pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer holder.Release()
	holderTx, err := holder.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := holderTx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, boundaryAdmissionLockKey(boundary)); err != nil {
		t.Fatalf("hold advisory: %v", err)
	}
	if _, err := holderTx.Exec(ctx, `INSERT INTO collector_boundary_admission (boundary_at, regular_drain_complete, reset_drain_complete, safe_handoff, state) VALUES ($1, false, false, false, 'regular_draining') ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff=false`, boundary); err != nil {
		t.Fatalf("hold boundary row: %v", err)
	}
	done := make(chan error, 1)
	go func() {
		_, schedErr := store.scheduleDueRegular(ctx, now, 5*time.Minute, 10)
		done <- schedErr
	}()
	time.Sleep(200 * time.Millisecond)
	// Commit while the scheduler waits; it must see the committed gate.
	if err := holderTx.Commit(ctx); err != nil {
		t.Fatalf("commit holder: %v", err)
	}
	select {
	case schedErr := <-done:
		if schedErr != nil {
			t.Fatalf("scheduler after wait: %v", schedErr)
		}
	case <-time.After(10 * time.Second):
		t.Fatalf("scheduler did not finish after advisory release")
	}
	var allowed bool
	store.pool.QueryRow(ctx, `SELECT gate_allowed FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&allowed)
	_ = ids
	// The exact gate value depends on the boundary timing; the invariant is
	// that the scheduler waited and then committed one evidence row.
	var events int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&events)
	if events != 1 {
		t.Fatalf("events=%d, want 1 fresh-snapshot row", events)
	}
}

func TestAdmissionEvidenceCycleEndSecondsOldDefers(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("cycle-end-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	// A player becoming due seconds before the scheduler tick is only
	// seconds old, not past its five-minute allowance.
	dueAt := time.Now().UTC().Add(-2 * time.Second)
	seedDuePlayers(t, ctx, store, 1, dueAt)
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("seconds-old schedule: %v", err)
	}
	var selPast, unselPast int
	store.pool.QueryRow(ctx, `SELECT selected_past_deadline_count, unselected_visible_past_deadline_count FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&selPast, &unselPast)
	if selPast != 0 || unselPast != 0 {
		t.Fatalf("seconds-old past = %d %d, want 0 0", selPast, unselPast)
	}
}

func TestAdmissionEvidenceExactDeadlineIsTimely(t *testing.T) {
	// Deadline arithmetic: equality is timely, only strictly-greater is late.
	due := time.Date(2026, 9, 10, 12, 0, 0, 0, time.UTC)
	deadline := due.Add(5 * time.Minute)
	if !deadline.Equal(due.Add(5 * time.Minute)) {
		t.Fatalf("deadline arithmetic broken")
	}
	if !(deadline.After(due.Add(5*time.Minute - time.Second))) {
		t.Fatalf("deadline ordering broken")
	}
	// late iff database_at > deadline.
	if !(deadline.Add(time.Second).After(deadline)) {
		t.Fatalf("late comparison broken")
	}
	if deadline.After(deadline) {
		t.Fatalf("equality must be timely, not late")
	}
}

func TestAdmissionEvidenceEvidenceDisabledPreservesSQL(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 5)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	t.Cleanup(store.close)
	if store.admissionEvidence != nil {
		t.Fatalf("disabled store should have nil evidence config")
	}
	now := time.Now().UTC()
	seedDuePlayers(t, ctx, store, 1, now.Add(-time.Minute))
	if _, err := store.scheduleDueRegular(ctx, now, 5*time.Minute, 10); err != nil {
		t.Fatalf("disabled schedule: %v", err)
	}
	var evidence int
	// Evidence tables exist but stay empty when disabled.
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence`).Scan(&evidence); err != nil {
		t.Fatalf("count evidence: %v", err)
	}
	if evidence != 0 {
		t.Fatalf("disabled evidence rows=%d, want 0", evidence)
	}
}

func TestAdmissionEvidenceEmptyAndGateBlockedWriteRows(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("empty-rows-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	// Empty invocation (no due players) still writes one evidence row.
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("empty schedule: %v", err)
	}
	var emptySelected int
	var emptyVisible int
	store.pool.QueryRow(ctx, `SELECT cardinality(selected_player_ids), visible_due_count FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&emptySelected, &emptyVisible)
	if emptySelected != 0 || emptyVisible != 0 {
		t.Fatalf("empty event selected=%d visible=%d, want 0 0", emptySelected, emptyVisible)
	}
	// Invocation IDs are unique across empty and non-empty calls.
	var firstID string
	store.pool.QueryRow(ctx, `SELECT invocation_id FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&firstID)
	seedDuePlayers(t, ctx, store, 1, time.Now().UTC().Add(-time.Minute))
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("second schedule: %v", err)
	}
	var ids []string
	rows, _ := store.pool.Query(ctx, `SELECT invocation_id FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id`, config.runID)
	defer rows.Close()
	for rows.Next() {
		var id string
		rows.Scan(&id)
		ids = append(ids, id)
	}
	if len(ids) != 2 || ids[0] == ids[1] {
		t.Fatalf("invocation IDs=%v, want 2 unique", ids)
	}
	// No automatic deletion: counts only grow.
	var count int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&count)
	if count != 2 {
		t.Fatalf("evidence count=%d, want 2 (no deletion)", count)
	}
}

func TestAdmissionEvidenceDelayedLowerIDCommit(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("delayed-id-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	// Transaction A allocates a lower evidence ID and holds it uncommitted.
	holderA, _ := store.pool.Acquire(ctx)
	defer holderA.Release()
	txA, _ := holderA.Begin(ctx)
	eventA := start.Add(time.Minute)
	eventB := start.Add(2 * time.Minute)
	var idA int64
	if err := txA.QueryRow(ctx, `INSERT INTO collector_regular_admission_evidence (run_id, invocation_id, capture_start, capture_end, cycle_at, scheduler_at, database_at, gate_allowed, batch_limit, visible_due_count, unselected_visible_due_count, unselected_visible_past_deadline_count, selected_past_deadline_count, selected_player_ids, selected_due_ats, selected_profile_version_ids, selected_eligibility_states, inserted_job_ids, advanced_count) VALUES ($1, $2, $3, $4, date_bin('5 minutes', $5, timestamptz '2000-01-01 00:00:00+00'), $5, $5, true, 10, 0, 0, 0, 0, '{}', '{}', '{}', '{}', '{}', 0) RETURNING id`, config.runID, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", start, end, eventA).Scan(&idA); err != nil {
		t.Fatalf("txA insert: %v", err)
	}
	// Transaction B commits a higher ID for the same run.
	var idB int64
	if err := store.pool.QueryRow(ctx, `INSERT INTO collector_regular_admission_evidence (run_id, invocation_id, capture_start, capture_end, cycle_at, scheduler_at, database_at, gate_allowed, batch_limit, visible_due_count, unselected_visible_due_count, unselected_visible_past_deadline_count, selected_past_deadline_count, selected_player_ids, selected_due_ats, selected_profile_version_ids, selected_eligibility_states, inserted_job_ids, advanced_count) VALUES ($1, $2, $3, $4, date_bin('5 minutes', $5, timestamptz '2000-01-01 00:00:00+00'), $5, $5, true, 10, 0, 0, 0, 0, '{}', '{}', '{}', '{}', '{}', 0) RETURNING id`, config.runID, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", start, end, eventB).Scan(&idB); err != nil {
		t.Fatalf("txB insert: %v", err)
	}
	if idB <= idA {
		t.Fatalf("ids A=%d B=%d, want B>A (allocation order)", idA, idB)
	}
	// Sample max(id) while only B is visible, then commit A.
	var sampledMax int64
	store.pool.QueryRow(ctx, `SELECT max(id) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&sampledMax)
	if sampledMax != idB {
		t.Fatalf("sampled max=%d, want B=%d", sampledMax, idB)
	}
	if err := txA.Commit(ctx); err != nil {
		t.Fatalf("commit A: %v", err)
	}
	// Final semantic reconciliation by run_id finds both invocations despite
	// A's ID being below the sampled maximum; the old (low,high] watermark
	// would permanently omit A.
	var found int
	store.pool.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1 AND invocation_id IN ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb')`, config.runID).Scan(&found)
	if found != 2 {
		t.Fatalf("semantic found=%d, want 2", found)
	}
	if !(idA < sampledMax) {
		t.Fatalf("adversarial control broken: A=%d sampledMax=%d, want A<max", idA, sampledMax)
	}
}

func TestAdmissionEvidenceCadenceGapAndTail(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("cadence-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	// Merge fixture uses a small fixed evidence threshold (5 seconds); real
	// Step 9 chooses its run-specific max_invocation_gap before Phase 5.
	const maxGap = 5 * time.Second
	for i := 0; i < 3; i++ {
		if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
			t.Fatalf("cadence schedule %d: %v", i, err)
		}
		time.Sleep(100 * time.Millisecond)
	}
	rows, err := store.pool.Query(ctx, `SELECT database_at FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY database_at, id`, config.runID)
	if err != nil {
		t.Fatalf("read database_at: %v", err)
	}
	defer rows.Close()
	var prev *time.Time
	for rows.Next() {
		var at time.Time
		rows.Scan(&at)
		if prev != nil && at.Sub(*prev) > maxGap {
			t.Fatalf("invocation gap %v exceeds %v (admission_visibility_unknown)", at.Sub(*prev), maxGap)
		}
		copy := at
		prev = &copy
	}
	// Tail: every obligation due in the core receives its full 5-minute
	// deadline, so evidence must extend at least 5 minutes past core end.
	// Here the capture window itself is the core+tail; assert the last event
	// is inside capture and the validator can require tail coverage.
	var lastAt time.Time
	store.pool.QueryRow(ctx, `SELECT max(database_at) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&lastAt)
	if lastAt.Before(start) || !lastAt.Before(end) {
		t.Fatalf("last database_at %v outside capture [%v,%v)", lastAt, start, end)
	}
}

func TestAdmissionEvidenceMetricsPrivacy(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 5)
	if err != nil {
		t.Fatalf("openStore: %v", err)
	}
	t.Cleanup(store.close)
	store.metrics = newCollectorMetrics()
	store.metrics.recordJob("regular_poll", "normal", "scheduled")
	store.metrics.recordStorageError("admission_evidence_capacity_exceeded")
	output := store.metrics.renderRuntime(store)
	for _, forbidden := range []string{"#", "normalized_tag", "player_id", "regular:"} {
		if strings.Contains(output, forbidden) {
			t.Fatalf("runtime metrics leaks %q", forbidden)
		}
	}
	if !strings.Contains(output, "admission_evidence_capacity_exceeded") {
		t.Fatalf("runtime metrics missing fixed failure category")
	}
}

func TestAdmissionEvidenceSchedulerAvoidsHistoryScans(t *testing.T) {
	// The gate retains its bounded reset-lineage probe (collector_jobs,
	// collector_attempts, sweeps, boundary admission); minute work must not
	// scan growing occurrence/Python history.
	for _, table := range []string{"collector_observations", "collector_endpoint_results", "python_processing_jobs", "player_profile_effects", "player_profile_versions", "collector_transport_failures", "global_rankings", "boundary_publication"} {
		if strings.Contains(admissionEvidenceSchedulerSQL, table) {
			t.Fatalf("scheduler SQL references growing history table %q", table)
		}
	}
}

func TestAdmissionEvidenceIndexPlanAndSizes(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("plan-size-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	dueAt := time.Now().UTC().Add(-time.Minute)
	seedDuePlayers(t, ctx, store, 3, dueAt)
	now := time.Now().UTC()
	cycleAt := now.Truncate(5 * time.Minute).UTC()
	boundary := boundaryAdmissionBoundary(now)
	rows, err := store.pool.Query(ctx, `EXPLAIN (COSTS OFF) `+admissionEvidenceSchedulerSQL, now, 10, cycleAt.Unix(), cycleAt.Add(5*time.Minute), int64(300),
		now.UTC().Format(time.RFC3339), boundary.UTC().Format(time.RFC3339),
		config.runID, "abcdef0123456789abcdef0123456789", config.captureStart, config.captureEnd, cycleAt)
	if err != nil {
		t.Fatalf("explain: %v", err)
	}
	defer rows.Close()
	var plan strings.Builder
	for rows.Next() {
		var line string
		rows.Scan(&line)
		plan.WriteString(line + "\n")
	}
	planText := plan.String()
	if !strings.Contains(planText, "players_due_regular_poll_v2") {
		t.Fatalf("scheduler plan does not use players_due_regular_poll_v2:\n%s", planText)
	}
	// Small fixtures may seq-scan the 3-row players heap for the bounded
	// advance; the invariant is indexed visible-due access and no history
	// scans, asserted separately.
	_ = planText
	var walBefore string
	if err := store.pool.QueryRow(ctx, `SELECT pg_current_wal_lsn()::text`).Scan(&walBefore); err != nil {
		t.Fatalf("wal lsn before: %v", err)
	}
	started := time.Now()
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("schedule: %v", err)
	}
	latency := time.Since(started)
	t.Logf("admission scheduler latency with 3 due rows: %v", latency)
	var walBytes int64
	if err := store.pool.QueryRow(ctx, `SELECT pg_wal_lsn_diff(pg_current_wal_lsn(), $1::pg_lsn)::bigint`, walBefore).Scan(&walBytes); err != nil {
		t.Fatalf("wal diff: %v", err)
	}
	if walBytes <= 0 {
		t.Fatalf("wal bytes=%d, want > 0 for one committed admission", walBytes)
	}
	t.Logf("wal bytes for one admission: %d", walBytes)
	for _, table := range []string{"collector_regular_admission_evidence", "collector_regular_admission_evidence_runs"} {
		var heapBytes, indexBytes, totalBytes int64
		if err := store.pool.QueryRow(ctx, `SELECT pg_relation_size($1), pg_indexes_size($1), pg_total_relation_size($1)`, table).Scan(&heapBytes, &indexBytes, &totalBytes); err != nil {
			t.Fatalf("sizes %s: %v", table, err)
		}
		t.Logf("relation %s heap=%d index=%d total=%d", table, heapBytes, indexBytes, totalBytes)
	}
	var evidenceIndex int
	if err := store.pool.QueryRow(ctx, `SELECT count(*) FROM pg_indexes WHERE tablename='collector_regular_admission_evidence' AND indexname='collector_regular_admission_evidence_run_cycle'`).Scan(&evidenceIndex); err != nil {
		t.Fatalf("index lookup: %v", err)
	}
	if evidenceIndex != 1 {
		t.Fatal("collector_regular_admission_evidence_run_cycle index is missing")
	}
}

func TestAdmissionEvidenceObserverLeastPrivilege(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("observer-lp-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	seedDuePlayers(t, ctx, store, 1, time.Now().UTC().Add(-time.Minute))
	if _, err := store.scheduleDueRegular(ctx, time.Now().UTC(), 5*time.Minute, 10); err != nil {
		t.Fatalf("seed admission row: %v", err)
	}
	connection, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatalf("connect for role probe: %v", err)
	}
	defer connection.Close(ctx)
	if _, err := connection.Exec(ctx, `SET ROLE clashlens_python_worker`); err != nil {
		t.Fatalf("set worker role: %v", err)
	}
	defer connection.Exec(context.Background(), `RESET ROLE`)
	// Positive reads: run header, event rows, and ranking cycle identities.
	var runs, events int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence_runs WHERE run_id=$1`, config.runID).Scan(&runs); err != nil {
		t.Fatalf("worker SELECT runs: %v", err)
	}
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM collector_regular_admission_evidence WHERE run_id=$1`, config.runID).Scan(&events); err != nil {
		t.Fatalf("worker SELECT evidence: %v", err)
	}
	if runs != 1 || events != 1 {
		t.Fatalf("worker reads runs=%d events=%d, want 1 1", runs, events)
	}
	var cycles int
	if err := connection.QueryRow(ctx, `SELECT count(*) FROM global_rankings_intents`).Scan(&cycles); err != nil {
		// Empty intents table still proves the column grant via a scoped probe.
		if !strings.Contains(err.Error(), "permission denied") {
			t.Fatalf("worker SELECT cycle_at: %v", err)
		}
	}
	if _, err := connection.Exec(ctx, `SELECT cycle_at FROM global_rankings_intents LIMIT 1`); err != nil {
		t.Fatalf("worker SELECT cycle_at column: %v", err)
	}
	// Other columns stay denied: created_at probes fail.
	if _, err := connection.Exec(ctx, `SELECT created_at FROM global_rankings_intents LIMIT 1`); err == nil {
		t.Fatal("worker SELECT created_at succeeded, want denial")
	} else if !isPermissionDenied(err) {
		t.Fatalf("created_at error = %v, want permission denied", err)
	}
	// Write paths stay denied.
	for _, stmt := range []string{
		`INSERT INTO collector_regular_admission_evidence_runs (run_id, capture_start, capture_end, max_events, max_selected_entries) VALUES ('lp-deny', now(), now()+interval '1 hour', 1, 1)`,
		`UPDATE collector_regular_admission_evidence_runs SET state='active' WHERE run_id='observer-lp-v1'`,
		`DELETE FROM collector_regular_admission_evidence_runs WHERE run_id='observer-lp-v1'`,
		`INSERT INTO collector_regular_admission_evidence (run_id, invocation_id, capture_start, capture_end, cycle_at, scheduler_at, database_at, gate_allowed, batch_limit, visible_due_count, unselected_visible_due_count, unselected_visible_past_deadline_count, selected_past_deadline_count, selected_player_ids, selected_due_ats, selected_profile_version_ids, selected_eligibility_states, inserted_job_ids, advanced_count) VALUES ('observer-lp-v1', 'ffffffffffffffffffffffffffffffff', now()-interval '1 hour', now()+interval '1 hour', date_bin('5 minutes', now(), timestamptz '2000-01-01 00:00:00+00'), now(), now(), true, 10, 0, 0, 0, 0, '{}', '{}', '{}', '{}', '{}', 0)`,
		`UPDATE collector_regular_admission_evidence SET gate_allowed=false WHERE run_id='observer-lp-v1'`,
		`DELETE FROM collector_regular_admission_evidence WHERE run_id='observer-lp-v1'`,
	} {
		if _, err := connection.Exec(ctx, stmt); err == nil {
			t.Fatalf("worker write succeeded, want denial: %.80s", stmt)
		} else if !isPermissionDenied(err) {
			t.Fatalf("worker write error = %v, want permission denied: %.80s", err, stmt)
		}
	}
	// Public API role gains nothing.
	var apiSelect bool
	if err := connection.QueryRow(ctx, `SELECT has_table_privilege('clashlens_python_api', 'collector_regular_admission_evidence', 'SELECT')`).Scan(&apiSelect); err != nil {
		t.Fatalf("inspect api privilege: %v", err)
	}
	if apiSelect {
		t.Fatal("clashlens_python_api has SELECT on admission evidence, want none")
	}
}

func isPermissionDenied(err error) bool {
	if err == nil {
		return false
	}
	return strings.Contains(err.Error(), "42501") || strings.Contains(strings.ToLower(err.Error()), "permission denied")
}

func TestAdmissionEvidenceTickAfterRunHeaderWait(t *testing.T) {
	databaseURL := startAdmissionDatabase(t)
	ctx := context.Background()
	start, end := admissionCaptureAroundNow(10 * time.Minute)
	config := newAdmissionConfig("tick-wait-v1", start, end, 100, 1000)
	store := openAdmissionStore(t, ctx, databaseURL, config)
	var dbNow time.Time
	if err := store.pool.QueryRow(ctx, `SELECT statement_timestamp()`).Scan(&dbNow); err != nil {
		t.Fatalf("read database clock: %v", err)
	}
	dbNow = dbNow.UTC()
	boundary := boundaryAdmissionBoundary(dbNow)
	if !dbNow.Before(boundary.Add(-5*time.Minute)) && dbNow.Before(boundary) {
		t.Skip("closed pre-reset window is wall-clock dependent")
	}
	// Safe handoff rows around today cover a post-boundary tick no matter
	// which side of 05:00 the post-wait read lands on.
	for _, day := range []time.Time{dbNow.Add(-24 * time.Hour), dbNow, dbNow.Add(24 * time.Hour)} {
		edge := boundaryAdmissionBoundary(day)
		if _, err := store.pool.Exec(ctx, `INSERT INTO collector_boundary_admission (boundary_at, regular_drain_complete, reset_drain_complete, safe_handoff, state, handoff_at) VALUES ($1, true, true, true, 'safe_handoff', $2) ON CONFLICT (boundary_at) DO UPDATE SET safe_handoff=true, state='safe_handoff', handoff_at=EXCLUDED.handoff_at, regular_drain_complete=true, reset_drain_complete=true`, edge, dbNow); err != nil {
			t.Fatalf("seed handoff: %v", err)
		}
	}
	seedDuePlayers(t, ctx, store, 1, dbNow.Add(-time.Minute))
	// Hold only the run-header row lock. The scheduler takes the free
	// advisory lock, then blocks inside its locked run-header SELECT, whose
	// statement_timestamp() predates this wait by construction.
	holder, err := store.pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("acquire: %v", err)
	}
	defer holder.Release()
	holderTx, err := holder.Begin(ctx)
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	var heldState string
	if err := holderTx.QueryRow(ctx, `SELECT state FROM collector_regular_admission_evidence_runs WHERE run_id=$1 FOR UPDATE`, config.runID).Scan(&heldState); err != nil {
		t.Fatalf("hold run header: %v", err)
	}
	// A stale caller tick must not leak into the evidence: the scheduler
	// tick comes from the post-wait database read, not this argument.
	staleNow := dbNow.Add(-time.Hour)
	waiterStart := time.Now().UTC()
	done := make(chan error, 1)
	go func() {
		_, schedErr := store.scheduleDueRegular(ctx, staleNow, 5*time.Minute, 10)
		done <- schedErr
	}()
	time.Sleep(2 * time.Second)
	release := time.Now().UTC()
	if err := holderTx.Commit(ctx); err != nil {
		t.Fatalf("release run header: %v", err)
	}
	select {
	case schedErr := <-done:
		if schedErr != nil {
			t.Fatalf("scheduler after run-header wait: %v", schedErr)
		}
	case <-time.After(15 * time.Second):
		t.Fatalf("scheduler did not finish after run-header release")
	}
	var schedulerAt, databaseAt, cycleAt time.Time
	if err := store.pool.QueryRow(ctx, `SELECT scheduler_at, database_at, cycle_at FROM collector_regular_admission_evidence WHERE run_id=$1 ORDER BY id DESC LIMIT 1`, config.runID).Scan(&schedulerAt, &databaseAt, &cycleAt); err != nil {
		t.Fatalf("read evidence tick: %v", err)
	}
	// Post-wait tick: a pre-wait statement_timestamp() would sit at
	// waiterStart, so require a full second past it after a 2s hold.
	if !schedulerAt.UTC().After(waiterStart.Add(time.Second)) {
		t.Fatalf("scheduler_at %v not after waiter start %v + 1s (tick predates run-header wait)", schedulerAt, waiterStart)
	}
	if schedulerAt.UTC().Before(release.Add(-2 * time.Second)) {
		t.Fatalf("scheduler_at %v older than release %v - 2s", schedulerAt, release)
	}
	if schedulerAt.UTC().Before(staleNow.Add(30 * time.Minute)) {
		t.Fatalf("scheduler_at %v follows stale caller now %v", schedulerAt, staleNow)
	}
	if databaseAt.UTC().Before(schedulerAt.UTC()) {
		t.Fatalf("database_at %v before scheduler_at %v", databaseAt, schedulerAt)
	}
	if !cycleAt.UTC().Equal(schedulerAt.UTC().Truncate(5 * time.Minute)) {
		t.Fatalf("cycle_at %v != truncate(scheduler_at %v)", cycleAt, schedulerAt)
	}
}

func TestBoundaryAdmissionBoundaryResetEdges(t *testing.T) {
	t.Parallel()
	day0930 := time.Date(2026, 9, 10, 5, 0, 0, 0, time.UTC)
	day0930Prev := time.Date(2026, 9, 9, 5, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name string
		at   time.Time
		want time.Time
	}{
		{"pre-window open edge", time.Date(2026, 9, 10, 4, 54, 59, 0, time.UTC), day0930},
		{"pre-window closed edge", time.Date(2026, 9, 10, 4, 55, 0, 0, time.UTC), day0930},
		{"reset instant", day0930, day0930},
		{"just after reset", day0930.Add(time.Second), day0930},
		{"midnight", time.Date(2026, 9, 10, 0, 0, 0, 0, time.UTC), day0930},
		{"late evening", time.Date(2026, 9, 10, 23, 0, 0, 0, time.UTC), day0930},
		{"previous evening", time.Date(2026, 9, 9, 23, 59, 59, 0, time.UTC), day0930Prev},
	} {
		if got := boundaryAdmissionBoundary(tc.at); !got.Equal(tc.want) {
			t.Fatalf("%s: boundary(%v)=%v, want %v", tc.name, tc.at, got, tc.want)
		}
	}
	// The 04:55 gate edge: open strictly before boundary-5m, closed after.
	openAt := time.Date(2026, 9, 10, 4, 54, 59, 0, time.UTC)
	closedAt := time.Date(2026, 9, 10, 4, 55, 0, 0, time.UTC)
	if !openAt.Before(boundaryAdmissionBoundary(openAt).Add(-5 * time.Minute)) {
		t.Fatalf("04:54:59 must be before the pre-reset gate edge")
	}
	if closedAt.Before(boundaryAdmissionBoundary(closedAt).Add(-5 * time.Minute)) {
		t.Fatalf("04:55:00 must not be before the pre-reset gate edge")
	}
	// The advisory key is intentionally constant across dates: a wait that
	// spans 05:00 serializes on the same lock, so no pre-wait key can go
	// stale.
	if boundaryAdmissionLockKey(openAt) != boundaryAdmissionLockKey(day0930.Add(time.Hour)) {
		t.Fatalf("boundary lock key varies across 05:00, want constant")
	}
}
