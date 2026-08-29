# Performance runner

`scripts/performance_runner.py` is the PostgreSQL-backed issue #60 Step 8
evidence harness. `scripts/fedora_probe.sh` is the checked-in Fedora
entrypoint; it requires an explicit disposable PostgreSQL URL and retains a
JSON artifact outside the checkout by default. It creates and drops an isolated schema per sample, applies
all migrations, serves committed fixtures from a local archive, and runs the
production collector probe, observation processor, reconciliation, snapshot,
analytics, army-publication, and API database paths. Duplicate-heavy mode also
runs a focused Go test (`TestS3ArchiveDuplicateStoreProbe`) against the
production `s3Archive.store` path on a real HTTP fake S3 server and reports its
conditional PUT plus GET verification totals separately from the Python
archive GET counts; a failed probe or malformed marker fails the run. Samples
also retain evidence novelty, pending/orphan, local-hit/repair, conditional
PUT/verification-GET, and bounded spool-capacity fields for the raw-evidence
contract. The `army-analytics` mode is the issue #73 PR 2 target-host protocol:
it seeds the fixed 12,500-member, 5.6-million-fact workload, exercises the
production `ApiDatabase.get_army_analytics` path for all six selection/lens
pairs, and retains query plans and mixed-load evidence.

Set the required `CLASHLENS_CANDIDATE_RECEIPT` to the exact-head
candidate-preparation receipt. Set `CLASHLENS_FEDORA_RESULTS_DIR` to choose the default retained-results
directory, or set `CLASHLENS_FEDORA_PROBE_OUTPUT` for one exact output path.

## Prerequisites and limits

- Python 3.12 and the locked `python/` development environment (`uv sync --locked`).
- Go 1.26 for the collector-to-Python probe. Its first run downloads the
  embedded PostgreSQL test binary and may need a warm cache to fit the probe's
  120-second timeout.
- The Fedora entrypoint raises only its own soft open-file limit to 65,536 for
  the spool's fixed lock-shard set; it does not change host or service limits.
- A disposable UTF-8 PostgreSQL database where the supplied user can create
  schemas. Never point the runner at production. `SQL_ASCII` can return text as
  bytes and is not representative of the deployed database. The fixed
  `army-analytics` proof additionally requires containerized PostgreSQL and a
  user with `pg_read_server_files` (or superuser) so its own cgroup swap/OOM
  counters can be read from fixed `/sys/fs/cgroup` paths.
- Enough local space for PostgreSQL WAL and relation growth. The default army
  workload in reset and correction modes is 1,000 facts; `--army-facts` accepts
  1 through 100,000. The `army-analytics` mode uses its fixed 5.6-million-fact
  workload and ignores the legacy `--army-facts` setting.
- Duplicate-heavy mode uses the fixed 25,024-response production mix: 12,500
  `profile`, 12,500 `battle_log`, and 24 `global_player_rankings` responses.
  Smaller requested populations are balanced across those endpoints so a
  disposable PostgreSQL run exercises each path. Profile occurrences are
  distributed across ~200 tracked players (~125 responses each) instead of
  concentrating every write on one row. `--lanes` (1-64) sets the processing
  concurrency used for both the PostgreSQL/archive pools and the job executor.
  `--duplicate-cycles` (1-4) repeats the duplicate window over
  the same spool: cycle one carries the ~1% hash-novelty sample and later
  cycles are 100% verified-duplicate steady state. With more than one cycle
  the workload reports per-cycle elapsed times plus a conservative
  `daily_288_cycle_projection_seconds` (median cycle × 288 five-minute
  intervals) as the 24h-equivalent aggregate; every executed response still
  runs the full raw-evidence/local/Python/PostgreSQL semantics.
- `pg_stat_statements` is optional. Its SQL-call delta is `null` when the
  extension is unavailable; the in-process Cursor call count remains present.
- RSS is process maximum RSS, not a per-sample instantaneous value. Workloads
  execute on the host and report `execution.kind: "host"` with an empty
  `executor_images` list. To correlate a prepared candidate without claiming
  that it executed the workload, pass a verified candidate-preparation receipt
  with `--candidate-receipt /retained/receipt.json`.

From `python/`:

```sh
export CLASHLENS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/clashlens
export UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv
export UV_LINK_MODE=copy

uv run --locked --python 3.12 ../scripts/performance_runner.py reset-boundary \
  --populations 2,4,8 --army-facts 1000 --output /retained/reset.json
uv run --locked --python 3.12 ../scripts/performance_runner.py correction \
  --populations 2,4,8 --army-facts 1000 --output /retained/correction.json
uv run --locked --python 3.12 ../scripts/performance_runner.py duplicate-heavy \
  --duplicate-observations 100 --output /retained/duplicates.json
uv run --locked --python 3.12 ../scripts/performance_runner.py mixed-backfill \
  --live-jobs 20 --backfill-jobs 100 --output /retained/mixed.json
uv run --locked --python 3.12 ../scripts/performance_runner.py coordinator-12500 \
  --army-facts 1 --lanes 1 --post-fix --output /retained/coordinator-12500.json

# Issue #73 PR 2 fixed army read, mixed-load, and query-plan protocol
uv run --locked --python 3.12 ../scripts/performance_runner.py army-analytics \
  --candidate-receipt /retained/clashlens-candidate-preparation.json \
  --output /retained/issue-73-pr2-army.json
```

The same `--candidate-receipt` option may be used with each workload. The
receipt must be a schema-2 `candidate-preparation` receipt whose clean source
SHA, migration filenames/hashes through 0013, application source/revision
labels, bounded candidate resource proof, and canonical receipt digest validate
against this checkout. The
runner records those identities under `prepared_candidate_images`; they are
never placed in `executor_images` for a host-run workload. The old ambiguous
`--image` option is rejected.

## Step 8 exact-head Fedora evidence

After the three application images and candidate receipt have been produced
from one clean head, run the four accepted protocols from that same unchanged
checkout. Use a fresh retained-results directory and a disposable UTF-8
PostgreSQL database; the output filenames below must not already exist.

```bash
export CLASHLENS_TEST_DATABASE_URL=postgresql://.../clashlens
export CLASHLENS_CANDIDATE_RECEIPT=/home/clashlens/results/step8-SHA/clashlens-candidate-preparation-TIMESTAMP-SHA.json
RESULTS_DIR=/home/clashlens/results/step8-SHA

CLASHLENS_FEDORA_PROBE_OUTPUT="$RESULTS_DIR/reset-boundary.json" \
  scripts/fedora_probe.sh reset-boundary \
  --populations 12500 --army-facts 1000 --post-fix

CLASHLENS_FEDORA_PROBE_OUTPUT="$RESULTS_DIR/army-analytics.json" \
  scripts/fedora_probe.sh army-analytics

CLASHLENS_FEDORA_PROBE_OUTPUT="$RESULTS_DIR/duplicate-heavy.json" \
  scripts/fedora_probe.sh duplicate-heavy \
  --duplicate-observations 25024 --duplicate-cycles 2

CLASHLENS_FEDORA_PROBE_OUTPUT="$RESULTS_DIR/mixed-backfill.json" \
  scripts/fedora_probe.sh mixed-backfill \
  --live-jobs 20 --backfill-jobs 100 --skip-collector-probe
```

The reset uses the landed bounded-writer guard before admitting the full
12,500-player population. Its opt-in Go probe uses the production contract-v5
admission and scheduler seams in the workload's disposable schema: a transitive
prior regular lineage blocks the reset, production creates the exact sweep,
membership, generation, and reset roots, regular scheduling remains blocked,
and safe handoff reopens scheduling. The duplicate run repeats the accepted 25,024-item
endpoint mix twice for its 24-hour-equivalent comparison. The mixed run keeps
the accepted Step 7 20-live/100-backfill protocol; its collector probe is
skipped because that production-path probe is already exercised by the other
three exact-head modes. Record each file SHA-256 and internal
`artifact_digest`, validate each artifact, and compare it with the retained
Step 1–7 result for that protocol. These are Step 8 measurements, not #31
launch approval or exact-candidate production evidence.

Reset and correction use paired committed reset fixtures and production
`complete_reconciliation`; they do not manufacture ranked-day versions. Their
legacy army-read section bulk-loads bounded synthetic facts after that
production seed. The dedicated `army-analytics` mode seeds exactly 12,500
members across 28 days, eight current facts per member/day/lens, 28,000 missing
trophies per lens, 28 identical fresh/confirmed Top-1,000 snapshots, and 27
troop keys. It executes one timed forced miss (required to stay below five seconds), then
five untimed warmups plus 100 timed cache-hit calls for each of `top-1000`,
`trophies-5000-9999`, and `streak-top-1000` in both lenses through
`ApiDatabase.get_army_analytics`. The forced miss is excluded from the warmed
p95. Cache misses use the measured 256 MiB query-local PostgreSQL work-memory
ceiling. Their SQL is captured and replayed under the same setting outside timing
with `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`. The retained result includes
selected/scanned/returned rows, p95 and RSS, PostgreSQL settings, host memory,
forced-miss and whole-run swap/OOM deltas, and the four-lane 25,024-observation mixed-load gate plus signed account
reads. A p95, overlap, queue, or five-minute violation is retained in the JSON
artifact and exits nonzero; no index or cache is added by the runner.

Results use artifact schema 8 and include an `artifact_digest` SHA-256 over
canonical JSON excluding that field. Artifacts are validated before they are
printed or written; missing/invalid digests, required metrics, or older artifact
versions fail the run. They require a clean exact source SHA, the runner hash,
source migration filenames/hashes and the applied database migration versions
through 0013, a sanitized configuration fingerprint, host/runtime/PostgreSQL
execution identity and settings, and fixed workload facts. They include
generated and retained PostgreSQL WAL and relation sizes,
per-relation DML and
vacuum/analyze lag, SQL calls, queue rows and oldest active age, fixed endpoint
counts, canonical parsed-content/source-row counts, storage-runway inputs,
fact/publication counts, stage and endpoint latency, archive operations, elapsed
time, CPU, and RSS. SQL and WAL windows
include workload seeding as well as production processing. Runway growth uses
retained WAL directory growth from `pg_ls_waldir()`; generated LSN WAL remains
reported separately. Reset modes also assert the exact production-admitted
membership and root count, prior-drain and safe-handoff states, separate
published snapshot/army readiness, current per-player job, two-header, and
quadratic entry counts rather than emitting a degraded sample. Python queues
are drained by the workload. Because official API traffic is forbidden, the
committed-fixture adapter attaches its two fixed observations and completed
attempt to each production-created reset root; it does not create the target
admission, sweep, membership, generation, or roots. Committed battle/ranking
fixture discoveries are pre-qualified as eligible only after target membership
capture so they do not change the claimed reset population or manufacture
collector-only discovery work. Any active collector or Python residue remains
visible and fails the applicable gate.

Retained processing evidence is aggregate-only: fixed
outcome/status/work-type distributions, counts, and bounded latency summaries
replace per-job rows and identifiers. Hard failures use a finite code
vocabulary. Mixed completion order is retained only when the entire configured
order fits the schema's 256-item bound; otherwise exact counts remain and
omission is explicit. `post_fix` is part of the sanitized configuration
fingerprint.

Mixed-backfill seeds committed battle-log fixtures through the production
processor, then creates migration-shaped `redecode_army` jobs for those real
battles and runs live/backfill work through the production worker lanes. Its
retained workload reports live/backfill completion counts and order, per-live
queue latency (p95 and maximum), oldest active queue age, elapsed/CPU/RSS/swap/WAL
measurements, source/configuration provenance, and explicit live-latency and
five-minute contract results. The mode exits nonzero on any unsuccessful live or
backfill result, a live job over five minutes, or active queue residue. Backfill
jobs use the forward-migration class and no official API traffic. `--lanes` is
accepted up to 64 globally; mixed-backfill reports its effective 32-lane worker
ceiling in both the workload and configuration fingerprint.

Reset and correction populations of 12,500 or more require `--post-fix`.
The flag verifies the checked-in snapshot and army writers contain bounded
bulk-write shapes before allowing the target reset. `coordinator-12500` runs
those real Python writers against the isolated PostgreSQL workload; it is not a
SQL-only cardinality shortcut. Each snapshot kind uses one application and one
PostgreSQL statement for entries; army facts use one set-based input read per
relation and fixed bulk writes (insert, supersede, stale deactivation, and the
completion marker). Population validation does not apply to duplicate-heavy or
mixed-backfill modes.

Run focused checks from the repository root:

```sh
python3 -m unittest scripts/test_performance_runner.py -v

# Fedora target-host probe; never point this at production data.
CLASHLENS_TEST_DATABASE_URL=postgresql://... \\
  CLASHLENS_FEDORA_PROBE_OUTPUT=/tmp/clashlens-step8-results/duplicates.json \\
  CLASHLENS_CANDIDATE_RECEIPT=/retained/clashlens-candidate-preparation.json \\
  scripts/fedora_probe.sh duplicate-heavy --duplicate-observations 25024
```

Without `CLASHLENS_TEST_DATABASE_URL`, PostgreSQL checks are skipped. A skip is
not performance acceptance. Database and collector-probe failures return a
short nonzero diagnostic and do not emit a partial JSON result. The runner
publishes a complete artifact atomically and exclusively before returning a
hard workload failure; an occupied output path is rejected. Retain real Fedora
target-host outputs for review. The dedicated `army-analytics` mode
asserts the warmed 200 ms p95, forced-miss five-second bound, four-lane overlap,
queue-drain, and five-minute gates;
its output is retained before a hard-gate failure returns nonzero.
