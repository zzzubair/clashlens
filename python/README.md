# Clash Lens Python application

This package owns domain processing, canonical battles, ranked days,
snapshots, analytics, replay, accounts, and the private signed API. The Go
collector owns official API collection and the immutable raw archive. Runtime
boundaries are in [`docs/architecture.md`](../docs/architecture.md); stable
domain rules are in [`docs/domain.md`](../docs/domain.md); production Podman
operations are in [`docs/deployment.md`](../docs/deployment.md).

## Layout

- `src/clashlens/` — application modules, worker, API, accounts, processing,
  reconciliation, analytics, verification, and HMAC proof.
- `tests/` — pytest suite, including PostgreSQL-backed tests.
- `testdata/` — synthetic fixtures only; no credentials or live player bodies.

The production schema is owned by `deploy/migrations/0001_collector.sql`
through `0008_public_army_analytics.sql`. Application startup does not create or alter
tables; tests apply these migrations directly.

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

The Go-to-Python handoff can be checked from the repository root:

```sh
go test ./internal/collector -run '^TestGoCollectorHandoffToPythonSignedPlayerPage$' \
  -count=1 -v -timeout=120s
```

`uv.lock` and `pyproject.toml` define the Python and dependency constraints.
Record unavailable prerequisites when an integration test skips.

## Production interface

The root `deploy.sh` owns the lifecycle:

```sh
../deploy.sh python-up
../deploy.sh status
../deploy.sh queue-status
../deploy.sh python-start
../deploy.sh api-start
../deploy.sh worker-start
../deploy.sh python-down
```

Workers claim fenced jobs from the shared queue and read archive objects only
through their archive-read credential. The private API is available only on
the private Podman network at `python-api:8000`.

After a publication contract change, bounded current-season republishing is
available to the worker role:

```sh
python -m clashlens.cli republish-current-season --max-jobs 100
```

Repeat until the command reports `enqueued_count` as zero.
