# Clash Lens Python application

This package owns the single Python asyncio collector, official API collection,
the bounded raw-response spool, the immutable raw archive, domain processing,
canonical battles, ranked days, snapshots, analytics, replay, accounts, and the
private signed API. Runtime boundaries are in
[`docs/architecture.md`](../docs/architecture.md); stable domain rules are in
[`docs/domain.md`](../docs/domain.md); production Podman operations are in
[`docs/deployment.md`](../docs/deployment.md).

## Layout

- `src/clashlens/` — application modules, worker, API, accounts, processing,
  reconciliation, analytics, verification, and HMAC proof.
- `tests/` — pytest suite, including PostgreSQL-backed tests.
- `testdata/` — synthetic fixtures only; no credentials or live player bodies.

The production schema is owned by the numbered SQL files under
`deploy/migrations/`.
See [history retention](../docs/history-retention.md) for compact storage,
raw-response expiry, and the limits on replay after expiry. Application startup
does not create or alter tables; tests apply these migrations directly.

## Local checks

From `python/`, use the locked environment:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv \
UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q
```

For PostgreSQL-backed tests, set `CLASHLENS_TEST_DATABASE_URL` first:

```sh
CLASHLENS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/clashlens \
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv \
UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q
```

Test databases built by `domain_database` in `tests/domain_test_support.py` also
run the worker permission limits that `../ops` applies after migrations, read
from `../ops` itself, so worker-role tests fail where production would.

The collector and worker share the bounded local spool. The collector saves the
exact raw response and durable observation metadata before the worker parses it;
restarts can recover incomplete handoffs without calling the official API.

`uv.lock` and `pyproject.toml` define the Python and dependency constraints.
Record unavailable prerequisites when an integration test skips.

GitHub runs the Python suite in four parallel groups, each with its own PostgreSQL
service. `CLASHLENS_TEST_GROUP=1`, `2`, `3` or `4` selects a group; leave it unset
to run the whole suite locally. Do not run two groups against the same database,
because some tests also change database roles.
Each group runs with `pytest --durations=30`, reporting its 30 slowest setup,
test, and cleanup phases. The separate fake-service tests in
`development/test_fixtures.py` run once, after group 1's Python tests.

`tests/ci_test_durations.json` records per-file seconds measured on GitHub on
October 3, 2026, from the two-group runs of main commits `6a21ddb`, `fcc5bcf`,
`3e59e20`, `d8b75e6` and `a7af5ca`. Each pytest progress line covers 72 tests
and carries GitHub's timestamp; each listed slow test kept its reported time,
the rest of that line's time was shared evenly across its other tests, and each
file keeps the median over the five runs. The longest files go first into the
group with less recorded work. New test files also run once, with an initial
estimate of one second. The workflow tests collect the full suite and all four
groups to check for missing or repeated tests and check that their recorded
time totals differ by less than 10 percent. Refresh the timings when the
groups' GitHub test times drift apart.

Pull requests always build the Python check image and run its packaged backup
and support tests. Pushes to main, and manual runs of a branch, also run the
whole packaged suite in the check image as four more groups, each against its
own PostgreSQL 18 Alpine service, the database image the development stack
uses. Packaged group 1 also runs Ruff, compiles `src` and `../development`, and
runs the fake-service tests. `Packaged Python tests` fails if any packaged group
fails, is cancelled, or is skipped. The full development container check then
only starts the stack and runs the website and browser checks against it.
The existing check names stay unchanged; the Python result waits for all four
groups and fails if any group fails, is cancelled, or is skipped.

Before this split, successful pull-request runs took a median 8m28s, and main's
full development container job took 29m52s to 44m33s because it ran the whole
packaged suite, 27 to 40 minutes, one test at a time before the browser checks.
The skips leave database query-count ceilings, cross-locale backup behavior,
and the opt-in 22,157-tag load run unverified.

## Production interface

The root `ops` entry point owns the lifecycle through rootless Podman Quadlet
services. See the [deployment runbook](../docs/deployment.md) for building a
release and configuring its services before starting it:

```sh
../ops up
../ops status
../ops logs worker
../ops down
```

Workers claim fenced jobs from the shared queue, verify local spool bytes, and
use their archive-read credential when archived evidence is needed. The private
API is reachable only within the stack; the website authenticates its requests.

For bounded current-season repair and republishing, follow
[Armies page empty](../docs/operating.md#armies-page-empty) for the command,
stopping conditions, failure reasons and repair limits.
