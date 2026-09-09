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
2. Locked run header `FOR UPDATE` with exact interval/quota equality; the
   database timestamp from this statement becomes the refreshed scheduler tick.
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
