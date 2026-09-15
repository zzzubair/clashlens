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

The collector and worker share the bounded local spool. The collector saves the
exact raw response and durable observation metadata before the worker parses it;
restarts can recover incomplete handoffs without calling the official API.

`uv.lock` and `pyproject.toml` define the Python and dependency constraints.
Record unavailable prerequisites when an integration test skips.

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

After a publication contract change, bounded current-season republishing is
available to the worker role:

```sh
python -m clashlens.cli republish-current-season --max-jobs 100
```

Repeat until the command reports `enqueued_count` as zero.
