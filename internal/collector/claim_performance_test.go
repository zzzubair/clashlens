package collector

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

// seedProductionShapedQueue fills the collector queue with a production-shaped
// population: 12,370 players and 74,429 active jobs of which roughly 35% are
// due now (26,012 in the reported production sample). The priority-100 range
// deliberately starts with 12,369 very old future-due jobs before one due job;
// this catches indexes that bound returned rows but still scan that prefix.
// Each player has exactly
// one regular poll, matching migration 0003's invariant; the remaining depth
// is initial-collection work. Priorities follow the live production audit (100
// on-time, 150 bulk initial collection, 200 overdue) and ages and due times are
// spread over hours so the planner sees realistic selectivity.
func seedProductionShapedQueue(t *testing.T, ctx context.Context, store *store) {
	t.Helper()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		SELECT '#SEEDPLAYER' || i, true, clock_timestamp()
		FROM generate_series(1, 12370) AS i
	`); err != nil {
		t.Fatalf("seed players: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, created_at
		)
		SELECT CASE WHEN i <= 12370 THEN 'regular_poll' ELSE 'initial_collection' END,
			player.id, player.normalized_tag, 'normal',
			CASE WHEN i <= 12370 THEN 100
				WHEN i % 2 = 0 THEN 150
				ELSE 200
			END,
			CASE WHEN i < 12370
				THEN clock_timestamp() + interval '1 day'
				WHEN i = 12370
				THEN clock_timestamp() - interval '1 minute'
				WHEN i % 20 < 7
				THEN clock_timestamp() - ((i % 360) || ' minutes')::interval
				ELSE clock_timestamp() + ((i % 120) || ' minutes')::interval
			END,
			'seed-regular:' || i,
			'pending',
			CASE WHEN i <= 12370
				THEN clock_timestamp() - interval '30 days'
				ELSE clock_timestamp() - ((i % 720) || ' minutes')::interval
			END
		FROM generate_series(1, 74429) AS i
		JOIN players AS player ON player.id = ((i - 1) % 12370) + 1
	`); err != nil {
		t.Fatalf("seed collector jobs: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `ANALYZE collector_jobs`); err != nil {
		t.Fatalf("analyze collector jobs: %v", err)
	}
}

var executionTimePattern = regexp.MustCompile(`Execution Time: ([0-9.]+) ms`)

// explainClaimStatement runs EXPLAIN ANALYZE over the exact collector claim
// statement with literals substituted for the parameters and returns the plan
// text plus the measured execution time in milliseconds.
func explainClaimStatement(t *testing.T, ctx context.Context, store *store, now time.Time) (string, float64) {
	t.Helper()
	literal := strings.NewReplacer(
		"$1", "'normal'",
		"$2", "'"+now.Format(time.RFC3339Nano)+"'::timestamptz",
		"$3", "'plan-owner'",
		"$4", "'plan-token'",
		"$5", "interval '1 minute'",
	).Replace(collectorClaimStatement)
	rows, err := store.pool.Query(ctx, `EXPLAIN (ANALYZE, COSTS OFF) `+literal)
	if err != nil {
		t.Fatalf("explain collector claim: %v", err)
	}
	var plan strings.Builder
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			t.Fatalf("scan collector claim plan: %v", err)
		}
		plan.WriteString(line)
		plan.WriteString("\n")
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		t.Fatalf("read collector claim plan: %v", err)
	}
	match := executionTimePattern.FindStringSubmatch(plan.String())
	if match == nil {
		t.Fatalf("collector claim plan has no execution time:\n%s", plan.String())
	}
	millis, err := strconv.ParseFloat(match[1], 64)
	if err != nil {
		t.Fatalf("parse collector claim execution time %q: %v", match[1], err)
	}
	return plan.String(), millis
}

func TestClaimCandidatePlanAtProductionDepth(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	applyMigrationThreeToStore(t, ctx, store)
	seedProductionShapedQueue(t, ctx, store)

	plan, millis := explainClaimStatement(t, ctx, store, time.Now().UTC())
	t.Logf("claim plan at production depth:\n%s", plan)
	if strings.Contains(plan, "Seq Scan on collector_jobs") {
		t.Fatalf("collector claim plan scans the whole queue:\n%s", plan)
	}
	if !strings.Contains(plan, "collector_jobs_claim_order_v2") {
		t.Fatalf("collector claim plan does not use the indexed claim order:\n%s", plan)
	}
	if millis > 100 {
		t.Fatalf("collector claim took %.1f ms at production depth, want < 100 ms", millis)
	}

	job, err := store.claimNext(ctx, "plan-worker", normalPool, time.Now().UTC(), time.Minute, "plan-token")
	if err != nil {
		t.Fatalf("claim at production depth: %v", err)
	}
	if job == nil {
		t.Fatal("claim at production depth returned no job")
	}
}

func TestExpiredRecoveryPlanAtProductionDepthIsBounded(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	applyMigrationThreeToStore(t, ctx, store)
	seedProductionShapedQueue(t, ctx, store)
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET status = 'leased', lease_owner = 'retired-worker',
			lease_token = 'expired-' || id,
			lease_expires_at = clock_timestamp()
				- ((id % 120 + 1) || ' minutes')::interval,
			updated_at = clock_timestamp()
		WHERE id IN (
			SELECT id FROM collector_jobs
			WHERE work_type = 'initial_collection' AND status = 'pending'
			ORDER BY id LIMIT 12500
		)
	`); err != nil {
		t.Fatalf("seed expired outage backlog: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `ANALYZE collector_jobs`); err != nil {
		t.Fatalf("analyze expired collector jobs: %v", err)
	}

	rows, err := store.pool.Query(ctx, `
		EXPLAIN (ANALYZE, COSTS OFF)
		SELECT job.id, job.lease_owner, job.lease_token,
			job.lease_generation, job.result_attempt_id
		FROM collector_jobs AS job
		WHERE job.capacity_pool = 'normal'
			AND job.status = 'leased'
			AND job.lease_expires_at <= clock_timestamp()
		ORDER BY job.lease_expires_at, job.id
		LIMIT 8
		FOR UPDATE OF job SKIP LOCKED
	`)
	if err != nil {
		t.Fatalf("explain expired recovery: %v", err)
	}
	var plan strings.Builder
	for rows.Next() {
		var line string
		if err := rows.Scan(&line); err != nil {
			t.Fatalf("scan expired recovery plan: %v", err)
		}
		plan.WriteString(line)
		plan.WriteByte('\n')
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		t.Fatalf("read expired recovery plan: %v", err)
	}
	planText := plan.String()
	if strings.Contains(planText, "Seq Scan on collector_jobs") {
		t.Fatalf("expired recovery scans the outage backlog:\n%s", planText)
	}
	if !strings.Contains(planText, "collector_jobs_expired_recovery_v2") {
		t.Fatalf("expired recovery does not use its expiration-first index:\n%s", planText)
	}
	match := executionTimePattern.FindStringSubmatch(planText)
	if match == nil {
		t.Fatalf("expired recovery plan has no execution time:\n%s", planText)
	}
	millis, err := strconv.ParseFloat(match[1], 64)
	if err != nil {
		t.Fatalf("parse expired recovery execution time %q: %v", match[1], err)
	}
	if millis > 100 {
		t.Fatalf("expired recovery took %.1f ms at production depth, want < 100 ms", millis)
	}
}

func TestDirectExpiredClaimPlanSkipsStaleAttemptsAtProductionDepth(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	applyMigrationThreeToStore(t, ctx, store)
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#EXPIREDPLAN', true);
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, lease_owner, lease_token,
			lease_expires_at, created_at
		)
		SELECT 'initial_collection', player.id, player.normalized_tag,
			'normal', 300, clock_timestamp() - interval '1 day',
			'expired-plan:' || i, 'leased', 'retired-worker',
			'expired-token-' || i,
			clock_timestamp() - ((12502 - i) || ' minutes')::interval,
			clock_timestamp() - interval '30 days'
		FROM generate_series(1, 12501) AS i
		CROSS JOIN players AS player
		WHERE player.normalized_tag = '#EXPIREDPLAN';
		WITH inserted AS (
			INSERT INTO collector_attempts (
				job_id, status, started_at, attempt_number,
				lease_owner, lease_token
			)
			SELECT job.id, 'running', clock_timestamp() - interval '30 days',
				1, job.lease_owner, job.lease_token
			FROM collector_jobs AS job
			WHERE job.coalescing_key LIKE 'expired-plan:%'
			  AND job.coalescing_key <> 'expired-plan:12501'
			RETURNING id, job_id
		)
		UPDATE collector_jobs AS job
		SET result_attempt_id = inserted.id
		FROM inserted
		WHERE job.id = inserted.job_id;
		ANALYZE collector_jobs;
	`); err != nil {
		t.Fatalf("seed stale-attempt outage backlog: %v", err)
	}

	plan, millis := explainClaimStatement(t, ctx, store, time.Now().UTC())
	if strings.Contains(plan, "Seq Scan on collector_jobs") {
		t.Fatalf("direct expired claim scans the stale-attempt backlog:\n%s", plan)
	}
	if !strings.Contains(plan, "collector_jobs_expired_claim_v2") {
		t.Fatalf("direct expired claim does not use its result-attempt partial index:\n%s", plan)
	}
	if millis > 100 {
		t.Fatalf("direct expired claim took %.1f ms at production depth, want < 100 ms", millis)
	}
}

func TestClaimThroughputAtProductionDepth(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	applyMigrationThreeToStore(t, ctx, store)
	seedProductionShapedQueue(t, ctx, store)

	start := time.Now()
	claimed := 0
	for index := 0; index < 40; index++ {
		job, err := store.claimNext(
			ctx,
			"throughput-worker",
			normalPool,
			time.Now().UTC(),
			time.Minute,
			fmt.Sprintf("throughput-token-%d", index),
		)
		if err != nil {
			t.Fatalf("throughput claim %d: %v", index, err)
		}
		if job != nil {
			claimed++
		}
	}
	elapsed := time.Since(start)
	if claimed != 40 {
		t.Fatalf("claimed %d of 40 jobs at production depth", claimed)
	}
	if elapsed > time.Second {
		t.Fatalf("40 claims at production depth took %v, want < 1 s", elapsed)
	}
}

func applyMigrationThreeToStore(t *testing.T, ctx context.Context, store *store) {
	t.Helper()
	connection, err := pgx.Connect(ctx, store.pool.Config().ConnString())
	if err != nil {
		t.Fatalf("connect for migration 0003: %v", err)
	}
	defer func() { _ = connection.Close(context.Background()) }()
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))
}

func TestRecoverExpiredAttemptsIsBoundedPerClaim(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#BOUND1', true, clock_timestamp()), ('#BOUND2', true, clock_timestamp())
	`); err != nil {
		t.Fatalf("insert bounded recovery players: %v", err)
	}
	for index := 0; index < 41; index++ {
		if _, err := store.pool.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status
			)
			SELECT 'regular_poll', player.id, player.normalized_tag, 'normal', 100,
				$1::timestamptz, $2, 'pending'
			FROM players AS player
			WHERE player.normalized_tag = CASE WHEN $3::bigint % 2 = 0 THEN '#BOUND1' ELSE '#BOUND2' END
			`, now, fmt.Sprintf("bounded-recovery:%d", index), index); err != nil {
			t.Fatalf("seed bounded recovery job %d: %v", index, err)
		}
	}

	var expiredJobIDs []int64
	for index := 0; index < 40; index++ {
		job, err := store.claimNext(ctx, "lease-owner", normalPool, now, time.Minute, fmt.Sprintf("lease-token-%d", index))
		if err != nil {
			t.Fatalf("setup claim %d: %v", index, err)
		}
		if job == nil {
			t.Fatalf("setup claim %d returned no job", index)
		}
		if _, _, err := store.prepareAttempt(ctx, job, now.Add(time.Second)); err != nil {
			t.Fatalf("prepare setup attempt %d: %v", index, err)
		}
		expiredJobIDs = append(expiredJobIDs, job.id)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_expires_at = clock_timestamp() - interval '1 second'
		WHERE status = 'leased'
	`); err != nil {
		t.Fatalf("expire setup leases: %v", err)
	}

	job, err := store.claimNext(ctx, "recover-owner", normalPool, time.Now().UTC(), time.Minute, "recover-token")
	if err != nil {
		t.Fatalf("claim with expired leases: %v", err)
	}
	if job == nil {
		t.Fatal("claim with expired leases returned no job")
	}
	var recovered int
	if err := store.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM collector_jobs
		WHERE id = ANY($1) AND status = 'pending'
	`, expiredJobIDs).Scan(&recovered); err != nil {
		t.Fatalf("count recovered expired leases: %v", err)
	}
	if recovered > collectorExpiredLeaseRecoveryLimit {
		t.Fatalf("one claim recovered %d expired leases, want at most %d", recovered, collectorExpiredLeaseRecoveryLimit)
	}

	remaining := 1
	for round := 0; round < 12 && remaining > 0; round++ {
		job, err := store.claimNext(ctx, "drain-owner", normalPool, time.Now().UTC(), time.Minute, fmt.Sprintf("drain-token-%d", round))
		if err != nil {
			t.Fatalf("drain claim %d: %v", round, err)
		}
		if job == nil {
			t.Fatalf("drain claim %d returned no job", round)
		}
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*)
			FROM collector_jobs
			WHERE status = 'leased' AND lease_expires_at <= clock_timestamp()
		`).Scan(&remaining); err != nil {
			t.Fatalf("count remaining expired leases: %v", err)
		}
	}
	if remaining > 0 {
		t.Fatalf("%d expired leases still unrecovered after 12 claims", remaining)
	}
}

func TestClaimOrderKeepsUnboundedAgeFairness(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#AGE1', true, clock_timestamp())
	`); err != nil {
		t.Fatalf("insert age fairness player: %v", err)
	}
	var oldLowPriorityID, freshHighPriorityID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, created_at
		)
		SELECT 'regular_poll', player.id, player.normalized_tag, 'normal',
			100, $1::timestamptz - interval '5 minutes', 'age-fairness-old', 'pending',
			$1::timestamptz - interval '200 minutes'
		FROM players AS player WHERE player.normalized_tag = '#AGE1'
		RETURNING id
	`, now).Scan(&oldLowPriorityID); err != nil {
		t.Fatalf("insert old low-priority job: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status, created_at
		)
		SELECT 'regular_poll', player.id, player.normalized_tag, 'normal',
			400, $1::timestamptz - interval '10 minutes', 'age-fairness-fresh', 'pending',
			$1::timestamptz - interval '70 minutes'
		FROM players AS player WHERE player.normalized_tag = '#AGE1'
		RETURNING id
	`, now).Scan(&freshHighPriorityID); err != nil {
		t.Fatalf("insert high-priority job: %v", err)
	}

	job, err := store.claimNext(ctx, "age-worker", normalPool, now, time.Minute, "age-token")
	if err != nil {
		t.Fatalf("age fairness claim: %v", err)
	}
	if job == nil {
		t.Fatal("age fairness claim returned no job")
	}
	// The 200-minute-old priority-100 job (score 2100) must outrank the
	// 70-minute-old priority-400 job (score 1100). The old capped ordering
	// tied both at 1100 and picked the high-priority job instead.
	if job.id != oldLowPriorityID {
		t.Fatalf("claimed job %d, want the 200-minute-old job %d", job.id, oldLowPriorityID)
	}
	if job.id == freshHighPriorityID {
		t.Fatalf("claimed the high-priority job %d instead of the oldest-due job %d", freshHighPriorityID, oldLowPriorityID)
	}
}

func TestInactivePlayerCleanupIsThrottledNotPerClaim(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	// One active pending job keeps the throttled claims returning work while
	// the inactive jobs sit un-cancelled. It is inserted first so the claim
	// statement's oldest-first order picks it before any inactive job.
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#THROTTLE-ACTIVE', true)
	`); err != nil {
		t.Fatalf("insert throttle active player: %v", err)
	}
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority,
			due_at, coalescing_key, status
		)
		SELECT 'regular_poll', player.id, player.normalized_tag, 'normal', 100,
			$1, 'throttle-active', 'pending'
		FROM players AS player
		WHERE player.normalized_tag = '#THROTTLE-ACTIVE'
	`, now); err != nil {
		t.Fatalf("insert throttle active job: %v", err)
	}
	for _, tag := range []string{"#INACTIVE1", "#INACTIVE2", "#INACTIVE3"} {
		var playerID int64
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO players (normalized_tag, active)
			VALUES ($1, false)
			RETURNING id
		`, tag).Scan(&playerID); err != nil {
			t.Fatalf("insert inactive player %s: %v", tag, err)
		}
		if _, err := store.pool.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status
			)
			VALUES ('regular_poll', $1, $2, 'normal', 100, $3, $4, 'pending')
		`, playerID, tag, now, "throttle-inactive-"+tag); err != nil {
			t.Fatalf("insert inactive pending job for %s: %v", tag, err)
		}
	}

	first, err := store.claimNext(ctx, "throttle-worker", normalPool, now, time.Minute, "throttle-token-1")
	if err != nil {
		t.Fatalf("first throttled claim: %v", err)
	}
	if first == nil {
		t.Fatal("first claim returned no job: inactive-player cleanup ran on every claim")
	}
	second, err := store.claimNext(ctx, "throttle-worker", normalPool, now.Add(time.Second), time.Minute, "throttle-token-2")
	if err != nil {
		t.Fatalf("second throttled claim: %v", err)
	}
	if second == nil {
		t.Fatal("second claim returned no job: inactive-player cleanup ran on every claim")
	}
	if first.id == second.id {
		t.Fatalf("two throttled claims both returned job %d", first.id)
	}

	// Forcing the cleanup interval to zero restores per-claim cleanup.
	store.inactiveCleanupInterval = 0
	third, err := store.claimNext(ctx, "throttle-worker", normalPool, now.Add(2*time.Second), time.Minute, "throttle-token-3")
	if err != nil {
		t.Fatalf("forced cleanup claim: %v", err)
	}
	if third != nil {
		t.Fatalf("claim with forced cleanup claimed inactive job %d", third.id)
	}
	var status string
	var cancelReason *string
	if err := store.pool.QueryRow(ctx, `
		SELECT status, cancel_reason
		FROM collector_jobs
		WHERE coalescing_key = 'throttle-inactive-#INACTIVE3'
	`).Scan(&status, &cancelReason); err != nil {
		t.Fatalf("read forced-cleanup job state: %v", err)
	}
	if status != "cancelled" || cancelReason == nil || *cancelReason != "player_inactive" {
		t.Fatalf("forced-cleanup job state = %q (%v), want cancelled (player_inactive)", status, cancelReason)
	}
}

func TestMigrationThreeAddsCollectorClaimIndexes(t *testing.T) {
	ctx := context.Background()
	connection := migratedVersionTwoConnection(t, ctx)
	// Simulate the production upgrade path: a populated v2 database receives
	// only the missing forward migration during a normal deployment.
	var playerID int64
	if err := connection.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#MIGIDX', true)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert pre-reapply player: %v", err)
	}
	if _, err := connection.Exec(ctx, `
		INSERT INTO collector_jobs (
			work_type, player_id, normalized_tag, capacity_pool, priority, due_at,
			coalescing_key, status
		) VALUES (
			'regular_poll', $1, '#MIGIDX', 'normal', 100, clock_timestamp(),
			'migration-index', 'pending'
		)
	`, playerID); err != nil {
		t.Fatalf("insert pre-reapply job: %v", err)
	}
	applySQLFile(t, ctx, connection, filepath.Join("..", "..", "deploy", "migrations", "0003_regular_poll_dedup.sql"))

	var indexes []string
	rows, err := connection.Query(ctx, `
		SELECT indexname
		FROM pg_indexes
		WHERE tablename = 'collector_jobs'
			AND indexname IN (
				'collector_jobs_claim_order_v2',
				'collector_jobs_expired_recovery_v2',
				'collector_jobs_expired_claim_v2',
				'collector_jobs_unknown_priority_v2'
			)
		ORDER BY indexname
	`)
	if err != nil {
		t.Fatalf("read collector claim indexes: %v", err)
	}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatalf("scan collector claim index: %v", err)
		}
		indexes = append(indexes, name)
	}
	rows.Close()
	if len(indexes) != 4 {
		t.Fatalf("collector claim indexes after 0003 = %v, want claim order, expired recovery, direct expired claim, and unknown-priority catch-all", indexes)
	}
	var jobCount int
	if err := connection.QueryRow(ctx, `
		SELECT count(*) FROM collector_jobs WHERE coalescing_key = 'migration-index'
	`).Scan(&jobCount); err != nil {
		t.Fatalf("count pre-reapply job: %v", err)
	}
	if jobCount != 1 {
		t.Fatalf("pre-migration job count = %d, want 1 (0003 must preserve canonical work)", jobCount)
	}
}

// productionCollectorPriorityClasses is the live-audited set of priority
// classes present in the production collector queue, with the work type each
// class carries. The claim statement probes one indexed range per priority,
// so this set and the statement's static list MUST stay equal or a supported
// enqueue class is silently never claimed.
var productionCollectorPriorityClasses = map[int]string{
	100: "regular_poll",
	150: "initial_collection",
	200: "initial_collection",
	250: "live_refresh",
	300: "endpoint_retry",
	400: "reset_baseline",
}

// parseCollectorClaimPriorities parses the static per-priority probe list the
// claim statement is built from and fails on empty or duplicate entries.
func parseCollectorClaimPriorities(t *testing.T) map[int]struct{} {
	t.Helper()
	declared := make(map[int]struct{})
	for _, raw := range strings.Split(collectorClaimPriorities, ",") {
		value, err := strconv.Atoi(strings.Trim(raw, " ()"))
		if err != nil {
			t.Fatalf("parse declared priority %q: %v", raw, err)
		}
		if _, exists := declared[value]; exists {
			t.Fatalf("collectorClaimPriorities declares priority %d twice", value)
		}
		declared[value] = struct{}{}
	}
	if len(declared) == 0 {
		t.Fatalf("collectorClaimPriorities %q declares no priorities", collectorClaimPriorities)
	}
	return declared
}

// parseCollectorClaimExclusions parses the catch-all probe's NOT IN list and
// fails on empty or duplicate entries.
func parseCollectorClaimExclusions(t *testing.T) map[int]struct{} {
	t.Helper()
	excluded := make(map[int]struct{})
	for _, raw := range strings.Split(collectorClaimPriorityExclusions, ",") {
		value, err := strconv.Atoi(strings.TrimSpace(raw))
		if err != nil {
			t.Fatalf("parse exclusion %q: %v", raw, err)
		}
		if _, exists := excluded[value]; exists {
			t.Fatalf("collectorClaimPriorityExclusions lists priority %d twice", value)
		}
		excluded[value] = struct{}{}
	}
	return excluded
}

func TestCollectorClaimPrioritiesMatchProductionClasses(t *testing.T) {
	declared := parseCollectorClaimPriorities(t)
	if len(declared) != len(productionCollectorPriorityClasses) {
		t.Fatalf("collector claim priorities %v do not match production classes %v", declared, productionCollectorPriorityClasses)
	}
	for priority, workType := range productionCollectorPriorityClasses {
		if _, ok := declared[priority]; !ok {
			t.Fatalf("collector claim statement omits production priority %d (%s); its jobs would never be claimed", priority, workType)
		}
	}
	// The catch-all probe's exclusion list must equal the fast-path list: a
	// production class excluded from the catch-all but missing from the fast
	// probes would be stranded.
	excluded := parseCollectorClaimExclusions(t)
	if len(excluded) != len(declared) {
		t.Fatalf("collector claim exclusions %v do not match fast-path priorities %v", excluded, declared)
	}
	for priority := range declared {
		if _, ok := excluded[priority]; !ok {
			t.Fatalf("collector claim catch-all probe does not exclude fast-path priority %d", priority)
		}
	}
}

func TestCollectorClaimCandidateWindowCoversTargetParallelWorkers(t *testing.T) {
	want := "32"
	if collectorClaimCandidateLimit != want {
		t.Fatalf("collector claim candidate limit = %s, want %s target workers", collectorClaimCandidateLimit, want)
	}
	if count := strings.Count(collectorClaimStatement, "LIMIT "+want); count != 3 {
		t.Fatalf("collector claim statement has %d candidate limits, want 3", count)
	}
}

// TestEveryProductionPriorityClassIsClaimable proves that a due pending job at
// every production priority class is actually claimed, in the pool the class
// uses in production. Priority 150 is the live-audited bulk initial-collection
// class that the pre-fix statement silently stranded.
func TestEveryProductionPriorityClassIsClaimable(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()

	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#COMPAT', true)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert compatibility player: %v", err)
	}

	// The 400 class is a real paired reset-baseline job so the baseline
	// identity trigger and checks run exactly as production enqueues them.
	boundary := time.Date(2026, time.August, 4, 5, 0, 0, 0, time.UTC)
	var sweepID, baselineID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_reset_sweeps (boundary_at)
		VALUES ($1)
		RETURNING id
	`, boundary).Scan(&sweepID); err != nil {
		t.Fatalf("insert reset sweep: %v", err)
	}
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO collector_reset_baseline_sweeps (reset_sweep_id, player_id, boundary_at, evidence_kind)
		VALUES ($1, $2, $3, 'paired_v2')
		RETURNING id
	`, sweepID, playerID, boundary).Scan(&baselineID); err != nil {
		t.Fatalf("insert reset baseline: %v", err)
	}

	normalClasses := map[int]struct{}{100: {}, 150: {}, 200: {}, 300: {}, 400: {}}
	interactiveClasses := map[int]struct{}{250: {}, 300: {}}

	insertJob := func(priority int, pool string) {
		t.Helper()
		switch priority {
		case 100, 200:
			if _, err := store.pool.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, player_id, normalized_tag, capacity_pool, priority,
					due_at, coalescing_key, status
				) VALUES ('regular_poll', $1, '#COMPAT', $2, $3, $4, $5, 'pending')
			`, playerID, pool, priority, now, fmt.Sprintf("compat-%d-%s", priority, pool)); err != nil {
				t.Fatalf("insert priority %d job: %v", priority, err)
			}
		case 150:
			if _, err := store.pool.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, player_id, normalized_tag, capacity_pool, priority,
					due_at, coalescing_key, status
				) VALUES ('initial_collection', $1, '#COMPAT', $2, $3, $4, $5, 'pending')
			`, playerID, pool, priority, now, fmt.Sprintf("compat-%d-%s", priority, pool)); err != nil {
				t.Fatalf("insert priority %d job: %v", priority, err)
			}
		case 250:
			if _, err := store.pool.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, player_id, normalized_tag, capacity_pool, priority,
					due_at, coalescing_key, status
				) VALUES ('live_refresh', $1, '#COMPAT', $2, $3, $4, $5, 'pending')
			`, playerID, pool, priority, now, fmt.Sprintf("compat-%d-%s", priority, pool)); err != nil {
				t.Fatalf("insert priority %d job: %v", priority, err)
			}
		case 300:
			if _, err := store.pool.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, player_id, normalized_tag, capacity_pool, priority,
					due_at, coalescing_key, required_endpoint, status
				) VALUES ('endpoint_retry', $1, '#COMPAT', $2, $3, $4, $5, 'profile', 'pending')
			`, playerID, pool, priority, now, fmt.Sprintf("compat-%d-%s", priority, pool)); err != nil {
				t.Fatalf("insert priority %d job: %v", priority, err)
			}
		case 400:
			if _, err := store.pool.Exec(ctx, `
				INSERT INTO collector_jobs (
					work_type, scope, player_id, normalized_tag, capacity_pool,
					priority, due_at, coalescing_key, sweep_id,
					reset_baseline_sweep_id, status
				) VALUES ('reset_baseline', 'player', $1, '#COMPAT', $2,
					$3, $4, $5, $6, $7, 'pending')
			`, playerID, pool, priority, now, fmt.Sprintf("compat-%d-%s", priority, pool), sweepID, baselineID); err != nil {
				t.Fatalf("insert priority %d job: %v", priority, err)
			}
		}
	}

	for priority := range normalClasses {
		insertJob(priority, "normal")
	}
	for priority := range interactiveClasses {
		insertJob(priority, "interactive")
	}

	claimPool := func(pool capacityPool, expected map[int]struct{}, owner string) {
		t.Helper()
		claimed := make(map[int]struct{})
		for index := 0; index < len(expected); index++ {
			job, err := store.claimNext(ctx, owner, pool, now, time.Minute, fmt.Sprintf("%s-token-%d", owner, index))
			if err != nil {
				t.Fatalf("claim %s pool job %d: %v", pool, index, err)
			}
			if job == nil {
				t.Fatalf("claim %s pool job %d returned no job; a production priority class is stranded", pool, index)
			}
			var priority int
			if err := store.pool.QueryRow(ctx, `
				SELECT priority FROM collector_jobs WHERE id = $1
			`, job.id).Scan(&priority); err != nil {
				t.Fatalf("read claimed priority: %v", err)
			}
			claimed[priority] = struct{}{}
		}
		if len(claimed) != len(expected) {
			t.Fatalf("claimed priorities in %s pool = %v, want %v", pool, claimed, expected)
		}
		for priority := range expected {
			if _, ok := claimed[priority]; !ok {
				t.Fatalf("claim in %s pool never returned a priority %d job", pool, priority)
			}
		}
	}

	claimPool(normalPool, normalClasses, "compat-normal")
	claimPool(interactivePool, interactiveClasses, "compat-interactive")
}

// TestUnknownPriorityJobsAreClaimableWithAgeFairness proves that due pending
// jobs whose priority is outside the declared production classes are claimed,
// and that the catch-all path keeps the exact global score ordering: an old
// unknown-priority job (score 175 + 2000) outranks a fresh unknown-priority
// job (score 999 + 50), which outranks a fresh known priority-400 job
// (score 400 + 50). Without the catch-all probe the unknown jobs are never
// candidates at all.
func TestUnknownPriorityJobsAreClaimableWithAgeFairness(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#UNKNOWN1', true)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert unknown-priority player: %v", err)
	}
	insertUnknownJob := func(priority int, createdAgo, dueAgo time.Duration, key string) int64 {
		t.Helper()
		var jobID int64
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status, created_at
			)
			SELECT 'regular_poll', player.id, player.normalized_tag, 'normal', $1,
				$2::timestamptz - $4::interval, $3, 'pending',
				$2::timestamptz - $5::interval
			FROM players AS player WHERE player.normalized_tag = '#UNKNOWN1'
			RETURNING id
		`, priority, now, key, fmt.Sprintf("%d minutes", int(dueAgo.Minutes())), fmt.Sprintf("%d minutes", int(createdAgo.Minutes()))).Scan(&jobID); err != nil {
			t.Fatalf("insert unknown-priority job %d: %v", priority, err)
		}
		return jobID
	}
	oldUnknownID := insertUnknownJob(175, 200*time.Minute, 5*time.Minute, "unknown-old-175")
	freshUnknownID := insertUnknownJob(999, 5*time.Minute, 0, "unknown-fresh-999")
	freshKnownID := insertUnknownJob(400, 5*time.Minute, 0, "known-fresh-400")

	expected := []int64{oldUnknownID, freshUnknownID, freshKnownID}
	for index, wantID := range expected {
		job, err := store.claimNext(ctx, "unknown-worker", normalPool, now, time.Minute, fmt.Sprintf("unknown-token-%d", index))
		if err != nil {
			t.Fatalf("unknown-priority claim %d: %v", index, err)
		}
		if job == nil {
			t.Fatalf("unknown-priority claim %d returned no job; a due job is stranded", index)
		}
		if job.id != wantID {
			t.Fatalf("unknown-priority claim %d returned job %d, want job %d in score order", index, job.id, wantID)
		}
	}
}

// TestUnknownPriorityDoesNotBeatHigherScoringKnownJob pins the catch-all
// probe to the same global score: a 300-minute-old known priority-400 job
// (score 3400) must still outrank a 200-minute-old unknown priority-175 job
// (score 2175). The catch-all adds candidates; it must not distort their
// relative ordering against known priorities.
func TestUnknownPriorityDoesNotBeatHigherScoringKnownJob(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	var playerID int64
	if err := store.pool.QueryRow(ctx, `
		INSERT INTO players (normalized_tag, active)
		VALUES ('#UNKNOWN2', true)
		RETURNING id
	`).Scan(&playerID); err != nil {
		t.Fatalf("insert ordering player: %v", err)
	}
	insertJob := func(priority int, createdAgo, dueAgo time.Duration, key string) int64 {
		t.Helper()
		var jobID int64
		if err := store.pool.QueryRow(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status, created_at
			)
			SELECT 'regular_poll', player.id, player.normalized_tag, 'normal', $1,
				$2::timestamptz - $4::interval, $3, 'pending',
				$2::timestamptz - $5::interval
			FROM players AS player WHERE player.normalized_tag = '#UNKNOWN2'
			RETURNING id
		`, priority, now, key, fmt.Sprintf("%d minutes", int(dueAgo.Minutes())), fmt.Sprintf("%d minutes", int(createdAgo.Minutes()))).Scan(&jobID); err != nil {
			t.Fatalf("insert ordering job %d: %v", priority, err)
		}
		return jobID
	}
	olderKnownID := insertJob(400, 300*time.Minute, 0, "ordering-known-400")
	youngerUnknownID := insertJob(175, 200*time.Minute, 5*time.Minute, "ordering-unknown-175")

	job, err := store.claimNext(ctx, "ordering-worker", normalPool, now, time.Minute, "ordering-token")
	if err != nil {
		t.Fatalf("ordering claim: %v", err)
	}
	if job == nil {
		t.Fatal("ordering claim returned no job")
	}
	if job.id != olderKnownID {
		t.Fatalf("ordering claim returned job %d, want the higher-scoring known job %d", job.id, olderKnownID)
	}
	if job.id == youngerUnknownID {
		t.Fatalf("ordering claim returned unknown job %d ahead of the higher-scoring known job %d", youngerUnknownID, olderKnownID)
	}
}

// TestExpiredStaleAttemptJobsAreNotDirectlyClaimed proves that a job whose
// expired lease still carries a result attempt is never returned by the
// claim statement's direct expired-lease probe. With more expired jobs than
// collectorExpiredLeaseRecoveryLimit, recovery alone must resolve every
// stale attempt: each claim recovers at most the bounded window, requeues
// the job as pending with result_attempt_id cleared, and only then is the
// job claimable again. A direct expired claim would burn a fresh lease on a
// job prepareAttempt cannot fence (errLeaseLost), so it must not happen.
func TestExpiredStaleAttemptJobsAreNotDirectlyClaimed(t *testing.T) {
	ctx := context.Background()
	store := startVersionTwoStore(t, ctx)
	now := time.Now().UTC()
	if _, err := store.pool.Exec(ctx, `
		INSERT INTO players (normalized_tag, active, next_due_at)
		VALUES ('#STALE', true, clock_timestamp())
	`); err != nil {
		t.Fatalf("insert stale-attempt player: %v", err)
	}
	const staleJobs = 12 // more than collectorExpiredLeaseRecoveryLimit (8)
	for index := 0; index < staleJobs; index++ {
		if _, err := store.pool.Exec(ctx, `
			INSERT INTO collector_jobs (
				work_type, player_id, normalized_tag, capacity_pool, priority,
				due_at, coalescing_key, status
			)
			SELECT 'regular_poll', player.id, player.normalized_tag, 'normal', 100,
				$1::timestamptz, $2, 'pending'
			FROM players AS player
			WHERE player.normalized_tag = '#STALE'
		`, now, fmt.Sprintf("stale-attempt:%d", index)); err != nil {
			t.Fatalf("seed stale-attempt job %d: %v", index, err)
		}
	}
	expiredJobIDs := make([]int64, 0, staleJobs)
	for index := 0; index < staleJobs; index++ {
		job, err := store.claimNext(ctx, "stale-setup", normalPool, now, time.Minute, fmt.Sprintf("stale-setup-token-%d", index))
		if err != nil {
			t.Fatalf("setup claim %d: %v", index, err)
		}
		if job == nil {
			t.Fatalf("setup claim %d returned no job", index)
		}
		if _, _, err := store.prepareAttempt(ctx, job, now.Add(time.Second)); err != nil {
			t.Fatalf("prepare setup attempt %d: %v", index, err)
		}
		expiredJobIDs = append(expiredJobIDs, job.id)
	}
	if _, err := store.pool.Exec(ctx, `
		UPDATE collector_jobs
		SET lease_expires_at = clock_timestamp() - interval '1 second'
		WHERE status = 'leased'
	`); err != nil {
		t.Fatalf("expire setup leases: %v", err)
	}

	claimed := make(map[int64]bool, staleJobs)
	for round := 0; round < 24 && len(claimed) < staleJobs; round++ {
		job, err := store.claimNext(ctx, "stale-drain", normalPool, time.Now().UTC(), time.Minute, fmt.Sprintf("stale-drain-token-%d", round))
		if err != nil {
			t.Fatalf("drain claim %d: %v", round, err)
		}
		if job == nil {
			t.Fatalf("drain claim %d returned no job before every stale job was reclaimed", round)
		}
		// The direct expired-lease probe must never return a job that still
		// carries a stale attempt: only recoverExpiredAttemptsV2 resolves
		// those, and it runs before the claim statement in the same claim.
		var carriesStaleAttempt bool
		if err := store.pool.QueryRow(ctx, `
			SELECT result_attempt_id IS NOT NULL
			FROM collector_jobs
			WHERE id = $1
		`, job.id).Scan(&carriesStaleAttempt); err != nil {
			t.Fatalf("read claimed job attempt linkage: %v", err)
		}
		if carriesStaleAttempt {
			t.Fatalf("claim returned job %d while it still carried a stale result attempt; stale-attempt jobs must be resolved by recovery, never claimed directly", job.id)
		}
		claimed[job.id] = true
	}
	for _, jobID := range expiredJobIDs {
		var recovered int
		if err := store.pool.QueryRow(ctx, `
			SELECT count(*)
			FROM collector_attempts
			WHERE job_id = $1
				AND status = 'failed'
				AND failure_category = 'lease_expired'
		`, jobID).Scan(&recovered); err != nil {
			t.Fatalf("count recovered attempts for job %d: %v", jobID, err)
		}
		if recovered != 1 {
			t.Fatalf("job %d has %d lease_expired-failed attempts, want exactly 1: every stale attempt must be resolved by recoverExpiredAttemptsV2 before its job is claimed again", jobID, recovered)
		}
	}
}

// TestClaimStatementsDifferOnlyInStaleAttemptGuard pins the contract between
// the version-one and version-two claim statements: they must be identical
// except that the version-two statement excludes jobs that still carry a
// stale result attempt from the direct expired-lease probe. Version two
// resolves those only through recoverExpiredAttemptsV2; version one has no
// bounded recovery, so its direct expired-lease path must keep stale-attempt
// jobs claimable (prepareAttempt resumes the existing attempt there).
func TestClaimStatementsDifferOnlyInStaleAttemptGuard(t *testing.T) {
	const guard = "				AND result_attempt_id IS NULL\n"
	if strings.Count(collectorClaimStatement, guard) != 1 {
		t.Fatalf("version-two claim statement must contain the stale-attempt guard exactly once")
	}
	if collectorClaimStatementV1 != strings.Replace(collectorClaimStatement, guard, "", 1) {
		t.Fatalf("version-one claim statement must equal the version-two statement minus the stale-attempt guard")
	}
	if strings.Contains(collectorClaimStatementV1, "result_attempt_id IS NULL") {
		t.Fatalf("version-one claim statement must keep the direct expired-lease path for stale attempts")
	}
}
