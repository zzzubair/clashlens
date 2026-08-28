package collector

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// boundaryAdmissionBoundary is the reset boundary whose work may affect the
// current scheduler tick. Before 05:00 it points at the upcoming boundary;
// after 05:00 it points at today's boundary.
func boundaryAdmissionBoundary(now time.Time) time.Time {
	now = now.UTC()
	return time.Date(now.Year(), now.Month(), now.Day(), 5, 0, 0, 0, time.UTC)
}

func boundaryAdmissionLockKey(_ time.Time) string {
	// One lock serializes admission, reset capture, and publication handoff
	// across all boundaries; a per-day lock would permit stale prior work to
	// overlap the next day's regular roots.
	return "collector-boundary-admission"
}

func (s *store) boundaryAdmissionAvailable(ctx context.Context) (bool, error) {
	var relation *string
	if err := s.pool.QueryRow(ctx,
		"SELECT to_regclass(current_schema() || '.collector_boundary_admission')",
	).Scan(&relation); err != nil {
		return false, fmt.Errorf("check boundary admission schema: %w", err)
	}
	return relation != nil, nil
}

func (s *store) regularJobsDrained(ctx context.Context, boundary time.Time) (bool, error) {
	var count int
	if err := s.pool.QueryRow(ctx, `
        WITH RECURSIVE lineage(job_id) AS (
            SELECT id FROM collector_jobs
            WHERE work_type = 'regular_poll' AND created_at < $1
            UNION
            SELECT child.id
            FROM collector_jobs AS child
            JOIN collector_attempts AS parent_attempt
              ON parent_attempt.id = child.parent_attempt_id
            JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
        )
        SELECT count(*)
        FROM collector_jobs AS job
        JOIN lineage ON lineage.job_id = job.id
        WHERE job.status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
    `, boundary).Scan(&count); err != nil {
		return false, fmt.Errorf("count regular lineage jobs before reset: %w", err)
	}
	return count == 0, nil
}

func (s *store) resetJobsDrained(ctx context.Context, boundary time.Time) (bool, error) {
	var count int
	if err := s.pool.QueryRow(ctx, `
        WITH RECURSIVE lineage(job_id) AS (
            SELECT job.id
            FROM collector_jobs AS job
            JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
            WHERE sweep.boundary_at = $1
              AND job.work_type IN ('reset_baseline', 'reset_profile', 'legacy_reset_profile')
            UNION
            SELECT child.id
            FROM collector_jobs AS child
            JOIN collector_attempts AS parent_attempt
              ON parent_attempt.id = child.parent_attempt_id
            JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
        )
        SELECT count(*)
        FROM collector_jobs AS job
        JOIN lineage ON lineage.job_id = job.id
        WHERE job.status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
    `, boundary).Scan(&count); err != nil {
		return false, fmt.Errorf("count reset lineage jobs: %w", err)
	}
	return count == 0, nil
}

func (s *store) setBoundaryAdmission(
	ctx context.Context,
	boundary time.Time,
	sweepID *int64,
	regularDrained, resetDrained, safeHandoff bool,
) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin boundary admission transaction: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		SELECT pg_advisory_xact_lock(hashtextextended($1, 0))
	`, boundaryAdmissionLockKey(boundary)); err != nil {
		return fmt.Errorf("lock boundary admission: %w", err)
	}
	_, err = tx.Exec(ctx, `
        INSERT INTO collector_boundary_admission (
            boundary_at, reset_sweep_id, regular_drain_complete,
            reset_drain_complete, safe_handoff, state, reset_generation,
            regular_nonterminal_count, reset_nonterminal_count, handoff_at
        ) VALUES ($1, $2::bigint, $3, $4, $5,
                  CASE WHEN $5 THEN 'safe_handoff'
                       WHEN $2::bigint IS NOT NULL AND NOT $4 THEN 'reset_running'
                       WHEN $2::bigint IS NOT NULL THEN 'reset_draining'
                       ELSE 'regular_draining' END,
                  CASE WHEN $2::bigint IS NOT NULL THEN 1 ELSE NULL END,
                  (WITH RECURSIVE lineage(job_id) AS (
                       SELECT id FROM collector_jobs WHERE work_type = 'regular_poll' AND created_at < $1
                       UNION
                       SELECT child.id FROM collector_jobs AS child
                       JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
                       JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
                   ) SELECT count(*) FROM collector_jobs AS job JOIN lineage ON lineage.job_id = job.id
                       WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')),
                  (WITH RECURSIVE lineage(job_id) AS (
                       SELECT job.id FROM collector_jobs AS job JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
                       WHERE sweep.boundary_at = $1 AND job.work_type IN ('reset_baseline','reset_profile','legacy_reset_profile')
                       UNION
                       SELECT child.id FROM collector_jobs AS child
                       JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
                       JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
                   ) SELECT count(*) FROM collector_jobs AS job JOIN lineage ON lineage.job_id = job.id
                       WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')),
                  CASE WHEN $5 THEN clock_timestamp() ELSE NULL END)
        ON CONFLICT (boundary_at) DO UPDATE SET
            reset_sweep_id = COALESCE(EXCLUDED.reset_sweep_id,
                                      collector_boundary_admission.reset_sweep_id),
            regular_drain_complete = collector_boundary_admission.regular_drain_complete OR EXCLUDED.regular_drain_complete,
            reset_drain_complete = collector_boundary_admission.reset_drain_complete OR EXCLUDED.reset_drain_complete,
            safe_handoff = collector_boundary_admission.safe_handoff OR EXCLUDED.safe_handoff,
            state = CASE
                WHEN collector_boundary_admission.safe_handoff OR EXCLUDED.safe_handoff THEN 'safe_handoff'
                WHEN EXCLUDED.reset_sweep_id IS NOT NULL AND NOT EXCLUDED.reset_drain_complete THEN 'reset_running'
                WHEN EXCLUDED.reset_sweep_id IS NOT NULL THEN 'reset_draining'
                WHEN NOT EXCLUDED.regular_drain_complete THEN 'regular_draining'
                WHEN collector_boundary_admission.regular_drain_complete OR EXCLUDED.regular_drain_complete THEN 'regular_draining'
                ELSE collector_boundary_admission.state
            END,
            reset_generation = COALESCE(EXCLUDED.reset_generation, collector_boundary_admission.reset_generation),
            handoff_at = CASE WHEN collector_boundary_admission.safe_handoff OR EXCLUDED.safe_handoff THEN COALESCE(collector_boundary_admission.handoff_at, clock_timestamp()) ELSE collector_boundary_admission.handoff_at END,
            updated_at = clock_timestamp()
    `, boundary, sweepID, regularDrained, resetDrained, safeHandoff)
	if err != nil {
		return fmt.Errorf("record boundary admission: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit boundary admission: %w", err)
	}
	return nil
}

func (s *store) clearBoundarySafeHandoff(ctx context.Context, boundary time.Time) error {
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin boundary handoff validation: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		SELECT pg_advisory_xact_lock(hashtextextended($1, 0))
	`, boundaryAdmissionLockKey(boundary)); err != nil {
		return fmt.Errorf("lock boundary handoff validation: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		WITH RECURSIVE regular_lineage(job_id) AS (
			SELECT id FROM collector_jobs
			WHERE work_type = 'regular_poll' AND created_at < $1
			UNION
			SELECT child.id FROM collector_jobs AS child
			JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
			JOIN regular_lineage AS parent ON parent.job_id = parent_attempt.job_id
		), reset_lineage(job_id) AS (
			SELECT job.id FROM collector_jobs AS job
			JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
			WHERE sweep.boundary_at = $1 AND job.work_type IN ('reset_baseline','reset_profile','legacy_reset_profile')
			UNION
			SELECT child.id FROM collector_jobs AS child
			JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
			JOIN reset_lineage AS parent ON parent.job_id = parent_attempt.job_id
		)
		UPDATE collector_boundary_admission
		SET safe_handoff = false, reset_drain_complete = false,
		    state = 'reset_draining', handoff_at = NULL,
		    regular_nonterminal_count = (SELECT count(*) FROM collector_jobs AS job JOIN regular_lineage ON regular_lineage.job_id = job.id
		                                 WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')),
		    reset_nonterminal_count = (SELECT count(*) FROM collector_jobs AS job JOIN reset_lineage ON reset_lineage.job_id = job.id
		                               WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')),
		    updated_at = clock_timestamp()
		WHERE boundary_at = $1 AND safe_handoff
	`, boundary); err != nil {
		return fmt.Errorf("clear invalid boundary handoff: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit boundary handoff validation: %w", err)
	}
	return nil
}

func (s *store) regularAdmissionAllowed(ctx context.Context, now time.Time) (bool, error) {
	available, err := s.boundaryAdmissionAvailable(ctx)
	if err != nil || !available {
		// Contract fixtures predating migration 0010 retain their old direct
		// store behavior; production has the durable gate.
		return true, err
	}
	boundary := boundaryAdmissionBoundary(now)
	now = now.UTC()
	if s.contractVersion >= 4 {
		tx, txErr := s.pool.BeginTx(ctx, pgx.TxOptions{})
		if txErr != nil {
			return false, fmt.Errorf("begin regular admission transaction: %w", txErr)
		}
		defer func() { _ = tx.Rollback(ctx) }()
		if _, txErr = tx.Exec(ctx, `
			SELECT pg_advisory_xact_lock(hashtextextended($1, 0))
		`, boundaryAdmissionLockKey(boundary)); txErr != nil {
			return false, fmt.Errorf("lock regular admission: %w", txErr)
		}
		olderCount, txErr := resetJobsDrainedForBoundariesTx(ctx, tx, boundary, "<", "older")
		if txErr != nil {
			return false, txErr
		}
		if olderCount > 0 {
			if txErr = tx.Commit(ctx); txErr != nil {
				return false, fmt.Errorf("commit blocked regular admission: %w", txErr)
			}
			return false, nil
		}
		if now.Before(boundary) {
			if !now.Before(boundary.Add(-5 * time.Minute)) {
				var state string
				txErr = tx.QueryRow(ctx, `
					SELECT state FROM collector_boundary_admission
					WHERE boundary_at = $1 FOR UPDATE
				`, boundary).Scan(&state)
				if txErr == nil && state == "regular_draining" {
					if txErr = tx.Commit(ctx); txErr != nil {
						return false, fmt.Errorf("commit regular drain admission: %w", txErr)
					}
					return false, nil
				}
				if txErr != nil && txErr != pgx.ErrNoRows {
					return false, fmt.Errorf("read regular admission state: %w", txErr)
				}
				regularCount, countErr := s.regularJobsDrainedTx(ctx, tx, boundary)
				if countErr != nil {
					return false, fmt.Errorf("count regular admission lineage: %w", countErr)
				}
				if txErr = s.writeBoundaryAdmissionTx(ctx, tx, boundary, nil, regularCount, 0, false); txErr != nil {
					return false, fmt.Errorf("record pre-reset admission: %w", txErr)
				}
				if txErr = tx.Commit(ctx); txErr != nil {
					return false, fmt.Errorf("commit pre-reset admission: %w", txErr)
				}
				return false, nil
			}
			if txErr = tx.Commit(ctx); txErr != nil {
				return false, fmt.Errorf("commit open regular admission: %w", txErr)
			}
			return true, nil
		}
		var safe bool
		txErr = tx.QueryRow(ctx, `
			SELECT safe_handoff
			FROM collector_boundary_admission
			WHERE boundary_at = $1
			FOR UPDATE
		`, boundary).Scan(&safe)
		if txErr != nil && txErr != pgx.ErrNoRows {
			return false, fmt.Errorf("read boundary safe handoff: %w", txErr)
		}
		if txErr = tx.Commit(ctx); txErr != nil {
			return false, fmt.Errorf("commit boundary admission: %w", txErr)
		}
		return txErr == nil && safe, nil
	}
	if now.Before(boundary) {
		if !now.Before(boundary.Add(-5 * time.Minute)) {
			if s.contractVersion >= 4 {
				var state string
				err := s.pool.QueryRow(ctx, `
					SELECT state FROM collector_boundary_admission
					WHERE boundary_at = $1
				`, boundary).Scan(&state)
				if err == nil && state == "regular_draining" {
					return false, nil
				}
				if err != nil && err != pgx.ErrNoRows {
					return false, fmt.Errorf("read regular admission state: %w", err)
				}
			}
			if err := s.setBoundaryAdmission(ctx, boundary, nil, false, false, false); err != nil {
				return false, err
			}
			return false, nil
		}
		return true, nil
	}
	var safe bool
	err = s.pool.QueryRow(ctx, `
        SELECT safe_handoff
        FROM collector_boundary_admission
        WHERE boundary_at = $1
    `, boundary).Scan(&safe)
	if err != nil {
		if err == pgx.ErrNoRows {
			return false, nil
		}
		return false, fmt.Errorf("read boundary safe handoff: %w", err)
	}
	return safe, nil
}

// prepareBoundaryAdmission is called before regular admission. It schedules
// reset work only after the previous regular cycle has drained and records the
// explicit safe handoff after reset work reaches a terminal state.
func (s *store) prepareBoundaryAdmission(ctx context.Context, now time.Time) (int64, bool, error) {
	available, err := s.boundaryAdmissionAvailable(ctx)
	if err != nil || !available {
		return 0, false, err
	}
	boundary := boundaryAdmissionBoundary(now)
	now = now.UTC()
	if s.contractVersion >= 4 {
		return s.prepareBoundaryAdmissionV4(ctx, now)
	}
	if now.Before(boundary) {
		return 0, false, nil
	}
	var existingSweepID *int64
	var safeHandoff bool
	var admissionState string
	if s.contractVersion >= 4 {
		err = s.pool.QueryRow(ctx, `
			SELECT reset_sweep_id, safe_handoff, state
			FROM collector_boundary_admission
			WHERE boundary_at = $1
		`, boundary).Scan(&existingSweepID, &safeHandoff, &admissionState)
	} else {
		err = s.pool.QueryRow(ctx, `
			SELECT reset_sweep_id, safe_handoff
			FROM collector_boundary_admission
			WHERE boundary_at = $1
		`, boundary).Scan(&existingSweepID, &safeHandoff)
	}
	if err == nil && safeHandoff && existingSweepID != nil {
		drained, drainErr := s.resetJobsDrained(ctx, boundary)
		if drainErr != nil {
			return 0, false, drainErr
		}
		if drained {
			return *existingSweepID, false, nil
		}
		if err := s.clearBoundarySafeHandoff(ctx, boundary); err != nil {
			return 0, false, err
		}
		safeHandoff = false
		admissionState = "reset_draining"
	}
	if err != nil && err != pgx.ErrNoRows {
		return 0, false, fmt.Errorf("read boundary admission state: %w", err)
	}
	if existingSweepID != nil && (admissionState == "reset_draining" || admissionState == "safe_handoff") {
		if safeHandoff {
			return *existingSweepID, false, nil
		}
		drained, err := s.resetJobsDrained(ctx, boundary)
		if err != nil {
			return 0, false, err
		}
		if !drained {
			return *existingSweepID, false, nil
		}
		if err := s.setBoundaryAdmission(ctx, boundary, existingSweepID, true, true, true); err != nil {
			return 0, false, err
		}
		return *existingSweepID, false, nil
	}
	drained, err := s.regularJobsDrained(ctx, boundary)
	if err != nil {
		return 0, false, err
	}
	if !drained {
		return 0, false, nil
	}
	if err := s.setBoundaryAdmission(ctx, boundary, nil, true, false, false); err != nil {
		return 0, false, err
	}
	sweepID, created, err := s.scheduleResetSweep(ctx, boundary)
	if err != nil {
		return 0, false, err
	}
	if err := s.setBoundaryAdmission(ctx, boundary, &sweepID, true, false, false); err != nil {
		return 0, false, err
	}
	// The sweep roots are now admitted. Persist the separate draining state
	// after the durable count observes any pending/leased descendants.
	resetDrained, err := s.resetJobsDrained(ctx, boundary)
	if err != nil {
		return 0, false, err
	}
	if resetDrained {
		if err := s.setBoundaryAdmission(ctx, boundary, &sweepID, true, true, true); err != nil {
			return 0, false, err
		}
	} else if _, err := s.pool.Exec(ctx, `
        UPDATE collector_boundary_admission
        SET state = 'reset_draining', reset_nonterminal_count = (
            WITH RECURSIVE lineage(job_id) AS (
                SELECT id FROM collector_jobs WHERE sweep_id = $1
                UNION
                SELECT child.id FROM collector_jobs AS child
                JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
                JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
            )
            SELECT count(*) FROM collector_jobs AS job JOIN lineage ON lineage.job_id = job.id
            WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')
        ), updated_at = clock_timestamp()
        WHERE boundary_at = $2 AND safe_handoff = false
    `, sweepID, boundary); err != nil {
		return 0, false, fmt.Errorf("record reset draining state: %w", err)
	}
	return sweepID, created, nil
}

func (s *store) regularJobsDrainedTx(ctx context.Context, tx pgx.Tx, boundary time.Time) (int, error) {
	var count int
	err := tx.QueryRow(ctx, `
		WITH RECURSIVE lineage(job_id) AS (
			SELECT id FROM collector_jobs
			WHERE work_type = 'regular_poll' AND created_at < $1
			UNION
			SELECT child.id FROM collector_jobs AS child
			JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
			JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
		)
		SELECT count(*) FROM collector_jobs AS job JOIN lineage ON lineage.job_id = job.id
		WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')
	`, boundary).Scan(&count)
	return count, err
}

func (s *store) resetJobsDrainedTx(ctx context.Context, tx pgx.Tx, boundary time.Time) (int, error) {
	return resetJobsDrainedForBoundariesTx(ctx, tx, boundary, "=", "current")
}

func resetJobsDrainedForBoundariesTx(
	ctx context.Context, tx pgx.Tx, boundary time.Time, operator, label string,
) (int, error) {
	var count int
	err := tx.QueryRow(ctx, fmt.Sprintf(`
		WITH RECURSIVE lineage(job_id) AS (
			SELECT job.id FROM collector_jobs AS job
			JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
			WHERE sweep.boundary_at %s $1
			  AND job.work_type IN ('reset_baseline','reset_profile','legacy_reset_profile')
			UNION
			SELECT child.id FROM collector_jobs AS child
			JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
			JOIN lineage AS parent ON parent.job_id = parent_attempt.job_id
		)
		SELECT count(*) FROM collector_jobs AS job JOIN lineage ON lineage.job_id = job.id
		WHERE job.status IN ('pending','leased','waiting_retry','waiting_dependency')
	`, operator), boundary).Scan(&count)
	if err != nil {
		return 0, fmt.Errorf("count %s reset lineage jobs: %w", label, err)
	}
	return count, nil
}

func (s *store) writeBoundaryAdmissionTx(
	ctx context.Context,
	tx pgx.Tx,
	boundary time.Time,
	sweepID *int64,
	regularCount, resetCount int,
	safe bool,
) error {
	var resetGeneration *int
	if sweepID != nil {
		value := 1
		resetGeneration = &value
	}
	_, err := tx.Exec(ctx, `
		INSERT INTO collector_boundary_admission (
			boundary_at, reset_sweep_id, regular_drain_complete,
			reset_drain_complete, safe_handoff, state, reset_generation,
			regular_nonterminal_count, reset_nonterminal_count, handoff_at
		) VALUES ($1, $2, $3, $4, $5,
			CASE WHEN $5 THEN 'safe_handoff'
			     WHEN $2::bigint IS NOT NULL THEN 'reset_draining'
			     WHEN NOT $3 THEN 'regular_draining'
			     ELSE 'regular_open' END,
			$6, $7, $8,
			CASE WHEN $5 THEN clock_timestamp() ELSE NULL END)
		ON CONFLICT (boundary_at) DO UPDATE SET
			reset_sweep_id = COALESCE(EXCLUDED.reset_sweep_id,
				collector_boundary_admission.reset_sweep_id),
			regular_drain_complete = EXCLUDED.regular_drain_complete,
			reset_drain_complete = EXCLUDED.reset_drain_complete,
			safe_handoff = EXCLUDED.safe_handoff,
			state = EXCLUDED.state,
			reset_generation = COALESCE(EXCLUDED.reset_generation,
				collector_boundary_admission.reset_generation),
			regular_nonterminal_count = EXCLUDED.regular_nonterminal_count,
			reset_nonterminal_count = EXCLUDED.reset_nonterminal_count,
			handoff_at = CASE WHEN EXCLUDED.safe_handoff
				THEN COALESCE(collector_boundary_admission.handoff_at, clock_timestamp())
				ELSE NULL END,
			updated_at = clock_timestamp()
	`, boundary, sweepID, regularCount == 0, resetCount == 0 && sweepID != nil,
		safe, resetGeneration, regularCount, resetCount)
	return err
}

func (s *store) scheduleResetSweepLockedV4(
	ctx context.Context, tx pgx.Tx, boundary time.Time,
) (int64, bool, error) {
	var sweepID int64
	created := true
	err := tx.QueryRow(ctx, `
		INSERT INTO collector_reset_sweeps (boundary_at, membership_rule_version)
		VALUES ($1, 'active-members-v1')
		ON CONFLICT (boundary_at) DO NOTHING
		RETURNING id
	`, boundary).Scan(&sweepID)
	if errors.Is(err, pgx.ErrNoRows) {
		created = false
		if err := tx.QueryRow(ctx, `
			SELECT id FROM collector_reset_sweeps WHERE boundary_at = $1 FOR UPDATE
		`, boundary).Scan(&sweepID); err != nil {
			return 0, false, fmt.Errorf("find existing reset sweep: %w", err)
		}
	} else if err != nil {
		return 0, false, fmt.Errorf("create reset sweep: %w", err)
	}
	if created {
		if _, err := tx.Exec(ctx, `
			INSERT INTO collector_reset_sweep_members (sweep_id, player_id)
			SELECT $1, id FROM players WHERE active
		`, sweepID); err != nil {
			return 0, false, fmt.Errorf("capture reset sweep membership: %w", err)
		}
	}
	if _, err := tx.Exec(ctx, `
		UPDATE collector_reset_sweeps
		SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
		WHERE id = $1
	`, sweepID); err != nil {
		return 0, false, fmt.Errorf("capture reset sweep membership timestamp: %w", err)
	}
	if err := ensureBoundaryGeneration(ctx, tx, boundary, sweepID); err != nil {
		return 0, false, err
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO collector_reset_baseline_sweeps (
			reset_sweep_id, player_id, boundary_at, evidence_kind, state
		)
		SELECT $1, member.player_id, $2, 'paired_v2', 'pending'
		FROM collector_reset_sweep_members AS member
		WHERE member.sweep_id = $1
		ON CONFLICT (reset_sweep_id, player_id) DO NOTHING
	`, sweepID, boundary); err != nil {
		return 0, false, fmt.Errorf("create paired reset baselines: %w", err)
	}
	if _, err := tx.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, scope, player_id, normalized_tag, capacity_pool,
			priority, due_at, coalescing_key, sweep_id,
			reset_baseline_sweep_id, status
		)
		SELECT 'reset_baseline', 'player', player.id, player.normalized_tag,
			'normal', 400, $2,
			'reset-baseline:' || $1::bigint::text || ':' || player.id::text,
			$1, baseline.id, 'pending'
		FROM collector_reset_sweep_members AS member
		JOIN players AS player ON player.id = member.player_id
		JOIN collector_reset_baseline_sweeps AS baseline
		  ON baseline.reset_sweep_id = member.sweep_id
		 AND baseline.player_id = member.player_id
		WHERE member.sweep_id = $1
		  AND NOT EXISTS (
			SELECT 1 FROM collector_jobs AS existing
			WHERE existing.sweep_id = $1
			  AND existing.reset_baseline_sweep_id = baseline.id
			  AND existing.work_type = 'reset_baseline'
		  )
		ON CONFLICT DO NOTHING
	`, sweepID, boundary); err != nil {
		return 0, false, fmt.Errorf("create paired reset jobs: %w", err)
	}
	return sweepID, created, nil
}

func (s *store) prepareBoundaryAdmissionV4(ctx context.Context, now time.Time) (int64, bool, error) {
	boundary := boundaryAdmissionBoundary(now)
	tx, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, false, fmt.Errorf("begin atomic boundary admission: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err := tx.Exec(ctx, `
		SELECT pg_advisory_xact_lock(hashtextextended($1, 0))
	`, boundaryAdmissionLockKey(boundary)); err != nil {
		return 0, false, fmt.Errorf("lock atomic boundary admission: %w", err)
	}
	regularCount, err := s.regularJobsDrainedTx(ctx, tx, boundary)
	if err != nil {
		return 0, false, fmt.Errorf("count regular admission lineage: %w", err)
	}
	if now.Before(boundary) {
		if !now.Before(boundary.Add(-5 * time.Minute)) {
			if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, nil, regularCount, 0, false); err != nil {
				return 0, false, fmt.Errorf("record pre-reset admission: %w", err)
			}
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, false, fmt.Errorf("commit pre-reset admission: %w", err)
		}
		return 0, false, nil
	}
	priorResetCount, err := resetJobsDrainedForBoundariesTx(ctx, tx, boundary, "<", "older")
	if err != nil {
		return 0, false, fmt.Errorf("count older reset admission lineage: %w", err)
	}
	admission := struct {
		sweepID *int64
		safe    bool
		state   string
	}{}
	err = tx.QueryRow(ctx, `
		SELECT reset_sweep_id, safe_handoff, state
		FROM collector_boundary_admission
		WHERE boundary_at = $1
		FOR UPDATE
	`, boundary).Scan(&admission.sweepID, &admission.safe, &admission.state)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return 0, false, fmt.Errorf("read atomic admission state: %w", err)
	}
	if priorResetCount > 0 || (regularCount > 0 && admission.sweepID == nil) {
		if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, admission.sweepID, regularCount, 0, false); err != nil {
			return 0, false, fmt.Errorf("record blocked admission: %w", err)
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, false, fmt.Errorf("commit blocked admission: %w", err)
		}
		return 0, false, nil
	}
	if admission.sweepID != nil {
		resetCount, countErr := s.resetJobsDrainedTx(ctx, tx, boundary)
		if countErr != nil {
			return 0, false, fmt.Errorf("count current reset admission lineage: %w", countErr)
		}
		if resetCount == 0 && regularCount == 0 {
			if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, admission.sweepID, regularCount, 0, true); err != nil {
				return 0, false, fmt.Errorf("record safe handoff: %w", err)
			}
			if err := tx.Commit(ctx); err != nil {
				return 0, false, fmt.Errorf("commit safe handoff: %w", err)
			}
			return *admission.sweepID, false, nil
		}
		if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, admission.sweepID, regularCount, resetCount, false); err != nil {
			return 0, false, fmt.Errorf("record reset draining admission: %w", err)
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, false, fmt.Errorf("commit reset draining admission: %w", err)
		}
		return *admission.sweepID, false, nil
	}
	if regularCount > 0 {
		if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, nil, regularCount, 0, false); err != nil {
			return 0, false, fmt.Errorf("record regular draining admission: %w", err)
		}
		if err := tx.Commit(ctx); err != nil {
			return 0, false, fmt.Errorf("commit regular draining admission: %w", err)
		}
		return 0, false, nil
	}
	sweepID, created, err := s.scheduleResetSweepLockedV4(ctx, tx, boundary)
	if err != nil {
		return 0, false, err
	}
	resetCount, err := s.resetJobsDrainedTx(ctx, tx, boundary)
	if err != nil {
		return 0, false, fmt.Errorf("count admitted reset lineage: %w", err)
	}
	if err := s.writeBoundaryAdmissionTx(ctx, tx, boundary, &sweepID, 0, resetCount, resetCount == 0); err != nil {
		return 0, false, fmt.Errorf("record admitted reset: %w", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, false, fmt.Errorf("commit admitted reset: %w", err)
	}
	return sweepID, created, nil
}
