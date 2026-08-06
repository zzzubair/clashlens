# Go collector prototype runbook

**Status:** executable prototype for GitHub issue #2. This is not a
production deployment specification.

## 1. Know the boundary before you run it

The prototype performs this collection path:

1. The Go scheduler creates durable collection work.
2. Go requests player-profile and battle-log responses from the official Clash
   of Clans API.
3. Go hashes and archives each exact response body. It records the PostgreSQL
   observation only after archive integrity checks pass.
4. Go creates one durable `python_processing_jobs` row for each observation.

The collector does not interpret battle meaning. It does not create canonical
battles, reconcile ranked days, infer shields or automatic defense, classify
armies, or calculate product analytics. Python owns those rules. See
[architecture.md](architecture.md), [domain.md](domain.md), and
[ADR 0001](adr/0001-separate-collection-from-domain-processing.md).

The current prototype has these limits:

- `reset_profile` requests only the profile endpoint. It does not create the
  paired profile and battle-log reset-baseline sweep needed to prove complete
  ranked-day evidence.
- The current schema and executable do not collect the official global Top-200
  response as a player-independent observation.
- The collector checks the shared contract version. It never applies a
  migration.
- [`testdata/contract.sql`](../testdata/contract.sql) is a prototype test
  contract. [`deploy/migrations/0001_collector.sql`](../deploy/migrations/0001_collector.sql)
  is the current deployment migration. Neither file implements the accepted
  future version-2 contract.

Completion check: before a run, you can state whether you are testing raw
collection and durable handoff, or testing Python-owned domain processing. This
runbook covers only the first path.

## 2. Confirm the prototype stack

The executable uses:

- Go, with the repository version from [`go.mod`](../go.mod);
- PostgreSQL through `pgx/v5`;
- an S3-compatible archive through `minio-go/v7`;
- embedded real PostgreSQL and an in-process fake S3 service in tests.

The package and current license checks are in
[collector-prototype-stack.md](research/collector-prototype-stack.md).
Integration tests download a temporary PostgreSQL binary through
`embedded-postgres`. They do not require Docker. Production code does not
depend on either test service.

Completion check: Go is available at the version required by `go.mod`, and the
test environment can download the embedded PostgreSQL binary when an
integration test needs it.

## 3. Build and run the Go checks

Run these commands from the repository root, in order:

```bash
go build -o ./bin/collector ./cmd/collector
go test ./...
go test -race ./...
go vet ./...
```

The build creates `./bin/collector`. The test commands cover the collector,
store, archive, leases, retries, shutdown, readiness, and black-box paths.
The race run checks concurrent worker behavior. `go vet` checks the compiled
packages for static issues.

Completion check: all four commands exit with status `0`; the binary exists at
`./bin/collector`; and no command prints a credential or raw response body.

## 4. Configure credentials and runtime settings

Prefer file-backed credentials. Do not place credentials in command arguments,
logs, source files, or committed environment files. The source of truth for
parsing, defaults, and limits is
[`internal/collector/config.go`](../internal/collector/config.go).

### Required settings

| Variable | Use |
|---|---|
| `CLASHLENS_DATABASE_URL` or `CLASHLENS_DATABASE_URL_FILE` | PostgreSQL connection URL. |
| `CLASHLENS_ARCHIVE_ENDPOINT` | S3-compatible archive `host:port`; no scheme or path. |
| `CLASHLENS_ARCHIVE_BUCKET` | Existing raw-evidence bucket. |
| `CLASHLENS_ARCHIVE_ACCESS_KEY` or `CLASHLENS_ARCHIVE_ACCESS_KEY_FILE` | Archive access credential. |
| `CLASHLENS_ARCHIVE_SECRET_KEY` or `CLASHLENS_ARCHIVE_SECRET_KEY_FILE` | Archive secret credential. |
| `CLASHLENS_NORMAL_API_KEY_FILES` | Comma-separated `label=path` entries for normal API keys. Four are required unless reduced pools are explicitly enabled. |
| `CLASHLENS_INTERACTIVE_API_KEY_FILES` | Comma-separated `label=path` entries for interactive keys. One is required unless reduced pools are explicitly enabled. |

The collector also accepts inline `CLASHLENS_NORMAL_API_KEYS` and
`CLASHLENS_INTERACTIVE_API_KEYS` values in comma-separated `label=secret`
syntax. Use inline values only in isolated tests. The Fedora deployment rejects
inline API keys and requires file-backed keys.

`CLASHLENS_OFFICIAL_API_ORIGIN` defaults to
`https://api.clashofclans.com` and must be an absolute origin without a path.
`CLASHLENS_OFFICIAL_API_PROXY_URL` is optional; when set, it must be an HTTP or
HTTPS origin without credentials, path, query, or fragment.

### Operational settings

| Variable | Default | Use |
|---|---:|---|
| `CLASHLENS_SCHEMA_VERSION` | `1` | Required shared-contract version. |
| `CLASHLENS_ARCHIVE_SECURE` | `true` | Use TLS for the archive. |
| `CLASHLENS_HEALTH_LISTEN` | empty | Listen address for `/livez`, `/readyz`, and `/metrics`; for example `127.0.0.1:8081`. |
| `CLASHLENS_COLLECTOR_VERSION` | `prototype` | Version stored with each observation. |
| `CLASHLENS_POLL_CYCLE` | `5m` | Regular scheduling cycle. |
| `CLASHLENS_LEASE_DURATION` | `30s` | Collection-job lease duration. |
| `CLASHLENS_MAXIMUM_RETRIES` | `4` | Endpoint retry limit. |
| `CLASHLENS_RETRY_BASE_DELAY` | `500ms` | Initial retry delay. |
| `CLASHLENS_RETRY_MAXIMUM_DELAY` | `30s` | Maximum retry delay. |
| `CLASHLENS_RETRY_JITTER_FRACTION` | `0.2` | Retry jitter from `0` through `1`. |
| `CLASHLENS_INTERACTIVE_COOLDOWN` | `30s` | Recent-success window for interactive intent coalescing. |
| `CLASHLENS_REQUESTS_PER_SECOND_PER_KEY` | `30` | Internal prototype safety budget for each process-owned key. The valid range is `1` through `30`; this is not a published Supercell limit. |
| `CLASHLENS_WORKERS_PER_KEY` | `8` | Go worker goroutines per configured key. |
| `CLASHLENS_MAXIMUM_RESPONSE_BYTES` | `4194304` | Maximum body size for one official response. |
| `CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL` | `false` | Prototype-only degraded use of interactive keys for normal work. |

The collector also parses scheduler, worker-idle, request-timeout, and schedule
batch settings. Read `config.go` before changing them. The accepted Phase 1
shared-key traffic gate is not implemented by this Go-only prototype.

`CLASHLENS_ALLOW_REDUCED_KEY_POOLS` and
`CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN` exist for isolated tests and local
prototypes. Keep them unset in production. `CLASHLENS_ALLOW_INSECURE_TEST_ORIGIN`
permits a non-HTTPS official API origin only for test use; it does not make the
provider connection safe.

Completion check: each configured secret is available through a file or an
isolated test value, `CLASHLENS_SCHEMA_VERSION` matches the database contract,
and the official API and archive settings pass the validation in `config.go`.

## 5. Run the scheduler and workers

Run both roles in one process for the normal prototype path:

```bash
./bin/collector run --role both
```

Run the roles separately when testing process ownership or failure isolation:

```bash
./bin/collector run --role scheduler
./bin/collector run --role worker
```

The `run` command accepts `--role both`, `--role scheduler`, or
`--role worker`. The `both` and `worker` roles acquire PostgreSQL advisory-lock
ownership for each API-key secret. A second Go worker process using the same
key fails at startup. The scheduler does not acquire key ownership or send API
requests, but the current shared CLI still loads the full collector
configuration. Maintenance commands load only the database URL and optional
schema version.

Completion check: the selected process stays running, creates no duplicate
key owner, and exposes the health listener only when
`CLASHLENS_HEALTH_LISTEN` is set.

## 6. Run one bounded cycle

Use this command for a deterministic scheduler pass followed by a worker drain:

```bash
./bin/collector run --role both --once
```

`--once` accepts no positional arguments. It schedules due regular work and a
reset sweep, drains available interactive and normal work, and returns a JSON
result with `"status":"complete"`.

Completion check: the command exits with status `0` and returns
`{"status":"complete"}` without leaving a live worker process.

## 7. Enqueue collection intent

Use normalized uppercase tags and choose one of the two supported work types:

```bash
./bin/collector enqueue --type initial_collection --tag '#PLAYER'
./bin/collector enqueue --type live_refresh --tag '#PLAYER'
```

Use `--bypass-cooldown` only for an explicit operator override:

```bash
./bin/collector enqueue --type live_refresh --tag '#PLAYER' --bypass-cooldown
```

The command accepts `--type`, `--tag`, and `--bypass-cooldown`; it accepts no
positional arguments. It records interactive intent outcomes as `created`,
`coalesced`, `cooldown_hit`, or `partial_retry`. The response contains the
resulting job and attempt IDs and a reuse flag. It does not contain response
bodies.

Completion check: the command returns JSON with `job_id`, `attempt_id`, and
`reused`, and the matching durable intent event is visible in PostgreSQL.

## 8. Inspect and recover durable failures

Run the supported maintenance commands:

```bash
./bin/collector maintenance list-failed --limit 100
./bin/collector maintenance list-leases --limit 100
./bin/collector maintenance requeue --job-id 123
./bin/collector maintenance reset-processing --processing-job-id 456
```

`list-failed` reports failed collector jobs. `list-leases` reports expired
collector leases. `requeue` takes a positive collector `--job-id`.
`reset-processing` takes a positive Python `--processing-job-id`. Maintenance
uses only `CLASHLENS_DATABASE_URL` or its file setting and the optional schema
version; archive and API-key outages do not block these database operations.

The output contains IDs, categories, states, and times. It does not contain
API-key values or raw response bodies. Use a recovery command only after
checking the failure category and preserving the evidence that caused it.

Completion check: the inspection output is safe to share with an operator, and
a recovery command changes only the requested durable job state.

## 9. Observe health and metrics

Set `CLASHLENS_HEALTH_LISTEN` to expose the operational listener. It has no
built-in authentication, so bind it to localhost or a private operations
network.

- `GET /livez` returns `ok` when the process can serve requests.
- `GET /readyz` returns JSON with `ready` and separate `components` entries for
  `postgresql`, `archive`, `normal_key_pool`, and `interactive_key_pool`.
- `GET /metrics` returns Prometheus text with queue, lease, retry, endpoint,
  reset, refresh, API-outcome, storage-error, and non-secret per-key limiter
  metrics.

At startup, the collector writes and verifies one empty
`readiness/<process-token>` object. Readiness then checks that the configured
archive bucket is still available.

Completion check: each health request returns only its documented health or
metric data, and no health listener is exposed on a public interface.

## 10. Apply failure and shutdown rules

Keep these rules beside the operation that can fail:

- Archive exact bytes and verify their SHA-256 before committing an observation.
- Treat profile and battle-log endpoint outcomes independently. Preserve a
  successful endpoint when its sibling fails and retry only the incomplete
  endpoint.
- Use lease tokens and lease-expiry checks as the write fence. An expired lease
  cannot renew or publish a durable result, even before another worker claims
  it.
- Renew active leases. On graceful shutdown, cancel requests, release
  unfinished leases to `pending`, and leave unfinished work incomplete.
- When a required key pool has no healthy key, leave new work unclaimed. Normal
  work uses interactive keys only when
  `CLASHLENS_ALLOW_INTERACTIVE_FOR_NORMAL=true` is explicitly set.

The current prototype uses an in-memory per-key limiter plus PostgreSQL
advisory ownership. Before Python receives the shared interactive key, Go and
Python must use the PostgreSQL traffic gate defined in
[architecture.md](architecture.md). Do not use this prototype's process-local
counters as proof of the combined Go/Python limit.

Completion check: after shutdown or a dependency failure, durable evidence is
still present, unfinished work is runnable or inspectably failed, and no late
worker can publish through an expired lease.

## Production gap

Do not promote this prototype as the production collector contract. The
accepted architecture still needs the version-1-to-version-2 bridge, the
paired reset-baseline sweep, the global Top-200 observation, and the shared
traffic gate. Python must implement the domain interpretations and its durable
processing contract. Record those changes in the approved source documents
before changing this runbook.
