# Issue 29 Python-layer prototype

**Status:** throwaway prototype code and prototype-only deployment slice. It
does not define the production Python application or a production database
migration.

Start with this page when you need to run the prototype checks. Use
[`deployment.md`](deployment.md) for the rootless Podman lifecycle. Use
[`docs/architecture.md`](../docs/architecture.md) and
[`docs/domain.md`](../docs/domain.md) for the accepted production boundaries
and domain rules.

## What the prototype proves

The prototype answers this question:

> Can one synthetic archived profile observation pass through a real PostgreSQL
> lease, an integrity-checked S3-compatible archive read, versioned profile
> parsing, one fenced transaction, and a signed private API response without
> exposing an internal player ID?

Yes. The black-box test starts the repository's embedded PostgreSQL server,
starts a local S3-compatible HTTP fixture, runs the Python worker, and calls
the signed FastAPI route. The focused PostgreSQL suite also checks idempotency,
lease fencing, retry limits, version rejection, current-state ordering, and
classified failures.

This result is a seam check. The prototype does not process the production
backlog, call the official Clash of Clans API, or implement battle processing,
ranked-day reconciliation, leaderboards, accounts, Discord behavior, exports,
or overlays.

Completion check: a passing test proves the synthetic profile path only. It is
not evidence of production coverage, API access, or production readiness.

## 1. Run the local checks

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

The Go tests create the Python environment in a temporary directory. They do
not leave a repository-local `.venv`. The dependency and Python-version
constraints are in [`pyproject.toml`](pyproject.toml) and [`uv.lock`](uv.lock).

Completion check: each selected command exits with status `0`. A test that
skips because PostgreSQL or `uv` is unavailable is not a passing integration
result; record the skipped prerequisite.

## 2. Run the isolated deployment seam

The deployment script uses only the `clashlens-python-prototype-` resource
prefix. It creates a private rootless network, one PostgreSQL volume, loopback
API publishing, read-only application containers, explicit memory and PID
limits, dropped Linux capabilities, and file-backed Podman secrets. It does
not use the Go collector's network, volume, or container names.

From the repository root, the two synthetic entry points are:

```sh
python-prototype/deploy.sh init
python-prototype/deploy.sh verify
```

`init` creates `prototype.env` and ignored runtime secret files when they do
not exist. It never prints generated key values and it does not accept official
API credentials. `verify` starts a temporary fake archive, seeds and processes
one synthetic profile, checks the signed saved-data API, removes the fake
archive, and restores the worker with its normal TLS default. The API and
PostgreSQL volume remain available.

Follow [`deployment.md`](deployment.md) for prerequisites, lifecycle commands,
secret rotation, systemd, failure cleanup, and the limits of a real Podman run.

Completion check: the deployment seam reports `deployment seam checks passed`,
and no fake archive container remains after verification.

## 3. Keep the fixed prototype contract

The selected runtime is fixed for this prototype:

- Python `3.12` is required by `pyproject.toml`.
- The code uses FastAPI, Pydantic 2, psycopg 3 direct SQL and pooling, MinIO,
  Uvicorn, and pytest. It does not use an ORM.
- The test client is `httpx2`, as required by the locked Starlette dependency.
- `uv` owns dependency locking. Do not replace it with an unlocked install in
  a reproducibility check.
- The archive reader uses TLS by default, disables client-library retries,
  applies explicit connect and read timeouts, retries at most once by default,
  and reads at most `2,000,000` bytes by default.
- An insecure archive origin requires the explicit
  `--archive-insecure-test-only` flag. The deployment uses that flag only for
  its temporary HTTP fixture.
- The worker verifies SHA-256 before JSON parsing and stops a durable job after
  its configured attempt count.
- HMAC files contain one unpadded Base64URL value that decodes to 32 bytes,
  followed by at most one final LF. The CLI accepts one current and one
  previous key for one caller.

The exact HMAC proof protocol is owned by
[`docs/architecture.md`](../docs/architecture.md). The prototype API checks
raw ASGI path and query bytes and authorizes only the configured caller and
operation. Provider identities remain denied until a Python-owned account
resolver exists.

Completion check: changes to a version, dependency, archive limit, proof rule,
or account rule are recorded in the source and tests before the prototype docs
are changed.

## 4. Keep the API and container boundary

The candidate route is `GET /v1/players/{player_tag}`. It returns screen-ready
profile data and does not return the internal numeric player ID. It is a
prototype route, not an approved production API contract.

`GET /livez` returns `{"live":true}` without private proof headers.
`GET /readyz` reports the PostgreSQL prototype contract state without private
proof headers. The private API rejects request bodies above `1,048,576` bytes
and returns stable error categories without backend exception details.

The isolated deployment publishes the API on loopback only. PostgreSQL and the
fake archive have no host ports. The API receives the database URL and HMAC
keys through mounted file secrets. The worker receives the database URL and
archive credentials through mounted file secrets. The deployment does not
mount official Clash of Clans API credentials because this prototype never
calls that API.

Completion check: a signed prototype response contains the normalized public
tag and profile fields, contains no `id` field, and a health request succeeds
without a proof header.

## Production gaps

These gaps are intentional and remain open:

- The repository collector and deployment migration `0001` use contract
  version `1`. The prototype `schema.sql` uses its own prototype-only contract
  version `2`; a tested version-1-to-version-2 Go bridge is required before a
  production migration can use that contract.
- [`src/clashlens_prototype/schema.sql`](src/clashlens_prototype/schema.sql)
  is prototype-only. Its startup guard refuses to replace an existing database
  with another contract version.
- `GET /v1/players/{player_tag}` is a candidate route, not an approved
  production API contract.
- This slice has no production package layout, production deployment, metrics,
  tracing, retention jobs, backups, migration rollback tooling, or production
  account resolver.
- This slice has no Discord behavior, accounts, battle processing, ranked-day
  reconciliation, leaderboards, exports, or overlays.
- The prototype does not prove the accepted private API caller-operation matrix,
  shared Go/Python traffic gate, or raw-archive production product.

Keep these gaps visible. Do not connect the prototype database to the
production collector or present a passing synthetic test as production
readiness.
