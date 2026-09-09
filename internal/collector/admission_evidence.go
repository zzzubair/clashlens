package collector

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
)

var (
	errAdmissionCapacityExceeded   = errors.New("admission_evidence_capacity_exceeded")
	errAdmissionCaptureOutOfRange  = errors.New("admission_evidence_capture_out_of_range")
	errAdmissionRunMissing         = errors.New("admission evidence run is missing")
	errAdmissionRunConflict        = errors.New("admission evidence run conflicts with configuration")
	errAdmissionCommitUnknown      = errors.New("admission evidence commit outcome unknown")
	admissionEvidenceRunIDPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)
	admissionEvidenceInvocationLen = 32
)

type admissionEvidenceConfig struct {
	runID              string
	captureStart       time.Time
	captureEnd         time.Time
	maxEvents          int
	maxSelectedEntries int64
}

func parseAdmissionEvidenceConfig(getenv func(string) string) (*admissionEvidenceConfig, error) {
	runID := strings.TrimSpace(getenv("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_RUN_ID"))
	startRaw := strings.TrimSpace(getenv("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_START"))
	endRaw := strings.TrimSpace(getenv("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END"))
	maxEventsRaw := strings.TrimSpace(getenv("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS"))
	maxSelectedRaw := strings.TrimSpace(getenv("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_SELECTED_ENTRIES"))
	setCount := 0
	for _, value := range []string{runID, startRaw, endRaw, maxEventsRaw, maxSelectedRaw} {
		if value != "" {
			setCount++
		}
	}
	if setCount == 0 {
		return nil, nil
	}
	if setCount != 5 {
		return nil, errors.New("admission evidence settings must be set all-or-none: run ID, start, end, max events, max selected entries")
	}
	if !admissionEvidenceRunIDPattern.MatchString(runID) {
		return nil, errors.New("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_RUN_ID must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
	}
	start, err := parseAdmissionEvidenceBound("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_START", startRaw)
	if err != nil {
		return nil, err
	}
	end, err := parseAdmissionEvidenceBound("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END", endRaw)
	if err != nil {
		return nil, err
	}
	if !end.After(start) {
		return nil, errors.New("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END must be after start")
	}
	if end.Sub(start) > 30*time.Hour {
		return nil, errors.New("admission evidence capture interval must not exceed 30 hours")
	}
	maxEvents, err := strconv.Atoi(maxEventsRaw)
	if err != nil || maxEvents < 1 || maxEvents > 108000 {
		return nil, errors.New("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS must be between 1 and 108000")
	}
	maxSelected, err := strconv.ParseInt(maxSelectedRaw, 10, 64)
	if err != nil || maxSelected < 1 || maxSelected > 5000000 {
		return nil, errors.New("CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_SELECTED_ENTRIES must be between 1 and 5000000")
	}
	return &admissionEvidenceConfig{
		runID:              runID,
		captureStart:       start,
		captureEnd:         end,
		maxEvents:          maxEvents,
		maxSelectedEntries: maxSelected,
	}, nil
}

func parseAdmissionEvidenceBound(name, raw string) (time.Time, error) {
	parsed, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, fmt.Errorf("%s must be RFC3339 UTC", name)
	}
	if _, offset := parsed.Zone(); offset != 0 {
		return time.Time{}, fmt.Errorf("%s must be UTC", name)
	}
	return parsed.UTC(), nil
}

func newAdmissionInvocationID() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("generate admission invocation ID: %w", err)
	}
	return hex.EncodeToString(raw[:]), nil
}

func (s *store) configureAdmissionEvidence(config *admissionEvidenceConfig) {
	s.admissionEvidence = config
}

func (s *store) ensureAdmissionEvidenceRun(ctx context.Context, config *admissionEvidenceConfig) error {
	if config == nil {
		return nil
	}
	if _, err := s.pool.Exec(ctx, `
		INSERT INTO collector_regular_admission_evidence_runs (
			run_id, capture_start, capture_end, max_events, max_selected_entries
		) VALUES ($1, $2, $3, $4, $5)
		ON CONFLICT (run_id) DO NOTHING
	`, config.runID, config.captureStart, config.captureEnd, config.maxEvents, config.maxSelectedEntries); err != nil {
		return fmt.Errorf("initialize admission evidence run: %w", err)
	}
	var captureStart, captureEnd time.Time
	var maxEvents int
	var maxSelected int64
	var state string
	if err := s.pool.QueryRow(ctx, `
		SELECT capture_start, capture_end, max_events, max_selected_entries, state
		FROM collector_regular_admission_evidence_runs
		WHERE run_id = $1
	`, config.runID).Scan(&captureStart, &captureEnd, &maxEvents, &maxSelected, &state); err != nil {
		return fmt.Errorf("read admission evidence run: %w", err)
	}
	if !captureStart.Equal(config.captureStart) || !captureEnd.Equal(config.captureEnd) ||
		maxEvents != config.maxEvents || maxSelected != config.maxSelectedEntries {
		return fmt.Errorf("%w: %q", errAdmissionRunConflict, config.runID)
	}
	if state != "active" {
		return fmt.Errorf("%w: %q is %q", errAdmissionRunConflict, config.runID, state)
	}
	return nil
}

func (s *store) commitAdmissionTx(ctx context.Context, tx pgx.Tx) error {
	if s.commitTx != nil {
		return s.commitTx(ctx, tx)
	}
	return tx.Commit(ctx)
}

// scheduleDueRegularWithEvidence implements the final-plan evidence path:
// explicit READ COMMITTED transaction, advisory lock in its own statement,
// locked run-header validation, a fresh post-wait database tick, then the
// scheduler statement. The durable stop commits before Go returns its fixed
// error.
func (s *store) scheduleDueRegularWithEvidence(ctx context.Context, now time.Time, cycle time.Duration, batchSize int, config *admissionEvidenceConfig) (int, error) {
	if config == nil {
		return 0, errors.New("admission evidence configuration is required")
	}
	if s.contractVersion < 4 {
		return 0, errors.New("admission evidence requires contract version 4")
	}
	if cycle <= 0 {
		return 0, errors.New("poll cycle must be positive")
	}
	if batchSize < 1 || batchSize > 1000 {
		return 0, errors.New("scheduler batch size must be between 1 and 1000")
	}
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return 0, fmt.Errorf("begin admission evidence transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1, 0))`, boundaryAdmissionLockKey(now)); err != nil {
		return 0, fmt.Errorf("lock admission evidence: %w", err)
	}
	var runCaptureStart, runCaptureEnd time.Time
	var runMaxEvents int
	var runMaxSelected int64
	var runState string
	if err := tx.QueryRow(ctx, `
		SELECT run.capture_start, run.capture_end,
		       run.max_events, run.max_selected_entries, run.state
		FROM collector_regular_admission_evidence_runs AS run
		WHERE run.run_id = $1
		FOR UPDATE OF run
	`, config.runID).Scan(&runCaptureStart, &runCaptureEnd, &runMaxEvents, &runMaxSelected, &runState); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, fmt.Errorf("%w: %q", errAdmissionRunMissing, config.runID)
		}
		return 0, fmt.Errorf("lock admission evidence run: %w", err)
	}
	if !runCaptureStart.Equal(config.captureStart) || !runCaptureEnd.Equal(config.captureEnd) ||
		runMaxEvents != config.maxEvents || runMaxSelected != config.maxSelectedEntries {
		return 0, fmt.Errorf("%w: %q", errAdmissionRunConflict, config.runID)
	}
	// Refresh the scheduler tick after the lock waits so a stale caller time
	// cannot admit against a pre-wait boundary. statement_timestamp() in the
	// locked SELECT above predates a run-header row-lock wait (statement
	// start precedes blocking), so read the database clock in a new
	// statement after both the advisory-lock and run-header-lock waits.
	var databaseNow time.Time
	if err := tx.QueryRow(ctx, `SELECT statement_timestamp()`).Scan(&databaseNow); err != nil {
		return 0, fmt.Errorf("read admission evidence tick: %w", err)
	}
	schedulerAt := databaseNow.UTC()
	cycleAt := schedulerAt.Truncate(cycle).UTC()
	boundary := boundaryAdmissionBoundary(schedulerAt)
	nextCycleStart := cycleAt.Add(cycle)
	cycleSeconds := int64(cycle / time.Second)
	invocationID, err := newAdmissionInvocationID()
	if err != nil {
		return 0, err
	}
	var admitted bool
	var runStateAfter, failureCodeAfter string
	var eventsWrittenAfter int
	var selectedWrittenAfter int64
	var insertedCount, advancedCount int
	var evidenceID *int64
	var selectedCount int64
	if err := tx.QueryRow(ctx, admissionEvidenceSchedulerSQL, schedulerAt, batchSize, cycleAt.Unix(), nextCycleStart, cycleSeconds,
		schedulerAt.UTC().Format(time.RFC3339), boundary.UTC().Format(time.RFC3339),
		config.runID, invocationID, config.captureStart, config.captureEnd, cycleAt,
	).Scan(&insertedCount, &advancedCount, &evidenceID, &selectedCount,
		&admitted, &runStateAfter, &failureCodeAfter, &eventsWrittenAfter, &selectedWrittenAfter); err != nil {
		return 0, fmt.Errorf("schedule due regular with evidence: %w", err)
	}
	if err := s.commitAdmissionTx(ctx, tx); err != nil {
		return 0, fmt.Errorf("%w: %w", errAdmissionCommitUnknown, err)
	}
	if s.metrics != nil {
		if !admitted {
			s.metrics.recordStorageError(failureCodeAfter)
		}
	}
	if !admitted {
		switch failureCodeAfter {
		case "admission_evidence_capacity_exceeded":
			return 0, errAdmissionCapacityExceeded
		case "admission_evidence_capture_out_of_range":
			return 0, errAdmissionCaptureOutOfRange
		default:
			if runStateAfter != "" {
				return 0, fmt.Errorf("admission evidence stopped: %s", runStateAfter)
			}
			return 0, errAdmissionCapacityExceeded
		}
	}
	if insertedCount != int(selectedCount) || advancedCount != int(selectedCount) {
		return insertedCount, fmt.Errorf("admission evidence root mismatch: selected %d inserted %d advanced %d", selectedCount, insertedCount, advancedCount)
	}
	_ = eventsWrittenAfter
	_ = selectedWrittenAfter
	_ = evidenceID
	return insertedCount, nil
}

const admissionEvidenceSchedulerSQL = `
	WITH tick AS MATERIALIZED (
		SELECT statement_timestamp() AS database_at
	), older_reset AS MATERIALIZED (
		WITH RECURSIVE lineage(job_id) AS (
			SELECT job.id
			FROM collector_jobs AS job
			JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
			WHERE sweep.boundary_at < $7::timestamptz
			  AND job.work_type IN ('reset_baseline','reset_profile','legacy_reset_profile')
			UNION
			SELECT child.id
			FROM collector_jobs AS child
			JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
			JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
		)
		SELECT count(*) FILTER (
			WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')
		) > 0 AS blocked
		FROM collector_jobs AS job
		JOIN lineage ON lineage.job_id = job.id
	), gate AS MATERIALIZED (
		SELECT CASE
			WHEN older_reset.blocked THEN false
			WHEN $6::timestamptz < $7::timestamptz - interval '5 minutes' THEN true
			WHEN $6::timestamptz >= $7::timestamptz THEN COALESCE((
				SELECT safe_handoff
				FROM collector_boundary_admission
				WHERE boundary_at = $7::timestamptz
				FOR UPDATE
			), false)
			ELSE false
		END AS allowed,
		(SELECT handoff_at FROM collector_boundary_admission WHERE boundary_at = $7::timestamptz) AS handoff_at
		FROM older_reset
	), visible_due AS MATERIALIZED (
		SELECT player.id, player.next_due_at
		FROM players AS player
		WHERE player.active AND player.next_due_at <= $1::timestamptz
	), due AS MATERIALIZED (
		SELECT player.id, player.normalized_tag, player.next_due_at,
		       player.current_profile_version_id, player.eligibility_state
		FROM players AS player
		CROSS JOIN gate
		WHERE gate.allowed
		  AND player.active AND player.next_due_at <= $1::timestamptz
		ORDER BY player.next_due_at, player.id
		FOR NO KEY UPDATE OF player SKIP LOCKED
		LIMIT $2
	), visibility AS MATERIALIZED (
		SELECT
			count(*)::integer AS visible_due_count,
			min(visible.next_due_at) AS visible_due_min_at,
			count(*) FILTER (WHERE selected.id IS NULL)::integer AS unselected_visible_due_count,
			min(visible.next_due_at) FILTER (WHERE selected.id IS NULL) AS unselected_visible_due_min_at,
			count(*) FILTER (WHERE selected.id IS NULL AND gate.allowed AND tick.database_at > COALESCE(GREATEST(visible.next_due_at, gate.handoff_at), visible.next_due_at) + interval '5 minutes')::integer AS unselected_past_deadline_count,
			min(visible.next_due_at) FILTER (WHERE selected.id IS NULL AND gate.allowed AND tick.database_at > COALESCE(GREATEST(visible.next_due_at, gate.handoff_at), visible.next_due_at) + interval '5 minutes') AS unselected_past_deadline_min_at
		FROM visible_due AS visible
		LEFT JOIN due AS selected ON selected.id = visible.id
		CROSS JOIN tick
		CROSS JOIN gate
	), selection AS MATERIALIZED (
		SELECT
			count(*)::bigint AS selected_count,
			COALESCE(array_agg(due.id ORDER BY due.id), ARRAY[]::bigint[]) AS selected_player_ids,
			COALESCE(array_agg(due.next_due_at ORDER BY due.id), ARRAY[]::timestamptz[]) AS selected_due_ats,
			COALESCE(array_agg(due.current_profile_version_id ORDER BY due.id), ARRAY[]::bigint[]) AS selected_profile_version_ids,
			COALESCE(array_agg(due.eligibility_state ORDER BY due.id), ARRAY[]::text[]) AS selected_eligibility_states,
			count(*) FILTER (WHERE tick.database_at > COALESCE(GREATEST(due.next_due_at, gate.handoff_at), due.next_due_at) + interval '5 minutes')::integer AS selected_past_deadline_count
		FROM due
		CROSS JOIN tick
		CROSS JOIN gate
	), reservation AS MATERIALIZED (
		UPDATE collector_regular_admission_evidence_runs AS run
		SET state = CASE
				WHEN run.state <> 'active' THEN run.state
				WHEN tick.database_at < run.capture_start OR tick.database_at >= run.capture_end OR $1::timestamptz < run.capture_start OR $1::timestamptz >= run.capture_end THEN 'capture_out_of_range'
				WHEN run.events_written + 1 > run.max_events OR run.selected_entries_written + selection.selected_count > run.max_selected_entries THEN 'capacity_exceeded'
				ELSE 'active' END,
		    events_written = CASE
				WHEN run.state = 'active'
				 AND tick.database_at >= run.capture_start AND tick.database_at < run.capture_end
				 AND $1::timestamptz >= run.capture_start AND $1::timestamptz < run.capture_end
				 AND run.events_written + 1 <= run.max_events
				 AND run.selected_entries_written + selection.selected_count <= run.max_selected_entries
				THEN run.events_written + 1 ELSE run.events_written END,
		    selected_entries_written = CASE
				WHEN run.state = 'active'
				 AND tick.database_at >= run.capture_start AND tick.database_at < run.capture_end
				 AND $1::timestamptz >= run.capture_start AND $1::timestamptz < run.capture_end
				 AND run.events_written + 1 <= run.max_events
				 AND run.selected_entries_written + selection.selected_count <= run.max_selected_entries
				THEN run.selected_entries_written + selection.selected_count ELSE run.selected_entries_written END,
		    stopped_at = CASE
				WHEN run.state = 'active'
				 AND tick.database_at >= run.capture_start AND tick.database_at < run.capture_end
				 AND $1::timestamptz >= run.capture_start AND $1::timestamptz < run.capture_end
				 AND run.events_written + 1 <= run.max_events
				 AND run.selected_entries_written + selection.selected_count <= run.max_selected_entries
				THEN NULL ELSE COALESCE(run.stopped_at, tick.database_at) END,
		    failure_code = CASE
				WHEN run.state <> 'active' THEN run.failure_code
				WHEN tick.database_at < run.capture_start OR tick.database_at >= run.capture_end OR $1::timestamptz < run.capture_start OR $1::timestamptz >= run.capture_end THEN 'admission_evidence_capture_out_of_range'
				WHEN run.events_written + 1 > run.max_events OR run.selected_entries_written + selection.selected_count > run.max_selected_entries THEN 'admission_evidence_capacity_exceeded'
				ELSE NULL END
		FROM selection, tick
		WHERE run.run_id = $8::text
		RETURNING run.state, run.failure_code, run.events_written, run.selected_entries_written, run.state = 'active' AS admitted
	), inserted AS (
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, status
		)
		SELECT 'regular_poll', due.id, due.normalized_tag, 'normal',
			CASE WHEN due.next_due_at <= $1::timestamptz - ($5::double precision * interval '1 second')
				THEN 200 ELSE 100 END,
			$1::timestamptz, 'regular:' || due.id || ':' || ($3::bigint)::text, 'pending'
		FROM due CROSS JOIN reservation
		WHERE reservation.admitted
		ON CONFLICT DO NOTHING
		RETURNING id, player_id
	), advanced AS (
		UPDATE players AS player
		SET next_due_at = $4::timestamptz + CASE
			WHEN $5::bigint < 1 THEN interval '0 seconds'
			ELSE ((player.id - 1) % $5::bigint) * interval '1 second' END
		FROM due CROSS JOIN reservation
		WHERE reservation.admitted AND player.id = due.id
		RETURNING player.id
	), evidence AS (
		INSERT INTO collector_regular_admission_evidence (
			run_id, invocation_id, capture_start, capture_end,
			cycle_at, scheduler_at, database_at,
			gate_allowed, gate_handoff_at, batch_limit,
			visible_due_count, visible_due_min_at,
			unselected_visible_due_count, unselected_visible_due_min_at,
			unselected_visible_past_deadline_count, unselected_visible_past_deadline_min_at,
			selected_past_deadline_count,
			selected_player_ids, selected_due_ats,
			selected_profile_version_ids, selected_eligibility_states,
			inserted_job_ids, advanced_count
		)
		SELECT $8::text, $9::text, $10::timestamptz, $11::timestamptz,
			$12::timestamptz, $1::timestamptz, tick.database_at,
			gate.allowed, gate.handoff_at, $2,
			visibility.visible_due_count, visibility.visible_due_min_at,
			visibility.unselected_visible_due_count, visibility.unselected_visible_due_min_at,
			visibility.unselected_past_deadline_count, visibility.unselected_past_deadline_min_at,
			selection.selected_past_deadline_count,
			selection.selected_player_ids, selection.selected_due_ats,
			selection.selected_profile_version_ids, selection.selected_eligibility_states,
			COALESCE((SELECT array_agg(id ORDER BY id) FROM inserted), ARRAY[]::bigint[]),
			(SELECT count(*) FROM advanced)
		FROM gate, visibility, selection, reservation, tick
		WHERE reservation.admitted
		RETURNING id
	)
	SELECT
		COALESCE((SELECT count(*) FROM inserted), 0),
		COALESCE((SELECT count(*) FROM advanced), 0),
		(SELECT id FROM evidence),
		COALESCE((SELECT selected_count FROM selection), 0),
		reservation.admitted,
		reservation.state,
		COALESCE(reservation.failure_code, ''),
		reservation.events_written,
		reservation.selected_entries_written
	FROM reservation
`
