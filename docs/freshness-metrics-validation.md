# Freshness measurements validation

Measured locally on 2026-10-02. Nothing was deployed and no production data or
credentials were used. The operating contract and metric names are in
[Operating Clash Lens](operating.md#freshness-measurements).

## Scope

The collector exports successful profile/battle-log check ages at `/metrics`.
The private API exports Live Leaderboard ages in `/operatorz.live_leaderboard`.
This split preserves the existing database permissions. Neither alert thresholds
nor the alert probe's two-count output changed.

The existing collector health query moved out of `collector_db.py`, reducing it
from 1,499 to 1,462 lines. The leaderboard page and operator query share one
membership expression. Most added lines cover behavioral tests and operating
instructions; there was no unrelated source code to remove.

## Local query cost

PostgreSQL 18.6, unpacked into this worktree, on an AMD Ryzen 5 5500U host.
The database used a task-specific localhost port and data directory, with no
service installation. These are shared-host timings, not isolated CPU or
production measurements. Other local tests were running during parts of the
measurement. SQL statements ran under `clashlens_collector` and
`clashlens_python_api`, respectively, with existing grants only.

The generated population contained 13,000 active players, 26,000 compact endpoint
states, 13,000 observations, and 143,000 profile versions: 13,000 current versions
plus 130,000 historical ones. Each current pointer referenced the newest group.
Work queues were empty. Table statistics were refreshed before measuring.

Each measurement executed the production query through its Python function, on
one open local database connection. Timings include the database round trip and
Python result conversion, but not opening the connection or serving HTTP.
One first call was followed by 30 repeated calls:

- Collector full health query: first 53.035 ms; repeated minimum 37.801 ms,
  median 40.209 ms, maximum 57.892 ms.
- Leaderboard age query: first 46.807 ms; repeated minimum 38.124 ms,
  median 40.177 ms, maximum 53.568 ms.
- Both execute exactly one SQL statement per sample. Endpoints reuse the sample
  for 30 seconds. Together the measured medians are about 80 ms per 30 seconds,
  or 0.27% of wall time. This is not a CPU-utilization estimate.

`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` measured execution times of 45.090 ms
and 48.377 ms. Neither plan spilled to temporary storage. The leaderboard used
`player_profile_versions_pkey` exactly 13,000 times, returning one current row
per lookup instead of reading historical versions. The `LATERAL` lookup's
`OFFSET 0` preserves that bound. Without it, the same latest-profile population
caused a full 143,000-row profile scan and a 66.637 ms repeated median.

The collector's new age aggregation reads the compact endpoint-state table,
not saved-response history. PostgreSQL chose hash joins with sequential reads
of that small population. Its join columns match the existing unique
`(scope, identity_key, endpoint)` key; no index or schema change was needed.
The existing due-work and queue lookups also retained their indexes.

## Checks and limits

The focused checks cover emitted collector text and signed API JSON, real
service-role permissions, nearest-rank percentiles, missing and empty populations,
future timestamps, unchanged successes, failed responses, current-profile
confirmation, inactive/unaccepted/not-found membership, the exact 600-second
boundary, cache expiry, concurrent collector scrapes and refresh failures.
Existing collector, leaderboard and alert regression tests are included.

An intermediate rerun had 50 passes and 13 setup errors because shared `/tmp`
reached its disk quota. The final run redirects `TMPDIR` and pytest's
`--basetemp` into this worktree's ignored scratch directory.
That final run passed all **154 tests in 86.07 seconds**, without skips, across:

```text
tests/test_freshness_metrics_postgres.py
tests/test_api_db_public_ops.py
tests/test_alerts.py
tests/test_collector_metrics_postgres.py
tests/test_collector_readiness.py
tests/test_api_security.py
tests/test_collector.py
tests/test_collector_db_postgres.py
```

Command: `uv run --locked --python 3.12 pytest -q` with those paths,
`CLASHLENS_TEST_DATABASE_URL` pointing to the temporary local PostgreSQL, and
worktree-local `UV_PROJECT_ENVIRONMENT`, `UV_CACHE_DIR`, `TMPDIR`, and
`--basetemp`. Ruff checks on all changed Python files and `git diff --check`
also passed. The temporary database and unpacked packages were removed after
validation.

No live API load, production query plans, production-sized processing backlog,
Reset trial, container build, full repository suite, or hosted checks were run.
The synthetic ages establish percentile correctness and query cost, not a
production freshness promise. Automated no-mistakes validation and publication
remain a separate firstmate handoff.

## Not-found exclusion follow-up

Measured locally on 2026-10-02 using PostgreSQL 18.6 on the same AMD Ryzen 5
5500U host. The collector now excludes players under the Live Leaderboard's
profile not-found rule from all three check-age gauges and their sample/missing
counts. Active-player and due-work counts retain those players for retries.
This adds one filter to the existing profile-state join, with no new query,
database grant, index, schema change, or metric.

Before the filter, three regression cases failed: a hidden player with old
successes entered the age samples, and a never-found player entered the
missing-check count. After the filter, **108 tests passed in 85.97 seconds**,
without skips, across:

```text
tests/test_freshness_metrics_postgres.py
tests/test_api_db_public_ops.py
tests/test_alerts.py
tests/test_collector_metrics_postgres.py
tests/test_collector_readiness.py
tests/test_collector_db_postgres.py
```

The changed tests cover emitted metrics under the collector's database role,
mixed and entirely hidden populations, a not-found time equal to a success,
a newer success, and battle-log not-found responses that must not hide a player.
A real response-recording sequence checks profile success, not found, server
error, and successful recovery. Existing missing-history and future-time cases
remain covered. Ruff lint/format checks and `git diff --check` also passed.

The query comparison used one disposable database with 13,000 active, due
players and 26,000 endpoint states. Ten players had 23.5-hour-old successful
checks and newer profile not-found responses; the other 12,990 had successful
profile checks 30 seconds old and battle-log checks 60 seconds old. Work queues
and saved-response history were empty. After `ANALYZE`, both the previous and
updated full health functions ran under `clashlens_collector` on one open
connection. Each had a first call followed by 30 alternating repeated calls,
including database round trips and Python result conversion:

- Previous query: first 42.562 ms; repeated minimum 32.826 ms, median 34.864 ms,
  maximum 42.099 ms.
- Updated query: first 37.056 ms; repeated minimum 32.082 ms, median 34.692 ms,
  maximum 37.434 ms.
- Samples changed from 13,000 to 12,990. Maximum age changed from 84,601.023
  seconds to 61.066 seconds. The median and 95th percentile remained about
  61 seconds. Active-player and due-work counts stayed at 13,000.

`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` took 44.453 ms for the updated query,
with no temporary-storage reads or writes. The filter removed exactly ten
players using the already-joined compact profile state. The repeated median
remains about 35 ms per 30-second sampling interval; the small timing difference
is shared-host noise, not evidence of a speedup.

The commands used the locked Python 3.12 environment and temporary PostgreSQL
recipe in `.no-mistakes.yaml`, with files inside this worktree. Pytest used the
six paths above with `-q --durations=10` and a worktree-local `--basetemp`.
This follow-up did not check production plans, a populated processing backlog,
the full repository suite, container builds, hosted checks, or live API traffic.
Nothing was deployed. The database was stopped after validation.
