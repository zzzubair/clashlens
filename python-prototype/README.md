# Issue 29 Python-layer prototype

This is throwaway prototype code and a prototype-only deployment slice; it does not define the production Python application or a production database migration.

## Question and result

Can one synthetic archived profile observation move through a real PostgreSQL lease, an integrity-checked S3-compatible archive read, versioned profile parsing, and one fenced transaction, then appear through a signed private API response without exposing an internal player ID?

Yes. The black-box test starts the repository's embedded PostgreSQL server, starts a local S3-compatible HTTP fixture, runs the Python worker, and calls the signed FastAPI route.

The focused PostgreSQL suite also verifies idempotency, lease fencing, retry limits, version rejection, current-state ordering, and classified failures.

The prototype does not process the production backlog or call the official Clash of Clans API.

## Fixed prototype choices

- Python `3.12` is the baseline; it has lower dependency risk than Python `3.14` for the selected stack.
- The runtime uses FastAPI, Pydantic 2, psycopg 3 direct SQL and pooling, MinIO, Uvicorn, and pytest; it does not use an ORM.
- The test client uses `httpx2`, as required by the selected Starlette version.
- The archive reader uses TLS by default, disables client-library retries, applies explicit connect and read timeouts, retries at most once by default, and retrieves at most `2,000,000` bytes.
- An insecure archive origin requires the explicit `--archive-insecure-test-only` flag and is used only by tests and synthetic deployment verification.
- The worker verifies SHA-256 before JSON parsing and limits durable processing jobs to their configured attempt count.
- HMAC keys come from files that contain an unpadded Base64URL-encoded 32-byte key and an optional final LF; the CLI supports one current key and one previous key for one caller.
- The private API checks exact raw ASGI path and query bytes and authorizes only the configured caller-operation matrix; provider identities are denied until a Python-owned account resolver exists.
- `GET /livez` is a liveness check and `GET /readyz` reports the PostgreSQL contract state without private proof headers.
- The private API rejects request bodies above `1,048,576` bytes and returns stable error categories without backend exception details.

## Local tests

Run the complete Python suite without PostgreSQL from `python-prototype`:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-prototype-venv UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q
```

Run the complete Python suite with embedded PostgreSQL from the repository root:

```sh
go test ./internal/collector -run '^TestPythonPrototypeSuiteEmbeddedPostgres$' -count=1 -v -timeout=150s
```

Run the complete vertical seam from the repository root:

```sh
go test ./internal/collector -run '^TestPythonPrototypeBlackBoxEmbeddedPostgresToSignedPlayerPage$' -count=1 -v -timeout=120s
```

Run the deployment command seam without Podman:

```sh
bash python-prototype/deploy_test.sh
```

The Go tests put the Python virtual environment in a temporary directory and do not leave a repository-local `.venv`.

## Prototype deployment

The isolated rootless-Podman deployment is documented in `python-prototype/deployment.md`.

The deployment uses only names with the `clashlens-python-prototype-` prefix, a private rootless network, one PostgreSQL volume, loopback-only API publishing, read-only application containers, explicit memory and PID limits, dropped Linux capabilities, and file-backed Podman secrets.

`python-prototype/deploy.sh init` creates a local `prototype.env` file and ignored runtime secret files when they do not exist; it never prints generated key values and it does not accept official API credentials.

`python-prototype/deploy.sh verify` starts a temporary fake archive container, seeds and processes one synthetic profile, verifies the saved-data API, removes the fake archive, and restarts the worker with TLS defaults; the API and PostgreSQL data remain available.

## Production gaps

- The repository collector and migration `0001` still use contract version 1; the collector needs a tested version-1 to version-2 bridge before any production migration can use this schema.
- `schema.sql` is prototype-only and its startup guard refuses to replace an existing contract version 1 database.
- The route `GET /v1/players/{player_tag}` is a candidate only and is not an approved production API contract.
- This slice does not include production package layout, production deployment, metrics, tracing, retention jobs, backups, migration rollback tooling, or a production account resolver.
- This slice does not include Discord behavior, accounts, battle processing, ranked-day reconciliation, leaderboards, exports, or overlays.
