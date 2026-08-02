# Collector prototype runbook

Status: executable prototype for GitHub issue #2. This is not a production deployment specification.

## Boundary

The collector gets raw player-profile and battle-log responses from the official Clash of Clans API. It archives each exact response body before it publishes a PostgreSQL observation. It then creates a durable `python_processing_jobs` record.

The collector does not parse battle meaning. It does not link battles, reconcile ranked days, infer shields or automatic defenses, classify armies, or calculate product analytics. Python owns those tasks. See [architecture.md](architecture.md) and [ADR 0001](adr/0001-separate-collection-from-domain-processing.md).

## Stack

- Go executable
- PostgreSQL through `pgx/v5`
- S3-compatible archive through `minio-go/v7`
- Embedded real PostgreSQL and an in-process fake S3 service for tests

The stack decision and current license checks are in [collector-prototype-stack.md](research/collector-prototype-stack.md).

## Schema ownership

The collector checks the shared schema contract version at startup. It never applies migrations.

`testdata/contract.sql` is only a prototype test contract. Do not apply it as a production migration. The future shared database owner must convert the approved contract into normal migrations.

## Build and test

Use the repository Go version from `go.mod`.

```bash
go build -o ./bin/collector ./cmd/collector
go test ./...
go test -race ./...
go vet ./...
```

The tests do not require Docker. The integration tests download and run a temporary real PostgreSQL binary through `embedded-postgres`. They use a small in-process fake S3-compatible HTTP service. Production code does not depend on either test service.

## Required configuration

Prefer API-key files. Do not put credentials in command arguments, logs, source files, or committed environment files.

| Variable | Purpose |
|---|---|
| `CLASHLENS_DATABASE_URL` | PostgreSQL connection URL for the shared schema. |
| `CLASHLENS_ARCHIVE_ENDPOINT` | S3-compatible endpoint as `host:port`. |
| `CLASHLENS_ARCHIVE_BUCKET` | Existing raw-evidence bucket. |
| `CLASHLENS_ARCHIVE_ACCESS_KEY` | Archive access key. |
| `CLASHLENS_ARCHIVE_SECRET_KEY` | Archive secret key. |
| `CLASHLENS_NORMAL_API_KEY_FILES` | Comma-separated `label=/protected/path` entries. Four normal keys are required by default. |
| `CLASHLENS_INTERACTIVE_API_KEY_FILES` | Comma-separated `label=/protected/path` entries. One interactive key is required by default. |

Inline `CLASHLENS_NORMAL_API_KEYS` and `CLASHLENS_INTERACTIVE_API_KEYS` values also use comma-separated `label=secret` entries. Use them only in isolated tests.

Useful optional variables:

| Variable | Default | Purpose |
|---|---:|---|
| `CLASHLENS_SCHEMA_VERSION` | `1` | Required shared-contract version. |
| `CLASHLENS_OFFICIAL_API_ORIGIN` | `https://api.clashofclans.com` | Official API origin. |
| `CLASHLENS_ARCHIVE_SECURE` | `true` | Use TLS for the archive. |
| `CLASHLENS_HEALTH_LISTEN` | empty | Listen address for `/livez`, `/readyz`, and `/metrics`. Example: `127.0.0.1:8081`. |
| `CLASHLENS_COLLECTOR_VERSION` | `prototype` | Version stored with each observation. |
| `CLASHLENS_POLL_CYCLE` | `5m` | Regular collection cycle. |
| `CLASHLENS_LEASE_DURATION` | `30s` | Work lease duration. Active workers renew it. |
| `CLASHLENS_MAXIMUM_RETRIES` | `4` | Endpoint retry limit. |
| `CLASHLENS_RETRY_BASE_DELAY` | `500ms` | Initial retry delay. |
| `CLASHLENS_RETRY_MAXIMUM_DELAY` | `30s` | Maximum retry delay. |
| `CLASHLENS_RETRY_JITTER_FRACTION` | `0.2` | Retry jitter from `0` through `1`. |
| `CLASHLENS_INTERACTIVE_COOLDOWN` | `30s` | Recent-success window for interactive intent coalescing. |
| `CLASHLENS_REQUESTS_PER_SECOND_PER_KEY` | `30` | Request limit for each process-owned key. |
| `CLASHLENS_WORKERS_PER_KEY` | `8` | Small Go worker goroutines per configured key. Tune with API latency and PostgreSQL capacity. |
| `CLASHLENS_MAXIMUM_RESPONSE_BYTES` | `4194304` | Maximum body size for one official response. |
| `CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL` | `false` | Explicit degraded-mode use of reserved interactive keys for normal work. |

`CLASHLENS_ALLOW_REDUCED_KEY_POOLS` and `CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN` exist only for isolated tests and local prototypes. Do not set them in production.

## Commands

Run the scheduler and workers in one process:

```bash
./bin/collector run --role both
```

Separate scheduler and worker processes:

```bash
./bin/collector run --role scheduler
./bin/collector run --role worker
```

PostgreSQL advisory locks enforce one live worker-process owner for each API-key secret. A second worker process with the same key fails at startup. This keeps the per-key in-memory rate limit authoritative across processes. Scheduler and maintenance processes do not own API keys.

Run one bounded scheduling and drain cycle:

```bash
./bin/collector run --role both --once
```

Enqueue an initial collection or interactive intent:

```bash
./bin/collector enqueue --type initial_collection --tag '#PLAYER'
./bin/collector enqueue --type live_refresh --tag '#PLAYER'
```

Use `--bypass-cooldown` only for an explicit operator override.

The prototype contract records each interactive intent as `created`, `coalesced`, `cooldown_hit`, or `partial_retry`. This supports durable coalescing and cooldown metrics without storing response bodies.

Inspect and recover durable failures:

```bash
./bin/collector maintenance list-failed --limit 100
./bin/collector maintenance list-leases --limit 100
./bin/collector maintenance requeue --job-id 123
./bin/collector maintenance reset-processing --processing-job-id 456
```

Maintenance output contains IDs, categories, states, and times. It does not contain API-key secrets or raw response bodies.

## Health and metrics

When `CLASHLENS_HEALTH_LISTEN` is set:

- `/livez` reports that the process can serve requests.
- `/readyz` returns JSON with separate `postgresql`, `archive`, `normal_api_keys`, and `interactive_api_keys` states.
- `/metrics` reports queue depth and age, active and expired leases, incomplete attempts, retries, terminal failures, endpoint freshness, reset progress, live-refresh latency and reuse counts, API outcomes, storage errors, and per-key rate, cooldown, and quarantine state by non-secret label.

Keep this listener on a private operations network or localhost. It has no built-in authentication.

## Failure and shutdown rules

- A response becomes an observation only after exact archive bytes pass SHA-256 verification.
- Profile and battle-log outcomes are independent. A successful endpoint remains durable when its sibling fails.
- Retry jobs target only incomplete endpoints.
- Execution tokens fence late workers after lease loss.
- Workers renew active leases. Graceful shutdown cancels requests, releases unfinished leases to `pending`, and does not mark unfinished work complete.
- When a required key pool has no healthy key, workers leave new work unclaimed. Normal workers do not use interactive keys unless the degraded-mode variable is explicitly true.
