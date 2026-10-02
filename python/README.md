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
operator-only cleanup, and the limits on replay after expiry. Application startup does not create or
alter tables; tests apply these migrations directly.

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

GitHub runs the Python suite in two parallel groups, each with its own PostgreSQL
service. `CLASHLENS_TEST_GROUP=1` or `2` selects a group; leave it unset to run the
whole suite locally. Do not run both groups against the same database, because
some tests also change database roles.
Each group runs with `pytest --durations=30`, reporting its 30 slowest setup,
test, and cleanup phases. The separate fake-service tests in
`development/test_fixtures.py` run once, after group 1's Python tests.

`tests/ci_test_durations.json` records per-file totals from a complete
`pytest --durations=0 --durations-min=0` run against isolated PostgreSQL 18 on
September 30, 2026. The longest files go first into the group with less measured
work. These totals include setup and cleanup. The timing container on rogue was
limited to two processor cores and 1 GiB of memory, with a separate database
limited to one processor core and 512 MiB. The interrupted file was remeasured
after pausing for service work. GitHub elapsed times still need their own measurement.
New test files also run once, with an initial estimate of one second.
The workflow tests collect the full suite and both groups to check for missing
or repeated tests and check that their recorded time totals differ by less than
10 percent. Refresh the timings from an ungrouped run when the groups drift.

Pull requests always build the Python check image and run its packaged backup
and support tests. The full development container check runs on pushes to main.
The existing required check names stay unchanged; the Python result waits for
both groups and fails if either group fails, is cancelled, or is skipped.

The September 30 local validation collected 975 tests, split into 547 and 428.
The two executed lists also contained 975 unique tests with no duplicates.
The groups passed 544 and 427 tests, with four skips in total, in 406.96 and
396.59 seconds. The packaged backup/support tests passed 30 cases with one
locale skip in 156.19 seconds. The skips leave database query-count ceilings,
cross-locale backup behavior, and the opt-in 22,157-tag load run unverified.

For hosted comparison, the [earlier full container run](https://github.com/zzzubair/clashlens/actions/runs/36774897967)
took 22m35s. Its five development image builds took about 21s, stack startup
took another 30s, and the repeated Python suite took 17m31s. A
[later packaging-only run](https://github.com/zzzubair/clashlens/actions/runs/36779100272)
passed that job in 2m56s, including an 11s image build and 2m35s packaged tests.
Its Python job took 9m53s. These are before-change hosted measurements; the
modified pull request's total hosted time must be recorded in its description.

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
