package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"testing"
	"time"
)

const step8AdmissionMarker = "CLASHLENS_STEP8_ADMISSION="

// TestStep8BoundaryAdmissionProbe is an opt-in real-path probe used by the
// retained performance runner. Normal test runs skip it. The probe deliberately
// calls the same unexported store seams as the production scheduler, against the
// runner's disposable schema, and emits only bounded aggregate evidence.
func TestStep8BoundaryAdmissionProbe(t *testing.T) {
	databaseURL := os.Getenv("CLASHLENS_STEP8_ADMISSION_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("Step 8 admission probe is not enabled")
	}
	phase := os.Getenv("CLASHLENS_STEP8_ADMISSION_PHASE")
	boundary, err := time.Parse(time.RFC3339, os.Getenv("CLASHLENS_STEP8_ADMISSION_BOUNDARY"))
	if err != nil {
		t.Fatal("invalid Step 8 admission boundary")
	}
	expectedPopulation, err := strconv.Atoi(os.Getenv("CLASHLENS_STEP8_ADMISSION_POPULATION"))
	if err != nil || expectedPopulation < 1 || expectedPopulation > 12_500 {
		t.Fatal("invalid Step 8 admission population")
	}

	ctx := context.Background()
	store, err := openStore(ctx, databaseURL, 5)
	if err != nil {
		t.Fatal("open Step 8 admission store")
	}
	defer store.close()
	if err := store.ready(ctx); err != nil {
		t.Fatal("Step 8 admission store is not ready")
	}

	var evidence map[string]any
	switch phase {
	case "admit":
		evidence = step8AdmissionProbeAdmit(t, ctx, store, boundary, expectedPopulation)
	case "handoff":
		evidence = step8AdmissionProbeHandoff(t, ctx, store, boundary, expectedPopulation)
	default:
		t.Fatal("invalid Step 8 admission phase")
	}
	payload, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal("encode Step 8 admission evidence")
	}
	fmt.Println(step8AdmissionMarker + string(payload))
}

func step8AdmissionProbeAdmit(
	t *testing.T,
	ctx context.Context,
	store *store,
	boundary time.Time,
	expectedPopulation int,
) map[string]any {
	t.Helper()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, status, created_at, updated_at
		)
		SELECT 'regular_poll', 'player', id, normalized_tag, 'normal',
			100, $1, 'step8-prior-regular-drain', 'pending', $2, $2
		FROM players ORDER BY id LIMIT 1
	`, boundary.Add(-time.Minute), boundary.Add(-time.Minute)); err != nil {
		t.Fatal("seed Step 8 prior regular work")
	}
	if _, err := store.pool.Exec(ctx, `
		WITH parent_attempt AS (
			INSERT INTO collector_attempts (job_id, status, started_at, completed_at)
			SELECT id, 'complete', $1, $1 FROM collector_jobs
			WHERE coalescing_key = 'step8-prior-regular-drain'
			RETURNING id, job_id
		)
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, parent_attempt_id,
			required_endpoint, status, created_at, updated_at
		)
		SELECT 'endpoint_retry', 'player', parent.player_id,
			parent.normalized_tag, 'normal', 50, $1,
			'step8-prior-regular-descendant', parent_attempt.id,
			'profile', 'waiting_dependency', $1, $1
		FROM collector_jobs AS parent
		JOIN parent_attempt ON parent_attempt.job_id = parent.id
		WHERE parent.coalescing_key = 'step8-prior-regular-drain'
	`, boundary.Add(-time.Minute)); err != nil {
		t.Fatal("seed Step 8 prior regular descendant")
	}

	sweepID, created, err := store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil || sweepID != 0 || created {
		t.Fatal("prior regular work did not block reset admission")
	}
	before := step8AdmissionState(t, ctx, store, boundary)
	if before.state != "regular_draining" || before.regularCount != 2 ||
		before.regularDrained || before.resetDrained || before.safe ||
		before.resetGeneration != nil || before.handoffAt != nil {
		t.Fatal("prior regular drain state is invalid")
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'complete', updated_at = clock_timestamp()
		WHERE coalescing_key IN (
			'step8-prior-regular-drain', 'step8-prior-regular-descendant'
		)
	`); err != nil {
		t.Fatal("complete Step 8 prior regular work")
	}

	sweepID, created, err = store.prepareBoundaryAdmission(ctx, boundary)
	if err != nil || sweepID == 0 || !created {
		t.Fatal("production reset admission did not create a sweep")
	}
	after := step8AdmissionState(t, ctx, store, boundary)
	var members, roots int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM collector_jobs
			 WHERE sweep_id = $1 AND work_type = 'reset_baseline')
	`, sweepID).Scan(&members, &roots); err != nil {
		t.Fatal("read Step 8 admitted reset facts")
	}
	allowed, err := store.regularAdmissionAllowed(ctx, boundary)
	if err != nil {
		t.Fatal("check Step 8 reset exclusivity")
	}
	scheduled, err := store.scheduleDueRegular(ctx, boundary, 5*time.Minute, 1)
	if err != nil {
		t.Fatal("check Step 8 scheduler gate")
	}
	if after.state != "reset_draining" || after.regularCount != 0 ||
		after.resetCount != roots || !after.regularDrained || after.resetDrained ||
		after.safe || after.resetGeneration == nil || *after.resetGeneration != 1 ||
		after.handoffAt != nil || members != expectedPopulation ||
		roots != expectedPopulation || allowed || scheduled != 0 {
		t.Fatal("production reset admission facts are invalid")
	}
	return map[string]any{
		"phase":                          "admit",
		"blocked_before_regular_drain":   true,
		"state_before_drain":             before.state,
		"regular_nonterminal_before":     before.regularCount,
		"state_after_admission":          after.state,
		"regular_drain_complete":         after.regularDrained,
		"reset_drain_complete":           after.resetDrained,
		"safe_handoff":                   after.safe,
		"reset_generation":               *after.resetGeneration,
		"regular_nonterminal_after":      after.regularCount,
		"reset_nonterminal_after":        after.resetCount,
		"membership_count":               members,
		"reset_root_count":               roots,
		"regular_allowed_during_reset":   allowed,
		"regular_scheduled_during_reset": scheduled,
	}
}

func step8AdmissionProbeHandoff(
	t *testing.T,
	ctx context.Context,
	store *store,
	boundary time.Time,
	expectedPopulation int,
) map[string]any {
	t.Helper()
	sweepID, created, err := store.prepareBoundaryAdmission(ctx, boundary.Add(time.Minute))
	if err != nil || sweepID == 0 || created {
		t.Fatal("production reset handoff did not reuse the admitted sweep")
	}
	admission := step8AdmissionState(t, ctx, store, boundary)
	allowed, err := store.regularAdmissionAllowed(ctx, boundary.Add(time.Minute))
	if err != nil {
		t.Fatal("check Step 8 post-reset admission")
	}
	var members, completedRoots int
	if err := store.pool.QueryRow(ctx, `
		SELECT
			(SELECT count(*) FROM collector_reset_sweep_members WHERE sweep_id = $1),
			(SELECT count(*) FROM collector_jobs
			 WHERE sweep_id = $1 AND work_type = 'reset_baseline' AND status = 'complete')
	`, sweepID).Scan(&members, &completedRoots); err != nil {
		t.Fatal("read Step 8 handoff facts")
	}
	scheduled, err := store.scheduleDueRegular(
		ctx, boundary.Add(time.Minute), 5*time.Minute, 1,
	)
	if err != nil {
		t.Fatal("check Step 8 scheduler after handoff")
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs SET status = 'complete', updated_at = clock_timestamp()
		WHERE work_type = 'regular_poll' AND status = 'pending'
	`); err != nil {
		t.Fatal("clean Step 8 post-handoff scheduler evidence")
	}
	if admission.state != "safe_handoff" || admission.regularCount != 0 ||
		admission.resetCount != 0 || !admission.regularDrained ||
		!admission.resetDrained || !admission.safe ||
		admission.resetGeneration == nil || *admission.resetGeneration != 1 ||
		admission.handoffAt == nil || members != expectedPopulation ||
		completedRoots != expectedPopulation || !allowed || scheduled != 1 {
		t.Fatal("production reset handoff facts are invalid")
	}
	return map[string]any{
		"phase":                           "handoff",
		"state":                           admission.state,
		"regular_drain_complete":          admission.regularDrained,
		"reset_drain_complete":            admission.resetDrained,
		"safe_handoff":                    admission.safe,
		"reset_generation":                *admission.resetGeneration,
		"handoff_recorded":                admission.handoffAt != nil,
		"regular_nonterminal_count":       admission.regularCount,
		"reset_nonterminal_count":         admission.resetCount,
		"membership_count":                members,
		"completed_reset_root_count":      completedRoots,
		"regular_allowed_after_handoff":   allowed,
		"regular_scheduled_after_handoff": scheduled,
	}
}

type step8AdmissionFacts struct {
	state           string
	regularDrained  bool
	resetDrained    bool
	safe            bool
	resetGeneration *int
	regularCount    int
	resetCount      int
	handoffAt       *time.Time
}

func step8AdmissionState(
	t *testing.T, ctx context.Context, store *store, boundary time.Time,
) step8AdmissionFacts {
	t.Helper()
	var facts step8AdmissionFacts
	if err := store.pool.QueryRow(ctx, `
		SELECT state, regular_drain_complete, reset_drain_complete,
			safe_handoff, reset_generation, regular_nonterminal_count,
			reset_nonterminal_count, handoff_at
		FROM collector_boundary_admission WHERE boundary_at = $1
	`, boundary).Scan(
		&facts.state,
		&facts.regularDrained,
		&facts.resetDrained,
		&facts.safe,
		&facts.resetGeneration,
		&facts.regularCount,
		&facts.resetCount,
		&facts.handoffAt,
	); err != nil {
		t.Fatal("read Step 8 boundary admission state")
	}
	return facts
}
