# Performance runner

`scripts/performance_runner.py` is the PostgreSQL-backed issue #60 Step 1
baseline harness. It creates and drops an isolated schema per sample, applies
all migrations, serves committed fixtures from a local archive, and runs the
production collector probe, observation processor, reconciliation, snapshot,
analytics, army-publication, and API database paths. Duplicate-heavy mode also
runs a focused Go test (`TestS3ArchiveDuplicateStoreProbe`) against the
production `s3Archive.store` path on a real HTTP fake S3 server and reports its
conditional PUT plus HEAD/GET verification totals separately from the Python
archive GET counts; a failed probe or malformed marker fails the run.

## Prerequisites and limits

- Python 3.12 and the locked `python/` development environment (`uv sync --locked`).
- Go 1.26 for the collector-to-Python probe. Its first run downloads the
  embedded PostgreSQL test binary and may need a warm cache to fit the probe's
  120-second timeout.
- A disposable UTF-8 PostgreSQL database where the supplied user can create
  schemas. Never point the runner at production. `SQL_ASCII` can return text as
  bytes and is not representative of the deployed database.
- Enough local space for PostgreSQL WAL and relation growth. The default army
  workload is 1,000 facts; `--army-facts` accepts 1 through 100,000. The army
  read workload runs in reset and correction modes and creates 28 Top-1,000
  snapshots plus synthetic facts in the disposable schema.
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
```

Reset and correction use paired committed reset fixtures and production
`complete_reconciliation`; they do not manufacture ranked-day versions. Their
army-read section bulk-loads bounded synthetic facts after that production
seed and records the 28-day Top 1,000, widest trophy range, and Top-1,000
streak endpoint latency. The army sample reports its own database snapshot
(WAL, relation sizes, queues, SQL calls), elapsed time, CPU, and RSS covering
evidence seeding, fact loading, EXPLAINs, and endpoint materialization before
the schema is dropped. Each selection retains its production-shaped fact
materialization `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, rows scanned, and
rows returned.

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

The known-bad source refuses reset or correction populations of 12,500 or
more. `--post-fix` only permits that size after source inspection finds the
coordinator, set-based snapshot writer, and removal of the per-player snapshot
key. Population validation does not apply to duplicate-heavy or mixed-backfill
modes.

Run focused checks from the repository root:

```sh
python3 -m unittest scripts/test_performance_runner.py -v
```

Without `CLASHLENS_TEST_DATABASE_URL`, PostgreSQL checks are skipped. A skip is
not performance acceptance. Database and collector-probe failures return a
short nonzero diagnostic and do not emit a partial JSON result. Retain real
Fedora target-host outputs for review; the runner does not assert the 200 ms
p95 target from one local sample.
