# Performance runner

`scripts/performance_runner.py` is the PostgreSQL-backed issue #60 Step 1
baseline and issue #64 raw-evidence harness. `scripts/fedora_probe.sh` is the
checked-in Fedora entrypoint; it requires an explicit disposable PostgreSQL
URL and retains a JSON artifact under `results/`. It creates and drops an isolated schema per sample, applies
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

## Prerequisites and limits

- Python 3.12 and the locked `python/` development environment (`uv sync --locked`).
- Go 1.26 for the collector-to-Python probe. Its first run downloads the
  embedded PostgreSQL test binary and may need a warm cache to fit the probe's
  120-second timeout.
- A disposable UTF-8 PostgreSQL database where the supplied user can create
  schemas. Never point the runner at production. `SQL_ASCII` can return text as
  bytes and is not representative of the deployed database.
- Enough local space for PostgreSQL WAL and relation growth. The default army
  workload in reset and correction modes is 1,000 facts; `--army-facts` accepts
  1 through 100,000. The `army-analytics` mode uses its fixed 5.6-million-fact
  workload and ignores the legacy `--army-facts` setting.
- Duplicate-heavy mode distributes occurrences across ~200 tracked players
  (~125 responses each) so duplicate writes match the production multi-player
  shape instead of concentrating on one row. `--lanes` (1-64) sets the
  processing concurrency used for both the PostgreSQL/archive pools and the
  job executor. `--duplicate-cycles` (1-4) repeats the duplicate window over
  the same spool: cycle one carries the ~1% hash-novelty sample and later
  cycles are 100% verified-duplicate steady state. With more than one cycle
  the workload reports per-cycle elapsed times plus a conservative
  `daily_288_cycle_projection_seconds` (median cycle × 288 five-minute
  intervals) as the 24h-equivalent aggregate; every executed response still
  runs the full raw-evidence/local/Python/PostgreSQL semantics.
- `pg_stat_statements` is optional. Its SQL-call delta is `null` when the
  extension is unavailable; the in-process Cursor call count remains present.
- RSS is process maximum RSS, not a per-sample instantaneous value. Image
  digests are operator-supplied provenance and must use
  `--image NAME=sha256:DIGEST`.

From `python/`:

```sh
export CLASHLENS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/clashlens
export UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv
export UV_LINK_MODE=copy

uv run --locked --python 3.12 ../scripts/performance_runner.py reset-boundary \
  --populations 2,4,8 --army-facts 1000 --output ../results/reset.json
uv run --locked --python 3.12 ../scripts/performance_runner.py correction \
  --populations 2,4,8 --army-facts 1000 --output ../results/correction.json
uv run --locked --python 3.12 ../scripts/performance_runner.py duplicate-heavy \
  --duplicate-observations 100 --output ../results/duplicates.json
uv run --locked --python 3.12 ../scripts/performance_runner.py mixed-backfill \
  --live-jobs 20 --backfill-jobs 100 --output ../results/mixed.json
uv run --locked --python 3.12 ../scripts/performance_runner.py coordinator-12500 \
  --army-facts 1 --lanes 1 --post-fix --output ../results/coordinator-12500.json

# Issue #73 PR 2 fixed army read, mixed-load, and query-plan protocol
uv run --locked --python 3.12 ../scripts/performance_runner.py army-analytics \
  --output ../results/issue-73-pr2-army.json
```

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

Results include source/dirty state, runner and migration hashes,
configuration, optional images, PostgreSQL WAL and relation sizes, SQL calls,
queue rows and oldest active age, fact/publication counts, stage and endpoint
latency, archive operations, elapsed time, CPU, and RSS. SQL and WAL windows
include workload seeding as well as production processing. Reset modes also
assert the current per-player job, two-header, and quadratic entry counts rather
than emitting a degraded sample. Python queues are
drained by the workload. Collector-owned active residue, including discovery
profiles, is reported separately by owner and work type because this Python
runner cannot claim collector work; it must not be read as drained reset work.

Mixed-backfill converts seeded observation jobs into replay jobs directly; it
measures production claim and replay processing order, not the public replay
request operation. Synthetic army rows preserve the production query shape but
are not official observations.

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
  CLASHLENS_FEDORA_PROBE_OUTPUT=/home/clashlens/step2-results/duplicates.json \\
  scripts/fedora_probe.sh duplicate-heavy --duplicate-observations 25024
```

Without `CLASHLENS_TEST_DATABASE_URL`, PostgreSQL checks are skipped. A skip is
not performance acceptance. Database and collector-probe failures return a
short nonzero diagnostic and do not emit a partial JSON result. Retain real
Fedora target-host outputs for review. The dedicated `army-analytics` mode
asserts the warmed 200 ms p95, forced-miss five-second bound, four-lane overlap,
queue-drain, and five-minute gates;
its output is retained before a hard-gate failure returns nonzero.
