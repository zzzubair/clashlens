package collector

import (
	"context"
	"fmt"
)

func (s *store) catalogueContains(ctx context.Context, hash string) (bool, error) {
	var found bool
	if err := s.pool.QueryRow(ctx, `SELECT EXISTS (SELECT 1 FROM archive_catalogue WHERE response_hash = $1)`, hash).Scan(&found); err != nil {
		return false, fmt.Errorf("check raw-evidence catalogue: %w", err)
	}
	return found, nil
}

func (s *store) pendingContains(ctx context.Context, hash string) (bool, error) {
	var found bool
	if err := s.pool.QueryRow(ctx, `
		SELECT EXISTS (SELECT 1 FROM collector_endpoint_results
		 WHERE outcome = 'pending_remote_verification' AND response_hash = $1)
	`, hash).Scan(&found); err != nil {
		return false, fmt.Errorf("check pending raw-evidence state: %w", err)
	}
	return found, nil
}

func (s *store) cleanupEligible(ctx context.Context, hash string) (bool, error) {
	var eligible bool
	err := s.pool.QueryRow(ctx, `
        SELECT EXISTS (SELECT 1 FROM archive_catalogue AS catalogue
          WHERE catalogue.response_hash = $1
            AND NOT EXISTS (
              SELECT 1 FROM collector_observations AS observation
              JOIN python_processing_jobs AS job ON job.observation_id = observation.id
              WHERE observation.response_hash = $1
                AND job.status NOT IN ('complete', 'failed', 'cancelled')
            )
            AND NOT EXISTS (
              SELECT 1 FROM python_processing_jobs AS replay
              JOIN collector_observations AS source ON source.id = replay.replay_observation_id
              WHERE source.response_hash = $1
                AND replay.status NOT IN ('complete', 'failed', 'cancelled')
            ))
    `, hash).Scan(&eligible)
	if err != nil {
		return false, fmt.Errorf("check raw-evidence cleanup eligibility: %w", err)
	}
	return eligible, nil
}
