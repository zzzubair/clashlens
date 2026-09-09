# Regular admission evidence (Step 9)

Bounded, disabled-by-default evidence for reconciling expected regular work
with admitted work. See `deploy/migrations/0022_step9_regular_admission_evidence.sql`
for the stable exact schema (frozen v1) and `internal/collector/admission_evidence.go`
for the scheduler transaction.

## Configuration (all-or-none)

- `CLASHLENS_REGULAR_ADMISSION_EVIDENCE_RUN_ID`
- `CLASHLENS_REGULAR_ADMISSION_EVIDENCE_START` (RFC3339 UTC)
- `CLASHLENS_REGULAR_ADMISSION_EVIDENCE_END` (RFC3339 UTC, at most 30h after start)
- `CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_EVENTS` (1–108000)
- `CLASHLENS_REGULAR_ADMISSION_EVIDENCE_MAX_SELECTED_ENTRIES` (1–5000000)

Unset all five for production (evidence disabled, scheduler SQL unchanged).
Scheduler defaults stay one second, batch 1000, five-minute cycle. The run
header is created at collector startup (`INSERT ... ON CONFLICT DO NOTHING`
then exact-equality check); a stale/colliding run ID fails startup. Disabled
receipt fields use `disabled`/`0` sentinels under the same `step9-v1`
allowlist.

## Scheduler transaction (evidence-enabled only)

Explicit `READ COMMITTED` transaction per invocation:

1. `pg_advisory_xact_lock('collector-boundary-admission')` in its own statement.
2. Locked run header `FOR UPDATE` with exact interval/quota equality, then a
   fresh `SELECT statement_timestamp()` in its own statement. The locked
   `SELECT`'s timestamp predates a run-header row-lock wait (statement start
   precedes blocking), so only the post-wait read becomes the refreshed
   scheduler tick.
3. One scheduler statement (`tick` → `gate` → `visible_due` → `due SKIP LOCKED`
   → `visibility`/`selection` → `reservation RETURNING` → `inserted`/`advanced`
   → `evidence` → final `SELECT FROM reservation`).

`reservation` admits only when quotas fit and both `tick.database_at` and
`scheduler_at` lie in `[capture_start, capture_end)`. Otherwise it commits
`capacity_exceeded` or `capture_out_of_range` with no event/root/advance.
Go commits before returning the fixed error; SQL failure rolls back
roots/advances/counters/evidence; commit failure returns
`admission evidence commit outcome unknown` without retrying with a new
invocation ID.

Production selection is unchanged (`active AND next_due_at <= scheduler_at`);
nullable profile IDs and non-eligible states are retained as observed.
`ON CONFLICT DO NOTHING` coalescing suppressions are preserved as
`selected != inserted` mismatches that fail validation.

## Evidence columns

Per event: `cycle_at` (5-minute `date_bin`), `scheduler_at` in cycle,
`database_at` in capture, `gate_allowed`, `gate_handoff_at`,
`visible_due_count/min`, `unselected_visible_due_count/min`,
`unselected_visible_past_deadline_count/min`, `selected_past_deadline_count`,
`selected_player_ids`, `selected_due_ats`, `selected_profile_version_ids`
(nulls allowed), `selected_eligibility_states` (observed, unrestricted),
`inserted_job_ids`, `advanced_count`, plus copied `capture_start/end`.

Deadlines: `effective_due = next_due_at`, or `max(next_due_at, handoff_at)`
for work due across the reset gate; `deadline = effective_due + 5 minutes`;
late iff `database_at > deadline` (equality timely). The merge fixture uses a
small fixed `max_invocation_gap` (5s in tests); Step 9 chooses its
run-specific threshold before Phase 5. Tail must extend at least 5 minutes
past the later of core end and safe handoff; missing tail/cadence classifies
`admission_visibility_unknown`, never pass.

## Observer grants

Migration 0022 grants the existing operating observer role
`clashlens_python_worker` only `SELECT` on both evidence tables and only
`SELECT (cycle_at)` on `global_rankings_intents`. No `INSERT`/`UPDATE`/`DELETE`,
no broader columns, and no grant to `PUBLIC` or `clashlens_python_api`.
Least-privilege is proved by migrated-PostgreSQL `SET ROLE` validation.

## Observer rules

- Authority is `(run_id, invocation_id)` rows plus semantic roots
  `regular:<player_id>:<cycle_epoch>` over all retained `regular_poll` rows
  including terminals; require exactly one root per selected identity.
- Join nullable profile arrays with `IS DISTINCT FROM`; scope invalid-profile
  filters to `selected.player_id IS NOT NULL` so empty placeholders do not count.
- Keep snapshot-visible vs lock-acquired sets distinct; only assert
  `0 <= unselected <= visible` and past-deadline subsets.
- Reports contain only counts/digests, never arrays/IDs/tags.
- No automatic deletion; physical cap (initial 512MiB measurement target) and
  quota stops are monitors, not cleanup.

## Storage sizing (Greptile P2 evidence)

Measured on PostgreSQL 18 (`TestAdmissionEvidenceIndexPlanAndSizes`, real
embedded PG, small fixture): one committed admission writes ~6.3KB WAL;
with one event row the evidence table is heap 8KB / index 49KB / total 64KB
(heap + pkey + `(run_id, invocation_id)` unique + `(run_id, cycle_at,
 database_at, id)` validator index) and the runs table totals 32KB.
Page-allocation floors dominate at this scale; rerun the test for the
current figures (`go test ./internal/collector -run
TestAdmissionEvidenceIndexPlanAndSizes -v`).

Finite per-run cap comes from migration CHECKs, not estimates: at most
108000 event rows per run, at most 5000000 selected entries across the run,
at most 1000 selected per invocation. Selected identities repeat across
four parallel arrays (~8B id + 8B due_at + 8B profile version + short
eligibility text per entry, order ~50B/entry), so 5M entries cost order
250MB plus per-row fixed columns (~0.5-1KB x up to 108000 rows, <= ~100MB)
plus the three indexes over the same keys: worst case is low hundreds of MB
per fully maxed-out run, reached only if the operator configures max quotas
and every invocation fills its batch.

Repeated-run implication: there is no automatic deletion (a dedicated test
asserts evidence counts only grow), so each additional run adds up to its
own quota-bounded footprint. Operators monitor
`pg_total_relation_size` of both tables against their budget; reclaiming
space is an explicit owner action (delete a run's event rows, then its
header per the `ON DELETE RESTRICT` order). No retention automation or
background deletion is added by this change.
