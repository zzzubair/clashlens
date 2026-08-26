package collector

import (
	"context"
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
        SELECT count(*)
        FROM collector_jobs
        WHERE work_type = 'regular_poll'
          AND status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
          AND created_at < $1
    `, boundary).Scan(&count); err != nil {
		return false, fmt.Errorf("count regular jobs before reset: %w", err)
	}
	return count == 0, nil
}

func (s *store) resetJobsDrained(ctx context.Context, boundary time.Time) (bool, error) {
	var count int
	if err := s.pool.QueryRow(ctx, `
        SELECT count(*)
        FROM collector_jobs AS job
        JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
        WHERE sweep.boundary_at = $1
          AND job.work_type IN ('reset_baseline', 'reset_profile')
          AND job.status IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency')
    `, boundary).Scan(&count); err != nil {
		return false, fmt.Errorf("count reset jobs: %w", err)
	}
	return count == 0, nil
}

func (s *store) setBoundaryAdmission(
	ctx context.Context,
	boundary time.Time,
	sweepID *int64,
	regularDrained, resetDrained, safeHandoff bool,
) error {
	_, err := s.pool.Exec(ctx, `
        INSERT INTO collector_boundary_admission (
            boundary_at, reset_sweep_id, regular_drain_complete,
            reset_drain_complete, safe_handoff
        ) VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (boundary_at) DO UPDATE SET
            reset_sweep_id = COALESCE(EXCLUDED.reset_sweep_id,
                                      collector_boundary_admission.reset_sweep_id),
            regular_drain_complete = collector_boundary_admission.regular_drain_complete OR EXCLUDED.regular_drain_complete,
            reset_drain_complete = collector_boundary_admission.reset_drain_complete OR EXCLUDED.reset_drain_complete,
            safe_handoff = collector_boundary_admission.safe_handoff OR EXCLUDED.safe_handoff,
            updated_at = clock_timestamp()
    `, boundary, sweepID, regularDrained, resetDrained, safeHandoff)
	if err != nil {
		return fmt.Errorf("record boundary admission: %w", err)
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
	if now.Before(boundary) {
		if !now.Before(boundary.Add(-5 * time.Minute)) {
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
	if now.Before(boundary) {
		return 0, false, nil
	}
	var existingSweepID *int64
	var safeHandoff bool
	err = s.pool.QueryRow(ctx, `
		SELECT reset_sweep_id, safe_handoff
		FROM collector_boundary_admission
		WHERE boundary_at = $1
	`, boundary).Scan(&existingSweepID, &safeHandoff)
	if err == nil && safeHandoff && existingSweepID != nil {
		return *existingSweepID, false, nil
	}
	if err != nil && err != pgx.ErrNoRows {
		return 0, false, fmt.Errorf("read boundary admission state: %w", err)
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
	resetDrained, err := s.resetJobsDrained(ctx, boundary)
	if err != nil {
		return 0, false, err
	}
	if resetDrained {
		if err := s.setBoundaryAdmission(ctx, boundary, &sweepID, true, true, true); err != nil {
			return 0, false, err
		}
	}
	return sweepID, created, nil
}
