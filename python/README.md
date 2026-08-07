# Clash Lens Phase 1 Python application layer

**Status:** production Issue 29 implementation. This package owns domain
processing, current player state, canonical battles, ranked days, snapshots,
analytics, replay, exports, accounts, and the private signed API. It does not
collect official API evidence; the Go collector owns collection and the
immutable raw archive.

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

Run the complete Python suite with embedded PostgreSQL from the repository root:

```sh
go test ./internal/collector -run '^TestPythonPrototypeSuiteEmbeddedPostgres$' -count=1 -v -timeout=150s
```

Run the complete vertical seam from the repository root:

```sh
go test ./internal/collector -run '^TestPythonPrototypeBlackBoxEmbeddedPostgresToSignedPlayerPage$' -count=1 -v -timeout=120s
```

The Go tests create the Python environment in a temporary directory. They do
not leave a repository-local `.venv`. The dependency and Python-version
constraints are in [`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock).

Completion check: each selected command exits with status `0`. A test that
skips because PostgreSQL or `uv` is unavailable is not a passing integration
result; record the skipped prerequisite.

## 2. Production worker

The root deployment script owns the production lifecycle:

```sh
../deploy.sh python-up
../deploy.sh python-status
../deploy.sh python-down
```

The worker image is built from this package's `Containerfile`. The worker
reads archive objects only through its own archive-read credential, claims
leased work in bounded batches, and never requests a new Supercell source
request during replay.

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
`deploy/migrations/0001_collector.sql` and
`deploy/migrations/0002_python_layer.sql`. Application startup does not create
or alter tables; `db.apply_schema()` is a test helper that applies the real
migration files, and it refuses to alter a contract version it does not own.
