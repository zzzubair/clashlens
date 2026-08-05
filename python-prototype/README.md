# Issue 29 Python-layer prototype

This is throwaway prototype code. It does not define the production Python application or a production database migration.

## Question and result

Can one synthetic archived profile observation move through a real PostgreSQL lease, an integrity-checked S3-compatible archive read, versioned profile parsing, and one fenced transaction, then appear through a signed private API response without exposing an internal player ID?

Yes. The black-box test starts the repository's embedded PostgreSQL server, starts a local S3-compatible HTTP fixture, runs the Python worker, and calls the signed FastAPI route. The focused PostgreSQL suite also verifies idempotency, lease fencing, retry limits, version rejection, current-state ordering, and classified failures.

The prototype does not process the production backlog or call the official Clash of Clans API.

## Fixed prototype choices

- Python `3.12` is the baseline. It has lower dependency risk than Python `3.14` for the selected stack.
- The runtime uses FastAPI, Pydantic 2, psycopg 3 direct SQL and pooling, MinIO, Uvicorn, and pytest. It does not use an ORM.
- The test client uses `httpx2`, as required by the selected Starlette version.
- The archive reader retrieves at most `2,000,000` bytes and verifies SHA-256 before JSON parsing.
- The worker accepts only the exact endpoint, schema, parser, and processing versions for this slice.
- HMAC keys come from files that contain an unpadded Base64URL-encoded 32-byte key and an optional final LF. The CLI supports one current key and one previous key for one caller.
- The private API checks exact raw ASGI path and query bytes. It permits the TypeScript and Discord callers for the public player-read operation. It denies other caller-operation pairs.

## Verification

Run the complete Python suite with embedded PostgreSQL:

```sh
go test ./internal/collector -run '^TestPythonPrototypeSuiteEmbeddedPostgres$' -count=1 -v -timeout=150s
```

Run the complete vertical seam:

```sh
go test ./internal/collector -run '^TestPythonPrototypeBlackBoxEmbeddedPostgresToSignedPlayerPage$' -count=1 -v -timeout=120s
```

Run tests that do not require PostgreSQL:

```sh
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-prototype-venv \
UV_LINK_MODE=copy \
uv run --locked --python 3.12 pytest -q
```

The Go tests put the Python virtual environment in a temporary directory. They do not leave a repository-local `.venv`.

## Production gaps

- The repository collector and migration `0001` still use contract version 1. The collector needs a tested version-1 to version-2 bridge before any production migration can use this schema.
- `schema.sql` is prototype-only. Its startup guard refuses to replace an existing contract version 1 database.
- The route `GET /v1/players/{player_tag}` is a candidate only. It is not an approved production API contract.
- This slice does not include a production package layout, deployment units, full worker supervision, metrics, tracing, health probes, retention jobs, or database migration rollback tooling.
- This slice does not include Discord behavior, accounts, battle processing, ranked-day reconciliation, leaderboards, exports, or overlays.
