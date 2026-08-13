package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

var (
	errLeaseLost    = errors.New("collector job lease was lost")
	errJobCancelled = errors.New("collector job was cancelled")
)

// inactivePlayerCleanupInterval bounds how often claimNext cancels pending
// regular-poll jobs of inactive players. The claim path itself cancels a
// claimed job when its player is inactive, so this cleanup is hygiene, not
// correctness: throttling it keeps the per-claim cost independent of queue
// depth. A zero interval restores per-claim cleanup.
const inactivePlayerCleanupInterval = 10 * time.Second

// collectorExpiredLeaseRecoveryLimit bounds how many expired leases one claim
// recovers. Claiming workers drain the expired set across claims instead of
// one claim processing the whole backlog.
const collectorExpiredLeaseRecoveryLimit = 8

// inactiveCleanupDue reports whether the inactive-player cleanup may run for
// this claim, and when it may, claims the cleanup window first. A zero
// interval restores per-claim cleanup. The atomic compare-and-swap lets at
// most one concurrent claim run the cleanup per interval; the rest skip it
// because the cleanup is hygiene — the claim path itself cancels a claimed
// job when its player is inactive.
func (s *store) inactiveCleanupDue(now time.Time) bool {
	interval := s.inactiveCleanupInterval
	if interval == 0 {
		return true
	}
	last := s.lastInactiveCleanupAt.Load()
	if time.Since(time.Unix(0, last)) < interval {
		return false
	}
	return s.lastInactiveCleanupAt.CompareAndSwap(last, time.Now().UnixNano())
}

// collectorClaimPriorities enumerates every priority class that can appear
// in the collector queue: 100 on-time regular polls, 150 bulk initial
// collection (present in the live production queue), 200 overdue regular
// polls, 250 interactive refresh work, 300 endpoint retries and global
// rankings, and 400 reset-baseline work. The claim candidate scan probes one
// indexed range per priority; a priority outside this list is still claimed
// through the catch-all probe, so the list is a fast-path declaration, not a
// claimability gate. Add a new enqueue priority here (and to the partial
// catch-all index in migration 0003) so its claims stay on the fast path.
// TestCollectorClaimPrioritiesMatchProductionClasses pins this list to the
// live production audit.
const collectorClaimPriorities = "(100), (150), (200), (250), (300), (400)"

// Keep one indexed candidate available per maximum in-process request worker.
// A smaller window makes concurrent SKIP LOCKED claims repeatedly collide on
// the same prefix and return empty while a deep queue still has due work.
const collectorClaimCandidateLimit = "32"

// collectorClaimPriorityExclusions is collectorClaimPriorities rewritten as a
// plain integer list for the catch-all probe's NOT IN predicate. Derived at
// package init from the same declaration so the two cannot drift.
var collectorClaimPriorityExclusions = strings.ReplaceAll(
	strings.ReplaceAll(collectorClaimPriorities, "(", ""),
	")", "",
)

// collectorClaimStatement claims the highest-priority due job for one
// capacity pool in a single statement. The candidate CTE probes the indexed
// per-priority oldest-due pending jobs, adds a catch-all probe
// for priorities outside the declared classes (scored exactly like the known
// probes so ordering stays globally correct), adds the indexed expired-lease
// set restricted to jobs with no stale result attempt (recoverExpiredAttemptsV2
// alone resolves jobs that still carry an attempt), and locks the best
// still-available candidate with SKIP LOCKED. The candidate predicate is
// repeated at lock time so a row claimed by another worker between the probe
// and the lock is skipped, never double claimed. $1 is the capacity pool, $2
// the claim time, $3 the lease owner, $4 the lease token, and $5 the lease
// duration.
var collectorClaimStatement = `
WITH candidate AS (
	SELECT job.id
	FROM (
		SELECT claim_id.id
		FROM (VALUES ` + collectorClaimPriorities + `) AS claim_priority (priority)
		CROSS JOIN LATERAL (
			SELECT id
			FROM collector_jobs
			WHERE capacity_pool = $1
				AND status = 'pending'
				AND priority = claim_priority.priority
				AND due_at <= $2
				ORDER BY due_at, created_at, id
			LIMIT ` + collectorClaimCandidateLimit + `
		) AS claim_id
		UNION ALL
		(
			SELECT id
			FROM collector_jobs
			WHERE capacity_pool = $1
				AND status = 'pending'
				AND priority NOT IN (` + collectorClaimPriorityExclusions + `)
				AND due_at <= $2
			ORDER BY due_at, created_at, id
			LIMIT ` + collectorClaimCandidateLimit + `
		)
		UNION ALL
		(
			SELECT id
			FROM collector_jobs
			WHERE capacity_pool = $1
				AND status = 'leased'
				AND lease_expires_at <= $2
				AND result_attempt_id IS NULL
			ORDER BY lease_expires_at, due_at, created_at, id
			LIMIT ` + collectorClaimCandidateLimit + `
		)
	) AS pick
	JOIN collector_jobs AS job ON job.id = pick.id
	WHERE (job.status = 'pending' AND job.due_at <= $2)
		OR (job.status = 'leased' AND job.lease_expires_at <= $2)
	ORDER BY (
		job.priority + floor(extract(epoch FROM ($2 - job.created_at)) / 60)::integer * 10
	) DESC,
		job.due_at,
		job.id
	FOR UPDATE OF job SKIP LOCKED
	LIMIT 1
)
UPDATE collector_jobs AS job
SET status = 'leased',
	lease_owner = $3,
	lease_token = $4,
	lease_expires_at = $2 + $5,
	updated_at = $2
FROM candidate
WHERE job.id = candidate.id
RETURNING job.id,
	job.work_type,
	COALESCE(to_jsonb(job) ->> 'scope', 'player'),
	job.player_id,
	COALESCE(job.normalized_tag, ''),
	job.capacity_pool,
	job.parent_attempt_id,
	job.required_endpoint,
	job.sweep_id,
	(to_jsonb(job) ->> 'reset_baseline_sweep_id')::bigint,
	job.lease_owner,
	COALESCE((to_jsonb(job) ->> 'lease_generation')::bigint, 0),
	job.lease_token`

// collectorClaimStatementV1 is the version-one claim statement: it reclaims
// expired leases directly even when the job still carries a result attempt,
// because version-one has no bounded attempt recovery and prepareAttempt
// resumes the existing attempt instead of fencing it. The version-two
// statement (collectorClaimStatement) drops that direct path so stale
// attempts are resolved only by recoverExpiredAttemptsV2. The two statements
// must differ only in the stale-attempt guard;
// TestClaimStatementsDifferOnlyInStaleAttemptGuard pins that.
var collectorClaimStatementV1 = strings.Replace(
	collectorClaimStatement,
	"				AND result_attempt_id IS NULL\n",
	"",
	1,
)

// collectorClaimStatementRecovery is the migration-0003 claim statement.
// Recovery jobs retain their source capacity_pool for auditability, but only
// the dedicated recovery lane may claim them. The returned retry_class tells
// the worker to spend the separate recovery share of the shared interactive
// key.
// Keep the bridge statements above unchanged: databases that have not
// applied migration 0003 do not have retry_class yet.
var collectorClaimStatementRecovery = strings.Replace(
	strings.Replace(
		strings.ReplaceAll(
			collectorClaimStatement,
			"capacity_pool = $1",
			"((capacity_pool = $1 AND retry_class = 'normal') OR ($1 = 'recovery' AND retry_class = 'recovery'))",
		),
		"\t\t\t\tAND result_attempt_id IS NULL\n",
		"\t\t\t\tAND result_attempt_id IS NULL\n\t\t\t\tAND retry_class = 'recovery'\n",
		1,
	),
	"\tjob.capacity_pool,\n",
	"\tjob.capacity_pool,\n\tjob.retry_class,\n",
	1,
)

var collectorClaimStatementRecoveryOnly = strings.ReplaceAll(
	collectorClaimStatementRecovery,
	"((capacity_pool = $1 AND retry_class = 'normal') OR ($1 = 'recovery' AND retry_class = 'recovery'))",
	"($1::text = 'recovery' AND retry_class = 'recovery')",
)

var collectorClaimStatementRecoveryV1 = strings.Replace(
	collectorClaimStatementRecovery,
	"\t\t\t\tAND result_attempt_id IS NULL\n\t\t\t\tAND retry_class = 'recovery'\n",
	"",
	1,
)

type collectionJob struct {
	id                   int64
	workType             string
	scope                string
	playerID             pgtype.Int8
	normalizedTag        string
	pool                 capacityPool
	parentAttemptID      pgtype.Int8
	requiredEndpoint     pgtype.Text
	sweepID              pgtype.Int8
	resetBaselineSweepID pgtype.Int8
	leaseOwner           string
	leaseToken           string
	leaseGeneration      int64
	retryClass           string
}

func (s *store) claimNext(
	ctx context.Context,
	owner string,
	pool capacityPool,
	now time.Time,
	leaseDuration time.Duration,
	leaseToken string,
) (*collectionJob, error) {
	if owner == "" || leaseToken == "" || leaseDuration <= 0 {
		return nil, errors.New("lease owner, token, and positive duration are required")
	}

	poolStartedAt := time.Now()
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if s.metrics != nil {
		s.metrics.recordStageDuration("claim_pool_acquire", time.Since(poolStartedAt))
	}
	if err != nil {
		return nil, fmt.Errorf("begin claim transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if s.inactiveCleanupDue(now) {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_jobs AS job
			SET status = 'cancelled',
				cancel_reason = 'player_inactive',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $1
			FROM players AS player
			WHERE job.player_id = player.id
				AND job.work_type = 'regular_poll'
				AND NOT player.active
				AND (
					job.status = 'pending'
					OR (job.status = 'leased' AND job.lease_expires_at <= $1)
				)
		`, now); err != nil {
			return nil, fmt.Errorf("cancel inactive regular jobs: %w", err)
		}
	}

	if s.contractVersion >= 2 && (!s.recoveryRetrySupported || pool == recoveryPool) {
		if err := s.recoverExpiredAttemptsV2(ctx, transaction, string(pool), now, collectorExpiredLeaseRecoveryLimit); err != nil {
			return nil, err
		}
	}

	claimStatement := collectorClaimStatement
	if s.recoveryRetrySupported {
		claimStatement = collectorClaimStatementRecovery
		if pool == recoveryPool {
			claimStatement = collectorClaimStatementRecoveryOnly
		}
	}
	if s.contractVersion < 2 {
		// Version one has no bounded attempt recovery, so its statement must
		// keep the direct expired-lease path for jobs that still carry an
		// attempt: prepareAttempt resumes the existing attempt there.
		claimStatement = collectorClaimStatementV1
		if s.recoveryRetrySupported {
			claimStatement = collectorClaimStatementRecoveryV1
			if pool == recoveryPool {
				claimStatement = strings.ReplaceAll(
					collectorClaimStatementRecoveryV1,
					"((capacity_pool = $1 AND retry_class = 'normal') OR ($1 = 'recovery' AND retry_class = 'recovery'))",
					"($1::text = 'recovery' AND retry_class = 'recovery')",
				)
			}
		}
	}
	row := transaction.QueryRow(ctx, claimStatement, string(pool), now, owner, leaseToken, leaseDuration)

	var job collectionJob
	var poolName string
	var scanTargets []any
	scanTargets = []any{
		&job.id,
		&job.workType,
		&job.scope,
		&job.playerID,
		&job.normalizedTag,
		&poolName,
		&job.parentAttemptID,
		&job.requiredEndpoint,
		&job.sweepID,
		&job.resetBaselineSweepID,
		&job.leaseOwner,
		&job.leaseGeneration,
		&job.leaseToken,
	}
	if s.recoveryRetrySupported {
		// retry_class is returned directly after capacity_pool by the
		// migration-0003 claim statement.
		scanTargets = append(scanTargets[:6], append([]any{&job.retryClass}, scanTargets[6:]...)...)
	}
	if err := row.Scan(scanTargets...); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			if err := transaction.Commit(ctx); err != nil {
				return nil, fmt.Errorf("commit empty claim transaction: %w", err)
			}
			return nil, nil
		}
		return nil, fmt.Errorf("claim collector job: %w", err)
	}
	job.pool = capacityPool(poolName)
	job.leaseOwner = owner
	if s.contractVersion >= 2 {
		var generation int64
		if err := transaction.QueryRow(ctx, `
			SELECT lease_generation
			FROM collector_jobs
			WHERE id = $1
			FOR UPDATE
		`, job.id).Scan(&generation); err != nil {
			return nil, fmt.Errorf("read collector lease generation: %w", err)
		}
		command, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET lease_generation = lease_generation + 1
			WHERE id = $1
			AND lease_owner = $2
			AND lease_token = $3
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		`, job.id, owner, leaseToken)
		if err != nil {
			return nil, fmt.Errorf("advance collector lease generation: %w", err)
		}
		if command.RowsAffected() != 1 {
			return nil, errLeaseLost
		}
		job.leaseGeneration = generation + 1
	}

	if err := transaction.Commit(ctx); err != nil {
		return nil, fmt.Errorf("commit claim transaction: %w", err)
	}
	return &job, nil
}

const maxCollectorAttemptsV2 = 3

func (s *store) recoverExpiredAttemptsV2(ctx context.Context, transaction pgx.Tx, pool string, now time.Time, limit int) error {
	recoveryFilter := "job.capacity_pool = $1"
	if s.recoveryRetrySupported {
		recoveryFilter = "$1 = 'recovery'"
	}
	rows, err := transaction.Query(ctx, `
		SELECT job.id, job.lease_owner, job.lease_token, job.lease_generation,
			job.result_attempt_id
		FROM collector_jobs AS job
		WHERE `+recoveryFilter+`
			AND job.status = 'leased'
			AND job.lease_expires_at <= $2
			ORDER BY job.lease_expires_at, job.id
		LIMIT $3
		FOR UPDATE OF job SKIP LOCKED
	`, pool, now, limit)
	if err != nil {
		return fmt.Errorf("select expired collector leases: %w", err)
	}

	type expiredLease struct {
		jobID      int64
		leaseOwner string
		leaseToken string
		generation int64
		attemptID  pgtype.Int8
	}
	expired := make([]expiredLease, 0)
	for rows.Next() {
		var lease expiredLease
		if err := rows.Scan(
			&lease.jobID,
			&lease.leaseOwner,
			&lease.leaseToken,
			&lease.generation,
			&lease.attemptID,
		); err != nil {
			rows.Close()
			return fmt.Errorf("scan expired collector lease: %w", err)
		}
		expired = append(expired, lease)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("read expired collector leases: %w", err)
	}
	rows.Close()

	for _, lease := range expired {
		attemptNumber := 0
		attemptStatus := ""
		failureCategory := pgtype.Text{}
		if lease.attemptID.Valid {
			err := transaction.QueryRow(ctx, `
				SELECT status, attempt_number, failure_category
				FROM collector_attempts
				WHERE id = $1 AND job_id = $2
				FOR UPDATE
			`, lease.attemptID.Int64, lease.jobID).Scan(
				&attemptStatus,
				&attemptNumber,
				&failureCategory,
			)
			if errors.Is(err, pgx.ErrNoRows) {
				lease.attemptID = pgtype.Int8{}
			} else if err != nil {
				return fmt.Errorf("lock expired collector attempt: %w", err)
			}
		}
		if !lease.attemptID.Valid {
			if err := transaction.QueryRow(ctx, `
				SELECT COALESCE(MAX(attempt_number), 0)
				FROM collector_attempts
				WHERE job_id = $1
			`, lease.jobID).Scan(&attemptNumber); err != nil {
				return fmt.Errorf("read collector attempt bound: %w", err)
			}
		}

		if lease.attemptID.Valid && attemptStatus != "complete" {
			if _, err := transaction.Exec(ctx, `
				UPDATE collector_endpoint_results AS endpoint_result
				SET outcome = 'failed',
					next_retry_at = NULL,
					execution_token = NULL,
					failure_category = 'lease_expired'
				FROM collector_jobs AS job
				WHERE endpoint_result.attempt_id = $1
					AND job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
					AND endpoint_result.outcome <> 'observed'
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("fail expired collector endpoints: %w", err)
			}

			command, err := transaction.Exec(ctx, `
				UPDATE collector_attempts AS attempt
				SET status = 'failed',
					completed_at = $6,
					failure_category = 'lease_expired',
					lease_owner = NULL,
					lease_token = NULL
				FROM collector_jobs AS job
				WHERE attempt.id = $1
					AND attempt.job_id = $2
					AND job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
					AND attempt.status <> 'complete'
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now)
			if err != nil {
				return fmt.Errorf("terminalize expired collector attempt: %w", err)
			}
			if command.RowsAffected() != 1 {
				return errLeaseLost
			}

			if _, err := transaction.Exec(ctx, `
				INSERT INTO collector_attempt_events (
					job_id, attempt_id, event_type, from_status, to_status,
					lease_owner, lease_token, lease_generation, failure_category
				)
				SELECT job.id, $1, 'lease_expired', $2, 'failed',
					job.lease_owner, job.lease_token, job.lease_generation,
					'lease_expired'
				FROM collector_jobs AS job
				WHERE job.id = $3
					AND job.lease_owner = $4
					AND job.lease_token = $5
					AND job.lease_generation = $6
					AND job.status = 'leased'
					AND job.lease_expires_at <= $7
				ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
			`, lease.attemptID.Int64, attemptStatus, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("record expired collector attempt event: %w", err)
			}
		} else if lease.attemptID.Valid && failureCategory.Valid && failureCategory.String == "lease_expired" {
			if _, err := transaction.Exec(ctx, `
				INSERT INTO collector_attempt_events (
					job_id, attempt_id, event_type, from_status, to_status,
					lease_owner, lease_token, lease_generation, failure_category
				)
				SELECT job.id, $1, 'lease_expired', 'failed', 'failed',
					job.lease_owner, job.lease_token, job.lease_generation,
					'lease_expired'
				FROM collector_jobs AS job
				WHERE job.id = $2
					AND job.lease_owner = $3
					AND job.lease_token = $4
					AND job.lease_generation = $5
					AND job.status = 'leased'
					AND job.lease_expires_at <= $6
				ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
			`, lease.attemptID.Int64, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now); err != nil {
				return fmt.Errorf("record existing expired collector attempt event: %w", err)
			}
		}

		status := "pending"
		if attemptNumber >= maxCollectorAttemptsV2 {
			status = "failed"
		}
		recoveryFields := ""
		if s.recoveryRetrySupported {
			recoveryFields = `,
				retry_class = 'recovery',
				recovery_reason = 'collector_lease_expired',
				recovery_origin_pool = COALESCE(recovery_origin_pool, capacity_pool)`
		}
		command, err := transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = $6,
				due_at = CASE WHEN $6 = 'pending' THEN $5 ELSE due_at END,
				cancel_reason = CASE WHEN $6 = 'failed' THEN 'lease_expired' ELSE NULL END,
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				result_attempt_id = NULL,
				updated_at = $5`+recoveryFields+`
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at <= $5
		`, lease.jobID, lease.leaseOwner, lease.leaseToken, lease.generation, now, status)
		if err != nil {
			return fmt.Errorf("release expired collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
	}
	return nil
}

func (s *store) prepareAttempt(ctx context.Context, job *collectionJob, now time.Time) (int64, []endpointName, error) {
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return 0, nil, fmt.Errorf("begin attempt transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var status string
	var existingAttempt pgtype.Int8
	leaseQuery := `
		SELECT status, result_attempt_id
		FROM collector_jobs
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`
	leaseArgs := []any{job.id, job.leaseToken}
	if s.contractVersion >= 2 {
		leaseQuery = `
			SELECT status, result_attempt_id
			FROM collector_jobs
			WHERE id = $1
				AND lease_owner = $3
				AND lease_token = $2
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
			FOR UPDATE
		`
		leaseArgs = []any{job.id, job.leaseToken, job.leaseOwner, job.leaseGeneration}
	}
	if err := transaction.QueryRow(ctx, leaseQuery, leaseArgs...).Scan(&status, &existingAttempt); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return 0, nil, errLeaseLost
		}
		return 0, nil, fmt.Errorf("lock claimed job: %w", err)
	}
	if status != "leased" {
		return 0, nil, errLeaseLost
	}

	requiresActivePlayer := job.workType == "regular_poll"
	eligibilityPlayerID := job.playerID
	eligibilityRootJobID := job.id
	eligibilityAttemptID := existingAttempt
	if job.workType == "endpoint_retry" && job.parentAttemptID.Valid {
		var parentWorkType string
		if err := transaction.QueryRow(ctx, `
			WITH RECURSIVE lineage (attempt_id, job_id, work_type, player_id, parent_attempt_id, depth) AS (
				SELECT parent_attempt.id, parent_job.id, parent_job.work_type,
					parent_job.player_id, parent_job.parent_attempt_id, 1
				FROM collector_attempts AS parent_attempt
				JOIN collector_jobs AS parent_job ON parent_job.id = parent_attempt.job_id
				WHERE parent_attempt.id = $1

				UNION ALL

				SELECT parent_attempt.id, parent_job.id, parent_job.work_type,
					parent_job.player_id, parent_job.parent_attempt_id, child.depth + 1
				FROM lineage AS child
				JOIN collector_attempts AS parent_attempt ON parent_attempt.id = child.parent_attempt_id
				JOIN collector_jobs AS parent_job ON parent_job.id = parent_attempt.job_id
				WHERE child.work_type = 'endpoint_retry' AND child.depth < 32
			)
			SELECT work_type, player_id, job_id, attempt_id
			FROM lineage
			WHERE work_type <> 'endpoint_retry'
			ORDER BY depth
			LIMIT 1
		`, job.parentAttemptID.Int64).Scan(
			&parentWorkType,
			&eligibilityPlayerID,
			&eligibilityRootJobID,
			&eligibilityAttemptID,
		); err != nil {
			return 0, nil, fmt.Errorf("load endpoint retry eligibility: %w", err)
		}
		// Defensive terminal-state fence: a retry whose parent attempt or
		// required endpoint is already terminal is legacy debris that
		// predates the sibling-cancellation resolution path. Resuming it
		// would re-lock the terminal parent attempt, produce zero endpoint
		// work, and then fail resolveAttemptV2's lease fence, leaving the
		// retry leased to churn forever. Cancel only this claimed retry with
		// the attempt_terminal reason; leave the parent attempt, endpoint
		// results, observations, and sibling jobs untouched.
		var parentAttemptStatus string
		var requiredEndpointOutcome pgtype.Text
		if err := transaction.QueryRow(ctx, `
			SELECT attempt.status, endpoint_result.outcome
			FROM collector_attempts AS attempt
			LEFT JOIN collector_endpoint_results AS endpoint_result
				ON endpoint_result.attempt_id = attempt.id
				AND endpoint_result.endpoint = $2
			WHERE attempt.id = $1
			FOR UPDATE OF attempt
		`, job.parentAttemptID.Int64, job.requiredEndpoint.String).Scan(
			&parentAttemptStatus,
			&requiredEndpointOutcome,
		); err != nil {
			return 0, nil, fmt.Errorf("lock endpoint retry parent state: %w", err)
		}
		parentAttemptTerminal := parentAttemptStatus == "complete" || parentAttemptStatus == "failed"
		endpointResultTerminal := requiredEndpointOutcome.Valid &&
			(requiredEndpointOutcome.String == "observed" || requiredEndpointOutcome.String == "failed")
		if parentAttemptTerminal || endpointResultTerminal {
			cancelStatement := `
				UPDATE collector_jobs
				SET status = 'cancelled',
					cancel_reason = 'attempt_terminal',
					lease_owner = NULL,
					lease_token = NULL,
					lease_expires_at = NULL,
					updated_at = $3
				WHERE id = $1
					AND lease_token = $2
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()`
			cancelArgs := []any{job.id, job.leaseToken, now}
			if s.contractVersion >= 2 {
				cancelStatement += `
					AND lease_owner = $4
					AND lease_generation = $5`
				cancelArgs = []any{job.id, job.leaseToken, now, job.leaseOwner, job.leaseGeneration}
			}
			command, err := transaction.Exec(ctx, cancelStatement, cancelArgs...)
			if err != nil {
				return 0, nil, fmt.Errorf("cancel terminal endpoint retry: %w", err)
			}
			if command.RowsAffected() != 1 {
				return 0, nil, errLeaseLost
			}
			if err := transaction.Commit(ctx); err != nil {
				return 0, nil, fmt.Errorf("commit terminal endpoint retry cancellation: %w", err)
			}
			return 0, nil, errJobCancelled
		}
		requiresActivePlayer = parentWorkType == "regular_poll"
	}
	if requiresActivePlayer {
		var active bool
		if !eligibilityPlayerID.Valid {
			return 0, nil, errors.New("regular collection work has no player ID")
		}
		if err := transaction.QueryRow(ctx, `SELECT active FROM players WHERE id = $1`, eligibilityPlayerID).Scan(&active); err != nil {
			return 0, nil, fmt.Errorf("check regular player eligibility: %w", err)
		}
		if !active {
			attemptNeedsResolution := false
			if eligibilityAttemptID.Valid {
				var attemptStatus string
				if err := transaction.QueryRow(ctx, `
					SELECT status FROM collector_attempts WHERE id = $1 FOR UPDATE
				`, eligibilityAttemptID).Scan(&attemptStatus); err != nil {
					return 0, nil, fmt.Errorf("lock inactive collection attempt: %w", err)
				}
				switch attemptStatus {
				case "running", "incomplete":
					attemptNeedsResolution = true
				case "failed":
				case "complete":
					return 0, nil, errors.New("inactive collection attempt was already complete")
				default:
					return 0, nil, fmt.Errorf("inactive collection attempt has unknown status %q", attemptStatus)
				}
			}
			command, err := transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET status = 'cancelled', cancel_reason = 'player_inactive',
					lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = $2
				WHERE id = $1
					AND lease_token = $3
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, now, job.leaseToken)
			if err != nil {
				return 0, nil, fmt.Errorf("cancel inactive claimed job: %w", err)
			}
			if command.RowsAffected() != 1 {
				return 0, nil, errLeaseLost
			}
			if eligibilityAttemptID.Valid {
				if _, err := transaction.Exec(ctx, `
					UPDATE collector_endpoint_results
					SET outcome = 'failed',
						next_retry_at = NULL,
						failure_category = 'player_inactive'
					WHERE attempt_id = $1 AND outcome <> 'observed'
				`, eligibilityAttemptID); err != nil {
					return 0, nil, fmt.Errorf("fail inactive attempt endpoints: %w", err)
				}
				if attemptNeedsResolution {
					command, err = transaction.Exec(ctx, `
						UPDATE collector_attempts
						SET status = 'failed', completed_at = $2
						WHERE id = $1 AND status IN ('running', 'incomplete')
					`, eligibilityAttemptID, now)
					if err != nil {
						return 0, nil, fmt.Errorf("fail inactive collection attempt: %w", err)
					}
					if command.RowsAffected() != 1 {
						return 0, nil, errors.New("inactive collection attempt changed while locked")
					}
				}
				if _, err := transaction.Exec(ctx, `
					UPDATE collector_jobs
					SET status = 'cancelled',
						cancel_reason = 'player_inactive',
						lease_owner = NULL,
						lease_token = NULL,
						lease_expires_at = NULL,
						updated_at = $2
					WHERE parent_attempt_id = $1
						AND status IN ('pending', 'waiting_retry')
				`, eligibilityAttemptID, now); err != nil {
					return 0, nil, fmt.Errorf("cancel inactive sibling retries: %w", err)
				}
			}
			if eligibilityRootJobID != job.id {
				command, err = transaction.Exec(ctx, `
					UPDATE collector_jobs
					SET status = 'cancelled',
						cancel_reason = 'player_inactive',
						lease_owner = NULL,
						lease_token = NULL,
						lease_expires_at = NULL,
						updated_at = $2
					WHERE id = $1 AND status = 'waiting_retry'
				`, eligibilityRootJobID, now)
				if err != nil {
					return 0, nil, fmt.Errorf("cancel inactive root job: %w", err)
				}
				if command.RowsAffected() != 1 {
					var rootStatus string
					if err := transaction.QueryRow(ctx, `
						SELECT status FROM collector_jobs WHERE id = $1
					`, eligibilityRootJobID).Scan(&rootStatus); err != nil {
						return 0, nil, fmt.Errorf("check inactive root job: %w", err)
					}
					if rootStatus != "cancelled" {
						return 0, nil, fmt.Errorf("inactive root job has status %q", rootStatus)
					}
				}
			}
			if err := transaction.Commit(ctx); err != nil {
				return 0, nil, fmt.Errorf("commit inactive job cancellation: %w", err)
			}
			return 0, nil, errJobCancelled
		}
	}

	attemptID := existingAttempt.Int64
	if existingAttempt.Valid && s.contractVersion >= 2 && job.workType != "endpoint_retry" {
		if err := lockCurrentAttemptV2(ctx, transaction, job, existingAttempt.Int64); err != nil {
			return 0, nil, err
		}
	}
	if !existingAttempt.Valid {
		if job.workType == "endpoint_retry" && job.parentAttemptID.Valid {
			attemptID = job.parentAttemptID.Int64
		} else if s.contractVersion >= 2 {
			var previousAttemptNumber int
			if err := transaction.QueryRow(ctx, `
				SELECT COALESCE(MAX(attempt_number), 0)
				FROM collector_attempts
				WHERE job_id = $1
			`, job.id).Scan(&previousAttemptNumber); err != nil {
				return 0, nil, fmt.Errorf("read collection attempt number: %w", err)
			}
			if previousAttemptNumber >= maxCollectorAttemptsV2 {
				return 0, nil, fmt.Errorf("collector job %d exhausted attempt limit", job.id)
			}
			if err := transaction.QueryRow(ctx, `
				INSERT INTO collector_attempts (
					job_id, attempt_number, lease_owner, lease_token,
					lease_generation, status, started_at
				)
				VALUES ($1, $2, $3, $4, $5, 'running', $6)
				RETURNING id
			`, job.id, previousAttemptNumber+1, job.leaseOwner, job.leaseToken,
				job.leaseGeneration, now).Scan(&attemptID); err != nil {
				return 0, nil, fmt.Errorf("create version-two collection attempt: %w", err)
			}
		} else if err := transaction.QueryRow(ctx, `
			INSERT INTO collector_attempts (job_id, status, started_at)
			VALUES ($1, 'running', $2)
			RETURNING id
		`, job.id, now).Scan(&attemptID); err != nil {
			return 0, nil, fmt.Errorf("create collection attempt: %w", err)
		}
		var command pgconn.CommandTag
		if s.contractVersion >= 2 {
			command, err = transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET result_attempt_id = $2
				WHERE id = $1
					AND lease_owner = $3
					AND lease_token = $4
					AND lease_generation = $5
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, attemptID, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		} else {
			command, err = transaction.Exec(ctx, `
				UPDATE collector_jobs
				SET result_attempt_id = $2
				WHERE id = $1
					AND lease_token = $3
					AND status = 'leased'
					AND lease_expires_at > clock_timestamp()
			`, job.id, attemptID, job.leaseToken)
		}
		if err != nil {
			return 0, nil, fmt.Errorf("link collection attempt: %w", err)
		}
		if command.RowsAffected() != 1 {
			return 0, nil, errLeaseLost
		}
	}

	required := []endpointName{profileEndpoint, battleLogEndpoint}
	if job.workType == "legacy_reset_profile" || job.workType == "reset_profile" {
		required = []endpointName{profileEndpoint}
	}
	if job.workType == "global_player_rankings" {
		required = []endpointName{globalPlayerRankingsEndpoint}
	}
	if job.workType == "endpoint_retry" {
		required = []endpointName{endpointName(job.requiredEndpoint.String)}
	}
	for _, endpoint := range required {
		if _, err := transaction.Exec(ctx, `
			INSERT INTO collector_endpoint_results (attempt_id, endpoint, outcome)
			VALUES ($1, $2, 'pending')
			ON CONFLICT (attempt_id, endpoint) DO NOTHING
		`, attemptID, string(endpoint)); err != nil {
			return 0, nil, fmt.Errorf("create endpoint result %s: %w", endpoint, err)
		}
	}

	rows, err := transaction.Query(ctx, `
		SELECT endpoint
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
		ORDER BY endpoint
	`, attemptID)
	if err != nil {
		return 0, nil, fmt.Errorf("list required endpoint work: %w", err)
	}
	endpoints := make([]endpointName, 0, len(required))
	for rows.Next() {
		var endpoint string
		if err := rows.Scan(&endpoint); err != nil {
			rows.Close()
			return 0, nil, fmt.Errorf("scan required endpoint work: %w", err)
		}
		if job.workType == "endpoint_retry" && endpoint != job.requiredEndpoint.String {
			continue
		}
		endpoints = append(endpoints, endpointName(endpoint))
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, nil, fmt.Errorf("read required endpoint work: %w", err)
	}
	rows.Close()

	if s.contractVersion >= 2 {
		if _, err := transaction.Exec(ctx, `
			INSERT INTO collector_attempt_events (
				job_id, attempt_id, event_type, from_status, to_status,
				lease_owner, lease_token, lease_generation
			)
			SELECT job.id, $1, 'claimed', NULL, 'running',
				job.lease_owner, job.lease_token, job.lease_generation
			FROM collector_jobs AS job
			WHERE job.id = $2
				AND job.lease_owner = $3
				AND job.lease_token = $4
				AND job.lease_generation = $5
				AND job.status = 'leased'
				AND job.lease_expires_at > clock_timestamp()
				AND (job.result_attempt_id = $1 OR job.parent_attempt_id = $1)
			ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
		`, attemptID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration); err != nil {
			return 0, nil, fmt.Errorf("record claimed collection attempt event: %w", err)
		}
		if err := lockCurrentLeaseV2(ctx, transaction, job); err != nil {
			return 0, nil, err
		}
	} else if err := lockCurrentLease(ctx, transaction, job); err != nil {
		return 0, nil, err
	}

	if err := transaction.Commit(ctx); err != nil {
		return 0, nil, fmt.Errorf("commit attempt transaction: %w", err)
	}
	return attemptID, endpoints, nil
}

func (s *store) beginEndpointRequest(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	startedAt time.Time,
) (int, error) {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return 0, err
	}
	if contractVersion >= 2 {
		return s.beginEndpointRequestV2(ctx, job, attemptID, endpoint, startedAt)
	}
	var requestCount int
	err = s.pool.QueryRow(ctx, `
		UPDATE collector_endpoint_results AS endpoint_result
		SET request_count = request_count + 1,
			execution_token = $3,
			request_started_at = $4,
			key_label = NULL
		FROM collector_jobs AS job
		WHERE endpoint_result.attempt_id = $1
			AND endpoint_result.endpoint = $2
			AND job.id = $5
			AND job.lease_token = $3
			AND job.status = 'leased'
			AND job.lease_expires_at > clock_timestamp()
		RETURNING endpoint_result.request_count
	`, attemptID, string(endpoint), job.leaseToken, startedAt, job.id).Scan(&requestCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, errLeaseLost
	}
	if err != nil {
		return 0, fmt.Errorf("begin endpoint request: %w", err)
	}
	return requestCount, nil
}

func (s *store) commitObservation(
	ctx context.Context,
	job *collectionJob,
	attemptID int64,
	endpoint endpointName,
	requestCount int,
	response officialResponse,
	hash string,
	archiveReference string,
	collectorVersion string,
	keyLabel string,
	outcome string,
	nextRetryAt *time.Time,
) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.commitObservationV2(
			ctx, job, attemptID, endpoint, requestCount, response, hash,
			archiveReference, collectorVersion, keyLabel, outcome, nextRetryAt,
		)
	}
	headers, err := json.Marshal(response.headers)
	if err != nil {
		return fmt.Errorf("encode evidence headers: %w", err)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin observation transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var leaseCurrent bool
	if err := transaction.QueryRow(ctx, `
		SELECT true
		FROM collector_jobs
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
		FOR UPDATE
	`, job.id, job.leaseToken).Scan(&leaseCurrent); err != nil {
		return errLeaseLost
	}

	occurrenceKey := strconv.FormatInt(attemptID, 10) + ":" + string(endpoint) + ":" + strconv.Itoa(requestCount)
	var observationID int64
	err = transaction.QueryRow(ctx, `
		INSERT INTO collector_observations (
			occurrence_key,
			collection_job_id,
			attempt_id,
			player_id,
			normalized_tag,
			endpoint,
			request_started_at,
			response_completed_at,
			http_status,
			response_hash,
			archive_reference,
			collector_version,
			key_label,
			evidence_headers
		)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
		ON CONFLICT (occurrence_key) DO NOTHING
		RETURNING id
	`,
		occurrenceKey,
		job.id,
		attemptID,
		job.playerID,
		job.normalizedTag,
		string(endpoint),
		response.requestStartedAt,
		response.responseCompletedAt,
		response.statusCode,
		hash,
		archiveReference,
		collectorVersion,
		keyLabel,
		headers,
	).Scan(&observationID)
	if errors.Is(err, pgx.ErrNoRows) {
		err = transaction.QueryRow(ctx, `
			SELECT id FROM collector_observations WHERE occurrence_key = $1
		`, occurrenceKey).Scan(&observationID)
	}
	if err != nil {
		return fmt.Errorf("insert observation occurrence: %w", err)
	}

	if _, err := transaction.Exec(ctx, `
		INSERT INTO python_processing_jobs (observation_id)
		VALUES ($1)
		ON CONFLICT (observation_id) DO NOTHING
	`, observationID); err != nil {
		return fmt.Errorf("insert Python processing job: %w", err)
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_endpoint_results
		SET outcome = $4,
			next_retry_at = $5,
			request_started_at = $6,
			response_completed_at = $7,
			http_status = $8,
			response_hash = $9,
			archive_reference = $10,
			observation_id = $11,
			failure_category = NULL,
			key_label = $12
		WHERE attempt_id = $1
			AND endpoint = $2
			AND execution_token = $3
			AND EXISTS (
				SELECT 1
				FROM collector_jobs AS job
				WHERE job.id = $13
					AND job.lease_token = $3
					AND job.status = 'leased'
					AND job.lease_expires_at > clock_timestamp()
			)
	`,
		attemptID,
		string(endpoint),
		job.leaseToken,
		outcome,
		nextRetryAt,
		response.requestStartedAt,
		response.responseCompletedAt,
		response.statusCode,
		hash,
		archiveReference,
		observationID,
		keyLabel,
		job.id,
	)
	if err != nil {
		return fmt.Errorf("update endpoint result: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}

	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit observation transaction: %w", err)
	}
	return nil
}

func (s *store) finishAttemptV2(ctx context.Context, job *collectionJob, attemptID int64, completedAt time.Time) error {
	preflightIntent := attemptCommitIntent{
		job:       *job,
		attemptID: attemptID,
		now:       completedAt,
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin version-two completion transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	if err := lockCurrentAttemptV2(ctx, transaction, job, attemptID); err != nil {
		if errors.Is(err, errLeaseLost) {
			proofOutcome, proofErr := s.probeFreshConnection(ctx, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
				return s.proveTerminalCompletionCommit(proofCtx, connection, preflightIntent)
			})
			if proofErr == nil && proofOutcome == commitProofCommitted {
				return nil
			}
		}
		return err
	}
	var rootJobID int64
	if err := transaction.QueryRow(ctx, `
		SELECT job_id
		FROM collector_attempts
		WHERE id = $1
		FOR UPDATE
	`, attemptID).Scan(&rootJobID); err != nil {
		return fmt.Errorf("lock version-two completion attempt: %w", err)
	}
	preCommitSnapshot, snapshotPresent, err := readAttemptResolutionSnapshot(
		ctx, transaction, job.id, attemptID, rootJobID,
	)
	if err != nil {
		return fmt.Errorf("read version-two completion state: %w", err)
	}
	if !snapshotPresent {
		return errors.New("version-two completion state is missing")
	}
	commitIntent := attemptCommitIntent{
		job:               *job,
		attemptID:         attemptID,
		rootJobID:         rootJobID,
		now:               completedAt,
		completionOnly:    true,
		preCommitSnapshot: &preCommitSnapshot,
	}
	var incomplete int
	if err := transaction.QueryRow(ctx, `
		SELECT count(*)
		FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
	`, attemptID).Scan(&incomplete); err != nil {
		return fmt.Errorf("count version-two incomplete endpoints: %w", err)
	}

	if incomplete > 0 {
		command, err := transaction.Exec(ctx, `
			UPDATE collector_attempts AS attempt
			SET status = 'incomplete'
			FROM collector_jobs AS current_job
			WHERE attempt.id = $1
				AND attempt.status IN ('running', 'incomplete')
				AND current_job.id = $2
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		`, attemptID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
		if err != nil {
			return fmt.Errorf("mark version-two attempt incomplete: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
		if err := s.commitTransaction(ctx, transaction); err != nil {
			return fmt.Errorf("commit version-two incomplete attempt: %w", err)
		}
		return nil
	}

	command, err := transaction.Exec(ctx, `
		UPDATE collector_attempts AS attempt
		SET status = 'complete', completed_at = $2
		FROM collector_jobs AS current_job
		WHERE attempt.id = $1
			AND attempt.job_id = $3
			AND attempt.status IN ('running', 'incomplete')
			AND current_job.id = $4
			AND current_job.lease_owner = $5
			AND current_job.lease_token = $6
			AND current_job.lease_generation = $7
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
	`, attemptID, completedAt, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration)
	if err != nil {
		return fmt.Errorf("complete version-two attempt: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}

	if _, err := transaction.Exec(ctx, `
		UPDATE collector_reset_baseline_sweeps AS baseline
		SET state = 'complete', completed_at = $2
		FROM collector_jobs AS root_job
		JOIN collector_jobs AS current_job ON current_job.id = $3
		WHERE root_job.id = $1
			AND baseline.id = root_job.reset_baseline_sweep_id
			AND baseline.evidence_kind = 'paired_v2'
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $7 OR current_job.parent_attempt_id = $7)
	`, rootJobID, completedAt, job.id, job.leaseOwner, job.leaseToken,
		job.leaseGeneration, attemptID); err != nil {
		return fmt.Errorf("complete version-two reset baseline: %w", err)
	}
	if _, err := transaction.Exec(ctx, `
		INSERT INTO collector_attempt_events (
			job_id, attempt_id, event_type, from_status, to_status,
			lease_owner, lease_token, lease_generation
		)
		SELECT current_job.id, $1, 'completed', $2, 'complete',
			current_job.lease_owner, current_job.lease_token, current_job.lease_generation
		FROM collector_jobs AS current_job
		WHERE current_job.id = $3
			AND current_job.lease_owner = $4
			AND current_job.lease_token = $5
			AND current_job.lease_generation = $6
			AND current_job.status = 'leased'
			AND current_job.lease_expires_at > clock_timestamp()
			AND (current_job.result_attempt_id = $1 OR current_job.parent_attempt_id = $1)
		ON CONFLICT (attempt_id, event_type, lease_generation) DO NOTHING
	`, attemptID, preCommitSnapshot.attempt.status, job.id, job.leaseOwner,
		job.leaseToken, job.leaseGeneration); err != nil {
		return fmt.Errorf("record completed version-two attempt event: %w", err)
	}

	if rootJobID == job.id {
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'complete',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $5
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
				AND (result_attempt_id = $6 OR parent_attempt_id = $6)
		`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two collector job: %w", err)
		}
	} else {
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs AS root_job
			SET status = 'complete', updated_at = $6
			FROM collector_jobs AS current_job
			WHERE root_job.id = $1
				AND current_job.id = $2
				AND root_job.status IN ('waiting_retry', 'leased')
				AND current_job.lease_owner = $3
				AND current_job.lease_token = $4
				AND current_job.lease_generation = $5
				AND current_job.status = 'leased'
				AND current_job.lease_expires_at > clock_timestamp()
				AND (current_job.result_attempt_id = $7 OR current_job.parent_attempt_id = $7)
		`, rootJobID, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two root collector job: %w", err)
		}
		if command.RowsAffected() != 1 {
			return errLeaseLost
		}
		command, err = transaction.Exec(ctx, `
			UPDATE collector_jobs
			SET status = 'complete',
				lease_owner = NULL,
				lease_token = NULL,
				lease_expires_at = NULL,
				updated_at = $5
			WHERE id = $1
				AND lease_owner = $2
				AND lease_token = $3
				AND lease_generation = $4
				AND status = 'leased'
				AND lease_expires_at > clock_timestamp()
				AND (result_attempt_id = $6 OR parent_attempt_id = $6)
			`, job.id, job.leaseOwner, job.leaseToken, job.leaseGeneration, completedAt, attemptID)
		if err != nil {
			return fmt.Errorf("complete version-two retry collector job: %w", err)
		}
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if err := s.commitTransaction(ctx, transaction); err != nil {
		return s.reconcileCommitError(ctx, err, func(proofCtx context.Context, connection *pgx.Conn) (commitProofOutcome, error) {
			return s.proveTerminalCompletionCommit(proofCtx, connection, commitIntent)
		})
	}
	return nil
}

func (s *store) finishAttempt(ctx context.Context, job *collectionJob, attemptID int64, completedAt time.Time) error {
	contractVersion, err := s.currentContractVersion(ctx)
	if err != nil {
		return err
	}
	if contractVersion >= 2 {
		return s.finishAttemptV2(ctx, job, attemptID, completedAt)
	}
	transaction, err := s.pool.BeginTx(ctx, pgx.TxOptions{})
	if err != nil {
		return fmt.Errorf("begin completion transaction: %w", err)
	}
	defer func() { _ = transaction.Rollback(ctx) }()

	var incomplete int
	if err := transaction.QueryRow(ctx, `
		SELECT count(*) FROM collector_endpoint_results
		WHERE attempt_id = $1 AND outcome <> 'observed'
	`, attemptID).Scan(&incomplete); err != nil {
		return fmt.Errorf("count incomplete endpoints: %w", err)
	}
	if incomplete > 0 {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_attempts SET status = 'incomplete' WHERE id = $1
		`, attemptID); err != nil {
			return fmt.Errorf("mark attempt incomplete: %w", err)
		}
		return transaction.Commit(ctx)
	}

	if _, err := transaction.Exec(ctx, `
		UPDATE collector_attempts
		SET status = 'complete', completed_at = $2
		WHERE id = $1
	`, attemptID, completedAt); err != nil {
		return fmt.Errorf("complete attempt: %w", err)
	}
	command, err := transaction.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'complete',
			lease_owner = NULL,
			lease_token = NULL,
			lease_expires_at = NULL,
			updated_at = $3
		WHERE id = $1
			AND lease_token = $2
			AND status = 'leased'
			AND lease_expires_at > clock_timestamp()
	`, job.id, job.leaseToken, completedAt)
	if err != nil {
		return fmt.Errorf("complete collector job: %w", err)
	}
	if command.RowsAffected() != 1 {
		return errLeaseLost
	}
	if job.resetBaselineSweepID.Valid {
		if _, err := transaction.Exec(ctx, `
			UPDATE collector_reset_baseline_sweeps
			SET state = 'complete', completed_at = $2
			WHERE id = $1 AND evidence_kind = 'paired_v2'
		`, job.resetBaselineSweepID, completedAt); err != nil {
			return fmt.Errorf("complete reset baseline sweep: %w", err)
		}
	}
	if err := transaction.Commit(ctx); err != nil {
		return fmt.Errorf("commit completion transaction: %w", err)
	}
	return nil
}
