# Clash Lens Phase 1 Python application layer

**Status:** the production functional-beta Python layer on main. It does not
complete every item of Issue 29. This package owns domain processing, current
player state, canonical battles, ranked days, snapshots, analytics, replay,
accounts, and the private signed API. It does not collect official API
evidence; the Go collector owns collection and the immutable raw archive.

Open scope: the Discord bot role is absent from this package. Export routes and
database submission scaffolding exist, but beta caller authorization denies
those operations and the worker does not claim `build_export` jobs. Exports
stay disabled during beta. Global Top-200 collection stays default-off: the
deployment refuses to enable it during beta. Production login stays disabled
until the Python service enforces the strict inappropriate-name filter and the
root deployment passes the login configuration.

Use [`docs/architecture.md`](../docs/architecture.md) and
[`docs/domain.md`](../docs/domain.md) for the accepted production boundaries
and domain rules. Use [`docs/deployment.md`](../docs/deployment.md) for the
production Podman lifecycle. The root [`deploy.sh`](../deploy.sh) builds and
runs the production worker from this package.

## Package layout

- `src/clashlens/` — application modules: worker, private API, accounts,
  domain processing, reconciliation, snapshots, analytics, verification,
  and HMAC proof.
- `tests/` — pytest suite. PostgreSQL-backed tests run against the real
  migrations in `deploy/migrations/`.
- `testdata/` — synthetic fixtures only. No live player bodies, tokens, or
  credentials are committed.

## 1. Run the local checks

Run the complete Python suite without PostgreSQL from `python`:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q
```

Run the complete Python suite against a PostgreSQL 18 test database from
`python`:

```sh
CLASHLENS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/clashlens UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q
```

Run the complete vertical seam from the repository root:

```sh
go test ./internal/collector -run '^TestGoCollectorHandoffToPythonSignedPlayerPage$' -count=1 -v -timeout=120s
```

The Go seam creates its Python environment in a temporary directory and does
not leave a repository-local `.venv`. The dependency and Python-version
constraints are in [`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock).

Completion check: each selected command exits with status `0`. A test that
skips because PostgreSQL or `uv` is unavailable is not a passing integration
result; record the skipped prerequisite.

## 2. Production worker and private API

The root deployment script owns the production lifecycle:

```sh
../deploy.sh python-up
../deploy.sh status
../deploy.sh queue-status
../deploy.sh python-start
../deploy.sh api-start
../deploy.sh worker-start
../deploy.sh python-down
```

`python-up` builds the Python image and starts the private API and the
configured production-worker replicas; the `*-start` commands start roles
without building. Each worker uses bounded in-process lanes and database and
archive pools, claiming one fenced lease per lane. A processing pass stops
when the queue is idle or the configured `--max-jobs` count is reached. The
worker reads archive objects only through its own archive-read credential and
never requests a new Supercell source request during replay. The private API
listens on the private Podman network alias
`python-api:8000` with no published host port.

## 3. Fixed production contract

The selected runtime is fixed for this package:

- Python `3.12` is required by `pyproject.toml`.
- The code uses FastAPI, Pydantic 2, psycopg 3 direct SQL and pooling, MinIO,
  Uvicorn, and pytest. It does not use an ORM.
- The test client is `httpx2`, as required by the locked Starlette dependency.
- `uv` owns dependency locking. Do not replace it with an unlocked install in
  a reproducibility check.
- The archive reader uses TLS by default, disables client-library retries,
  applies explicit connect and read timeouts, retries at most once by default,
  and reads at most `2,000,000` bytes by default.

## 4. Schema ownership

The database schema lives in the production migrations at
`deploy/migrations/0001_collector.sql` through
`deploy/migrations/0003_regular_poll_dedup.sql`. Application startup does not create
or alter tables; tests apply the real migration files directly.
