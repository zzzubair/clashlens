# Step 6 ingestion performance evidence

This is the acceptance record for issue 39, Step 6 and issue 38. It covers the
implementation boundary, a controlled before/after comparison, three complete
five-minute cycles on the target host, and the production measurement seams
needed to detect a regression.

## Outcome

The target-host profile durably collected and fully processed every response
in three consecutive cycles. All cycles started with 12,500 due players and no
pre-enqueued regular or Python work. The normal one-second scheduler ran inside
the timed window. Each player emitted a profile and battle-log request; a
controlled retry pattern added 24 HTTP 503 responses, so every run archived,
recorded, and processed 25,024 observations.

| Cycle | Collector complete | Collector rate* | Python complete | Python rate* | Final state** |
|---|---:|---:|---:|---:|---|
| 17 | 211.485 s | 118.2/s | 211.485 s | 118.2/s | `25024|0|25024|0|0|0` |
| 18 | 247.529 s | 101.0/s | 247.529 s | 101.0/s | `25024|0|25024|0|0|0` |
| 19 | 210.702 s | 118.7/s | 210.702 s | 118.7/s | `25024|0|25024|0|0|0` |

\* Rates use the required 25,000 observations, not the extra retry responses.

\** Final state is observations, active collector jobs, completed Python jobs,
active Python jobs, due players, and oldest-due seconds. Queue depth and
oldest-due age therefore returned to zero in every cycle.

The slowest side still had 52.471 seconds of the five-minute deadline left.
Both paths exceeded the 100/s headroom requirement in all three cycles.

## Host and realistic workload

Measurements ran on `rogue`, the deployment ceiling in `AGENTS.md`: Fedora 44,
AMD Ryzen 9 5900HS (8 cores/16 threads), 16 GB RAM, 8 GB swap, and NVMe
storage. The isolated Podman profile was:

- PostgreSQL: 8 CPUs, 8 GB memory, 1 GB shared memory, 100 connections;
- collector: four normal keys at 30 requests/second/key, eight workers/key,
  32 database connections, 4 CPUs, and 1 GB memory;
- Python: one process with 12 bounded lanes, 12 database connections, 12
  archive connections, 4 CPUs, and 2 GB memory;
- archive: disk-backed MinIO on a named NVMe volume, 2 CPUs, and 1 GB memory;
- normal scheduling, retries, immutable archiving, verified Python reads,
  production migrations, parsing, and domain persistence enabled;
- global rankings and reset-baseline work excluded because they are not part
  of a regular 12,500-player cycle.

The controlled provider and MinIO ran as separate services on the same private
network so external availability did not make three long runs flaky. The
provider delay distribution was deliberately taken from the live measurements.
Across all three runs, profile p50/p95/p99 was approximately 162/441/1,290 ms
and battle log was 169/437/1,160 ms. One request per approximately 997 tags
returned 503 before succeeding: 12 profile retries and 12 battle-log retries
per cycle. This exercises 32-lane occupancy and retry headroom with the recorded
provider tail instead of a zero-delay happy path. MinIO performed real
filesystem-backed S3 operations on the host NVMe rather than keeping objects in
the load generator's memory.

Response bodies were production-shaped. Every successful profile and battle
body was unique per player; the shared retry body was content-addressed once.
The collector exercised rate admission, SHA-256 addressing, conditional
immutable PUT, archive-before-observation, fencing, observation/Python-job
creation, and retry resolution. Python performed verified GET/SHA-256,
production parsing, and all domain writes. Battle payloads had eight rows.

Each cycle ended with 25,024 archive references and 25,001 distinct response
hashes in PostgreSQL, plus exactly 25,001 objects under MinIO's content-addressed
prefix. After stopping both applications, the harness restarted MinIO and
recounted the objects. The count remained 25,001 and a sampled object's bytes
still hashed to its key in every cycle. This is the durable archive proof, not
just an API acknowledgement or an in-process object count.

## Controlled before/after boundary

The before run used pinned base `f969415c7923a7a864f3d254a87113aae3b188da`
with timing-only instrumentation. It used the same host, resource limits,
provider delay/retry distribution, normal scheduler, empty starting queues,
archive service, and 12,500 due players.

The pinned base failed to drain a normal scheduled cycle by the 300-second
deadline. It had scheduled 23,442 regular jobs for 12,500 players because it
lacked the one-active-poll invariant, recorded 37,912 observations, and still
had 4,502 collector jobs active with an oldest-due age of 107.755 seconds.
Although all 25,000 required player/endpoint pairs appeared among those rows,
the queue did not return to baseline. Its serial Python worker completed only
1,954 jobs by the 300-second sample (6.51/s) and had 35,958 active. At that rate
25,000 jobs take about 64 minutes. The after profile completed every one of the
25,024 rows and returned both queues to zero within 247.529 seconds.

Collector percentiles below are production histogram upper bounds from the
controlled base and cycle 18. Cycle 18 is the conservative representative
after run because it had the slowest completion. `GET verify` after is
only the 23 conditional-create conflicts for repeated retry bodies; a new
object no longer receives a synchronous success-path readback.

| Collector stage | Before p50/p95/p99 | After p50/p95/p99 |
|---|---:|---:|
| dependency readiness | <=1/10/50 ms | <=0.1/0.1/0.1 ms |
| claim pool acquire | <=0.1/5/25 ms | <=0.1/10/50 ms |
| claim transaction | <=25/100/100 ms | <=5/250/5,000 ms |
| prepare attempt | <=5/25/50 ms | <=5/50/100 ms |
| request-start transaction | <=5/25/100 ms | <=2.5/50/100 ms |
| official profile API | <=250/500/2,500 ms | <=250/500/2,500 ms |
| official battle-log API | <=250/500/2,500 ms | <=250/500/2,500 ms |
| archive HEAD | <=1/2.5/2.5 ms | <=1/2.5/2.5 ms |
| archive PUT | <=5/10/25 ms | <=5/10/10 ms |
| archive GET + verify | <=1/2.5/2.5 ms (37,942 calls) | <=1/1/2.5 ms (23 calls) |
| complete archive write | <=5/10/25 ms | <=5/10/25 ms |
| observation pool acquire | <=0.1/5/25 ms | <=0.1/5/25 ms |
| observation job-row lock | <=0.25/1/5 ms | <=0.25/10/50 ms |
| observation transaction | <=5/50/100 ms | <=5/100/100 ms |
| successful-path commit proof | <=1/10/25 ms (37,942 calls) | not called |
| attempt completion | <=25/50/100 ms | <=10/100/250 ms |
| complete player job | <=500/1,000/2,500 ms | <=500/1,000/2,500 ms |
| normal scheduler | <=25/50/1,000 ms | <=2.5/500/2,500 ms |

Successful commits no longer pay for a proof read. Proof is retained only for
an ambiguous commit error, has its own `ambiguous_commit_proof` production
stage, and is covered by the injected commit-ambiguity idempotency test. No
ambiguous commit occurred in the three acceptance cycles, so an after
percentile would be misleading.

Python before values are exact timings from the controlled durable-archive run.
After values are bounded production histogram upper bounds from cycle 18. The
base archive pool did not expose timing; its pool-only value comes from a
separate 100-job timing-instrumented sample of the same pinned worker.

| Python stage | Before p50/p95/p99 | After p50/p95/p99 |
|---|---:|---:|
| claim + broad recovery | 56.300/928.157/991.236 ms | <=25/100/100 ms |
| database pool acquire | 0.016/0.025/0.032 ms | <=0.1/0.1/0.1 ms |
| archive pool acquire | 0.023/0.031/0.046 ms | <=0.1/0.1/0.5 ms |
| archive GET + verify | 1.580/2.238/2.776 ms | <=2.5/5/10 ms |
| profile parse | 0.096/0.153/0.175 ms | <=0.1/0.25/0.25 ms |
| battle-log parse (8 rows) | 0.303/0.478/0.542 ms | <=0.5/0.5/1 ms |
| profile domain transaction | 5.185/10.482/31.639 ms | <=50/250/250 ms |
| battle domain transaction (8 rows) | 17.159/47.148/63.607 ms | <=50/250/250 ms |
| lease renewal | 2.281/5.202/18.179 ms (2/job) | <=10/50/100 ms (1/job) |
| bounded queue maintenance | embedded in every claim | <=2.5/50/50 ms, once/10 s |

Concurrency makes an individual transaction slightly wider in some low
percentiles, but removes the serial throughput cap without unsafe pool growth.
Battle persistence changed from `18 + R + 8V` statements (90 for eight valid
rows) to 18 statements end to end: three claim statements, one renewal, and
14 fixed completion statements independent of row count. `pg_stat_statements`
confirmed one call per set-based battle statement per battle observation. As
in the issue 38 audit, these counts exclude protocol `BEGIN`/`COMMIT`.

## Capacity, locks, and database pressure

The collector's base 16-connection pool recorded 81,850 empty acquires and
377.218 seconds total acquire time. Cycle 18's 32-connection pool recorded
9,797 empty acquires, 100.806 seconds total acquire time, and zero cancelled
acquires. Its claim and observation acquire percentiles are reported above.

Cycle 18's Python pool ended with all 12 connections available, no waiter, 25
queued acquisitions over 77,917 requests, and 586 ms total wait. Archive
acquire p99 was <=0.5 ms.

| Signal | Cycle 17 | Cycle 18 | Cycle 19 |
|---|---:|---:|---:|
| PostgreSQL peak memory | 544.9 MB | 508.4 MB | 533.9 MB |
| MinIO peak memory | 370.5 MB | 371.2 MB | 365.8 MB |
| collector peak memory | 24.4 MB | 24.0 MB | 24.5 MB |
| Python peak memory | 63.9 MB | 60.0 MB | 59.8 MB |
| PostgreSQL peak CPU | 158% | 163% | 166% |
| MinIO peak CPU | 18% | 29% | 35% |
| collector peak CPU | 37% | 37% | 36% |
| Python peak CPU | 98% | 100% | 99% |
| rollbacks / deadlocks / temp files | 2 / 0 / 0 | 1 / 0 / 0 | 1 / 0 / 0 |
| WAL bytes | 622.2 MB | 528.8 MB | 529.5 MB |
| WAL buffers full | 0 | 0 | 0 |

The host retained substantial CPU and memory headroom and used no swap for the
isolated stack. No application or database container was OOM-killed or
restarted. A sampled blocked-backend count briefly reached two/four/one in
cycles 17/18/19, returned to zero, and showed no upward trend. Cycle 18 also
captured checkpoint/write contention in the claim and row-lock tails, but
still cleared both queues with 52.471 seconds of deadline headroom and no
deadlock. The base scheduler produced 634 temporary files and 4.80 GB
temporary bytes; all after cycles produced zero temporary files or bytes.

PostgreSQL now starts with `pg_stat_statements`, `track_io_timing`, and
`track_wal_io_timing` enabled, and migration 0003 installs the extension. The
collector and Python worker expose bounded stage histograms and pool state in
their normal health/metrics output, so the measurements above remain available
after deployment.

## Implemented boundary and correctness

- Python uses one bounded 12-lane target-host worker, a 12-connection database
  pool, and a 12-connection archive pool. Claim recovery is separately paced
  and bounded; battle rows are persisted with deterministic set-based SQL.
- Collector cleanup/recovery is paced outside ordinary claims. Claims are
  bounded and indexed; the scheduler uses set SQL and a due-player index, with
  one active regular poll per player enforced in PostgreSQL.
- Claim indexes begin with due/expiration and Python's trigger-maintained
  compatibility marker, so old future-due, expired, stale-attempt, and future
  source-contract backlogs cannot turn a bounded return limit into an
  unbounded scan. Adversarial production-depth plan tests cover these shapes.
- Archive creation is conditional and immutable with `Content-MD5`. A
  successful PUT is the durable hot-path acknowledgement; conflict is verified
  by GET/SHA-256. Python still verifies exactly the bytes it parses.
- Observation + Python job + endpoint result remain one fenced transaction.
  Archive-before-observation, ambiguous-commit reconciliation, lease fencing,
  replay/idempotency, and deterministic conflict handling remain intact.
- Migration 0002 is unchanged from `main`. All new indexes, privileges, the
  reset-baseline lock seam, and metrics extension are in forward migration
  0003. Deployment consults `clash_lens_schema_migrations` and never replays an
  already-recorded migration; role-password reconciliation remains separate.
  Start-only collector and Python paths reject a contract-v2 database that is
  missing migration 0003.

The target profile is deliberately below the host ceiling: increasing lanes
or database connections is not justified. Four official keys cap admission at
120 requests/second. Two cycles sustained about 118/s; the checkpoint-heavy
cycle still sustained 101.0/s while Python kept pace.
