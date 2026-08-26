package collector

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var errIncompatibleContract = errors.New("incompatible shared contract version")

type store struct {
	pool                    *pgxpool.Pool
	contractVersion         int
	maxContractVersion      int
	recoveryRetrySupported  bool
	commitTx                func(context.Context, pgx.Tx) error
	inactiveCleanupInterval time.Duration
	lastInactiveCleanupAt   atomic.Int64
	metrics                 *collectorMetrics
	archiveInstanceID       string
}

func openStore(ctx context.Context, databaseURL string, expectedContractVersion int) (*store, error) {
	return openStoreWithPoolSize(ctx, databaseURL, expectedContractVersion, defaultCollectorDatabasePoolSize)
}

// openStoreWithPoolSize opens the store with an explicitly bounded PostgreSQL
// pool. The production collector sets the bound from
// CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE; without it pgxpool silently caps at
// max(4, NumCPU) connections, which is 16 on the 16-thread host. The measured
// target profile explicitly budgets 32 collector connections.
func openStoreWithPoolSize(ctx context.Context, databaseURL string, expectedContractVersion, maxConns int) (*store, error) {
	if maxConns < 1 {
		return nil, errors.New("collector database pool size must be at least 1")
	}
	poolConfig, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		return nil, fmt.Errorf("configure PostgreSQL pool: %w", err)
	}
	poolConfig.MaxConns = int32(maxConns)
	pool, err := pgxpool.NewWithConfig(ctx, poolConfig)
	if err != nil {
		return nil, fmt.Errorf("configure PostgreSQL pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("PostgreSQL readiness: %w", err)
	}

	var actualVersion int
	if err := pool.QueryRow(ctx, `SELECT version FROM clash_lens_contract WHERE singleton`).Scan(&actualVersion); err != nil {
		pool.Close()
		return nil, fmt.Errorf("read shared contract version: %w", err)
	}
	if !supportsContractVersion(actualVersion, expectedContractVersion) {
		pool.Close()
		return nil, fmt.Errorf("%w: got %d, support through %d", errIncompatibleContract, actualVersion, expectedContractVersion)
	}
	var recoveryRetrySupported bool
	if err := pool.QueryRow(ctx, `
		SELECT EXISTS (
			SELECT 1
			FROM pg_catalog.pg_attribute AS attribute
			JOIN pg_catalog.pg_class AS relation ON relation.oid = attribute.attrelid
			JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
			WHERE namespace.nspname = current_schema()
			  AND relation.relname = 'collector_jobs'
			  AND attribute.attname = 'retry_class'
			  AND NOT attribute.attisdropped
		)
	`).Scan(&recoveryRetrySupported); err != nil {
		pool.Close()
		return nil, fmt.Errorf("detect collector recovery retry contract: %w", err)
	}
	opened := &store{
		pool:                    pool,
		contractVersion:         actualVersion,
		maxContractVersion:      expectedContractVersion,
		recoveryRetrySupported:  recoveryRetrySupported,
		inactiveCleanupInterval: inactivePlayerCleanupInterval,
	}
	opened.lastInactiveCleanupAt.Store(time.Now().UnixNano())
	return opened, nil
}

func supportsContractVersion(actualVersion, maxContractVersion int) bool {
	if maxContractVersion == 2 {
		return actualVersion == 1 || actualVersion == 2
	}
	return actualVersion == maxContractVersion
}

func (s *store) close() {
	s.pool.Close()
}

func (s *store) scheduleDueRegular(ctx context.Context, now time.Time, cycle time.Duration, batchSize int) (int, error) {
	if cycle <= 0 {
		return 0, errors.New("poll cycle must be positive")
	}
	if batchSize < 1 {
		return 0, errors.New("scheduler batch size must be positive")
	}
	if s.contractVersion < 4 {
		if allowed, err := s.regularAdmissionAllowed(ctx, now); err != nil {
			return 0, err
		} else if !allowed {
			return 0, nil
		}
	}

	cycleStart := now.Truncate(cycle)
	boundary := boundaryAdmissionBoundary(now)
	nextCycleStart := cycleStart.Add(cycle)
	cycleSeconds := int64(cycle / time.Second)
	var created int
	gateCTE, fromPlayers, wherePlayers := "", "FROM players", "WHERE active"
	if s.contractVersion >= 4 {
		gateCTE = `gate_lock AS (
			SELECT pg_advisory_xact_lock(hashtextextended($8::text, 0))
		), older_reset AS (
			WITH RECURSIVE lineage(job_id) AS (
				SELECT job.id
				FROM collector_jobs AS job
				JOIN collector_reset_sweeps AS sweep ON sweep.id = job.sweep_id
				WHERE sweep.boundary_at < $7::timestamptz
				  AND job.work_type IN ('reset_baseline','reset_profile')
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
		), gate AS (
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
			END AS allowed
			FROM gate_lock CROSS JOIN older_reset
		), `
		fromPlayers = "FROM players CROSS JOIN gate"
		wherePlayers = "WHERE gate.allowed AND active"
	}
	query := fmt.Sprintf(`
		WITH %sdue AS MATERIALIZED (
			SELECT id, normalized_tag, next_due_at
			%s
			%s AND next_due_at <= $1
			ORDER BY next_due_at, id
			FOR NO KEY UPDATE SKIP LOCKED
			LIMIT $2
		), inserted AS (
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool,
				priority, due_at, coalescing_key, status
			)
			SELECT 'regular_poll', due.id, due.normalized_tag, 'normal',
				CASE WHEN due.next_due_at <= $1 - ($5::double precision * interval '1 second')
					THEN 200 ELSE 100 END,
				$1, 'regular:' || due.id || ':' || ($3::bigint)::text, 'pending'
			FROM due
			ON CONFLICT DO NOTHING
			RETURNING 1
		), advanced AS (
			UPDATE players AS player
			SET next_due_at = $4::timestamptz + CASE
				WHEN $5::bigint < 1 THEN interval '0 seconds'
				ELSE ((player.id - 1) %% $5::bigint) * interval '1 second'
			END
			FROM due
			WHERE player.id = due.id
			RETURNING player.id
		)
		SELECT count(*) FROM inserted
		`, gateCTE, fromPlayers, wherePlayers)
	args := []any{now, batchSize, cycleStart.Unix(), nextCycleStart, cycleSeconds}
	if s.contractVersion >= 4 {
		args = append(args, now.UTC().Format(time.RFC3339), boundary.UTC().Format(time.RFC3339), boundaryAdmissionLockKey(boundary))
	}
	if err := s.pool.QueryRow(ctx, query, args...).Scan(&created); err != nil {
		return 0, fmt.Errorf("schedule due regular polls: %w", err)
	}
	return created, nil
}

func deterministicStagger(playerID int64, cycle time.Duration) time.Duration {
	cycleSeconds := int64(cycle / time.Second)
	if cycleSeconds < 1 {
		return 0
	}
	return time.Duration((playerID-1)%cycleSeconds) * time.Second
}
