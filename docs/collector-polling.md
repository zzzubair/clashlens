# Continuous player polling

The maintainer's revised #107 contract is a continuous, fair loop through tracked
eligible Legend I players, aiming for roughly 5–10 minutes between successful
polls. Five minutes is not a deadline for draining the entire population.

## Queue behavior

Reuse `players.next_due_at` and the existing regular-job queue. Select oldest-due
players first, breaking ties by player ID. Each admission moves that player to
`admission time + CLASHLENS_POLL_CYCLE` (five minutes by default). This interval
prevents a small cohort being polled continuously at maximum API speed; it is a
minimum revisit interval, not a fixed wall-clock batch or a completion deadline.
Bounded worker lanes collect profiles and battle logs concurrently.

A player with an unfinished regular job is skipped without moving their queue
position. Other players continue. The existing database uniqueness constraint
prevents duplicate active regular jobs, including across scheduler processes.
There is no global wait for the slowest player and no new round table or cursor.

Ordinary regular-poll failures are recorded as failed attempts, not successful
coverage. The next pass collects the player again rather than creating an
immediate endpoint-retry queue. Interactive, initial-collection, ranking and
reset retries retain their existing behavior. Crash recovery, pending remote
verification and storage dependency deferrals remain protected: downloaded
raw evidence is not discarded just to move the loop along.

The Refresh button and JavaScript-enabled browser reloads both submit through
the existing protected refresh endpoint, using separate interactive allowance
and duplicate-request protection. Ordinary visits and back navigation remain
read-only; loader revalidation does not create a refresh loop. Without JavaScript,
the existing Refresh form still works. Neither action changes the regular due
time or suppresses the next regular poll because the player was recently
refreshed. Normal eligibility changes still remove newly ineligible players.

The 05:00 UTC reset admission gate, raw-evidence verification, storage limits and
lease fencing remain in force. A delayed poll cannot be assumed to recover every
battle: the source battle log is bounded, so gaps must remain visible.

## Durable spool throughput and rate enforcement

Concurrent Go capacity transactions share a bounded batch under the existing
cross-process capacity flock. Record fsyncs run together, affected directories
are synced, then the combined JSON capacity ledger is atomically published and
synced. No caller succeeds before the barrier. A failed barrier blocks further
batched mutations until reconciliation or restart. Python uses the same locks,
paths and ledger format; reservations, final-byte limits and crash recovery are
not replaced by an in-memory-only counter.

A verified local duplicate releases its stripe before waiting for its capacity
reservation release. It still holds the stripe through evidence lookup and the
observation commit, preserving exclusion against archive retirement.

Normal-key permits are acquired after database/spool admission, immediately
before HTTP, and remain occupied until one second after request completion.
This prevents delayed dispatch from bunching expired permits into an API burst.
Interactive capacity is never borrowed. Slow provider responses can lower
throughput; freshness measurements, not higher key limits, determine readiness.

## Validation and live-run boundary

Test ordering, repeat passes, transient failures, independent manual refresh,
reset exclusion, shared capacity and crash recovery. For a proposed 30-minute
real-API run, measure per-player gaps between successful profile/battle-log
collections, coverage, failure categories, queue age, resource use and recovery.
Use only the authorized tracked cohort, refreshing its eligibility first.

The retired fixed-window `normal-capacity` qualification and five-minute
deadline checks describe the previous policy. Do not relabel their historical
artifacts as proof of this rolling policy or reuse them as a live-run acceptance
gate.
The pre-change Fedora fixture collector drained
25,667 ordinary requests plus 402 immediate retries in approximately 262 seconds
at at most 25 requests/second per key and 100 total, without worker errors. That
is storage/collector evidence, not real-provider or rolling-policy evidence.
The broader historical qualification remained blocked by its downstream
PostgreSQL allocation cap; this change does not raise that cap or claim it passed.

A real-traffic run needs its own explicit request/retry allowance, archive cost
ceiling, current host storage/memory headroom and tested automatic stop. Neither
merging this implementation nor the old fixture result authorizes deployment,
real traffic or the separately controlled 24-hour run.
