# Collector Prototype Stack Decision

**Research date:** 2026-08-02

**Scope:** GitHub issue [#2](https://github.com/zzzubair/ClashLens/issues/2), the Go collector only.

## Decision

Use these production client modules:

- `github.com/jackc/pgx/v5` at `v5.10.0`.
- `github.com/minio/minio-go/v7` at `v7.2.1`.

Hide the object client behind a small owned `ObjectArchive` interface. Keep hashing,
retry policy, archive immutability, and the archive-then-PostgreSQL transaction in
collector code. Do not put domain processing in this interface. This keeps the Go
and Python boundary in [ADR 0001](../adr/0001-separate-collection-from-domain-processing.md).

For the primary black-box test seam, use:

1. Fedora PostgreSQL, started as a real service for manual work or as a disposable
   local cluster for each test run.
2. An in-process `gofakes3` `v1.2.0` server with its `s3mem` backend and
   `httptest.NewServer`.
3. An owned `httptest.NewServer` for the fake official API.

This stack needs no Docker, JVM, or always-on object-store process. Keep `gofakes3`
under test-only code. Do not import it into the collector binary.

## Why this is the smallest practical choice

`pgx` is a PostgreSQL driver and toolkit. It gives the collector direct access to
transactions, `FOR UPDATE SKIP LOCKED`, leases, and a bounded connection pool
without an ORM or a `database/sql` adapter. Its v5.10.0 module is MIT licensed and
has an active, non-archived upstream repository ([metadata](https://api.github.com/repos/jackc/pgx),
[go.mod](https://raw.githubusercontent.com/jackc/pgx/v5.10.0/go.mod),
[license](https://raw.githubusercontent.com/jackc/pgx/v5.10.0/LICENSE)).

`minio-go` is a focused S3-compatible client. Its constructor accepts an endpoint,
credentials, TLS choice, and path-style bucket lookup. This fits a provider-neutral
archive and a local fake without the AWS configuration chain. Version `v7.2.1` is
Apache-2.0 licensed, tagged, and current in its package documentation
([package](https://pkg.go.dev/github.com/minio/minio-go/v7),
[API](https://docs.min.io/aistor/developers/sdk/go/api),
[go.mod](https://raw.githubusercontent.com/minio/minio-go/v7.2.1/go.mod),
[license](https://raw.githubusercontent.com/minio/minio-go/v7.2.1/LICENSE)).

The AWS SDK for Go v2 remains a valid later option. It is Apache-2.0 licensed and
actively maintained ([repository](https://api.github.com/repos/aws/aws-sdk-go-v2),
[license](https://raw.githubusercontent.com/aws/aws-sdk-go-v2/main/LICENSE.txt)).
It has better AWS service coverage. It also adds a generated S3 service module and,
in the usual setup, separate `config` and `credentials` modules. Custom endpoints
are supported, but AWS documents endpoint resolution as an advanced topic and
recommends care with S3 endpoint behavior ([AWS endpoint guide](https://docs.aws.amazon.com/sdk-for-go/v2/developer-guide/configure-endpoints.html)).
For this collector's narrow S3-compatible contract, that is extra configuration and
build surface. Use AWS SDK v2 if AWS-specific features or other AWS services become
a real requirement.

## Memory and developer-experience trade-offs

**Facts.** The following Go module proxy sizes are download sizes, not process RSS:

| Sampled module | Zip size |
|---|---:|
| `pgx/v5@v5.10.0` | 602,168 bytes ([module](https://proxy.golang.org/github.com/jackc/pgx/v5/@v/v5.10.0.zip)) |
| `minio-go/v7@v7.2.1` | 519,098 bytes ([module](https://proxy.golang.org/github.com/minio/minio-go/v7/@v/v7.2.1.zip)) |
| AWS `service/s3@v1.106.3` | 887,919 bytes ([module](https://proxy.golang.org/github.com/aws/aws-sdk-go-v2/service/s3/@v/v1.106.3.zip)) |
| AWS `config@v1.32.34` | 132,548 bytes ([module](https://proxy.golang.org/github.com/aws/aws-sdk-go-v2/config/@v/v1.32.34.zip)) |
| AWS `credentials@v1.19.33` | 76,174 bytes ([module](https://proxy.golang.org/github.com/aws/aws-sdk-go-v2/credentials/@v/v1.19.33.zip)) |

**Estimate.** `minio-go` should have the lower build and configuration cost for this
small S3 surface. Both clients are Go libraries in the same process, so the live
RSS difference is not established by these sizes. Request concurrency, response
buffers, and PostgreSQL connections will matter more. No candidate publishes a
controlled, apples-to-apples RSS benchmark. Measure RSS after the first prototype
works; do not treat the estimate as a capacity guarantee.

Set `pgxpool.Config.MaxConns` explicitly. The upstream default is the greater of
four or `runtime.NumCPU()`, which can be too high for a small always-on laptop
([pgxpool docs](https://pkg.go.dev/github.com/jackc/pgx/v5/pgxpool),
[source](https://raw.githubusercontent.com/jackc/pgx/v5.10.0/pgxpool/pool.go)).
Use a small value and measure. Do not hold a database transaction open during HTTP
or archive calls.

`gofakes3` is the smallest useful S3 test server found for this seam. Its official
example uses `s3mem.New`, `gofakes3.New`, and `httptest.NewServer`. Its README
also states that it is for local development and testing, that significant S3 API
parts are not implemented, and that it has no production correctness, performance,
or security guarantee ([README](https://raw.githubusercontent.com/johannesboyne/gofakes3/v1.2.0/README.md),
[license](https://raw.githubusercontent.com/johannesboyne/gofakes3/v1.2.0/LICENSE),
[repository metadata](https://api.github.com/repos/johannesboyne/gofakes3)).
That warning is acceptable only because the black-box contract needs a small set of
operations. Test the exact `PutObject`, `StatObject`/`HeadObject`, and any read
operation used by the collector. Wrap the handler with a small test switch when a
scenario must force an archive `5xx` or timeout.

## Local integration-test stack on Fedora 44

- Fedora provides Go `1.26.1-1.fc44` ([package data](https://packages.fedoraproject.org/pkgs/golang/golang/fedora-44.html)). This is suitable for the selected modules.
- Fedora provides `postgresql-server-18.3-1.fc44` ([package data](https://packages.fedoraproject.org/pkgs/postgresql18/postgresql-server/fedora-44.html)). Use it for the real database.
- For an isolated test run, create a temporary data directory with `initdb`, start it with `pg_ctl` on an ephemeral port, create one test database, run the executable, and stop it with `pg_ctl`. These are PostgreSQL-supported tools ([`initdb`](https://www.postgresql.org/docs/current/app-initdb.html), [`pg_ctl`](https://www.postgresql.org/docs/current/app-pg-ctl.html)).
- The Fedora `golang-github-jackc-pgx-devel` page is an old v4 package and uses the non-v5 import path ([package data](https://packages.fedoraproject.org/pkgs/golang-github-jackc-pgx/golang-github-jackc-pgx-devel/fedora-44.html)). The Fedora MinIO compatibility package lists v7.0.82 for Fedora 43 ([package data](https://packages.fedoraproject.org/pkgs/golang-github-minio/compat-golang-github-minio7-devel/fedora-43.html)). Use Go modules with the exact pins above. Do not silently substitute those distro packages.

The test must assert the issue's real durability order: complete body, hash, archive
write or verified reuse, one PostgreSQL transaction for the observation and Python
job, then acknowledgement. Real PostgreSQL is important for lease expiry,
transactions, uniqueness, and skip-locked behavior. An in-process fake S3 keeps the
local test memory cost low and makes archive failures controllable.

## Rejected or fallback options

- **AWS SDK for Go v2 as the first client:** maintained and strong for AWS, but more
  configuration and module surface than this provider-neutral prototype needs.
- **Adobe S3Mock `5.1.0`:** active and Apache-2.0 licensed ([repository metadata](https://api.github.com/repos/adobe/S3Mock),
  [README](https://raw.githubusercontent.com/adobe/S3Mock/main/README.md),
  [license](https://raw.githubusercontent.com/adobe/S3Mock/5.1.0/LICENSE)). It is a
  JVM-based mock with Docker/Testcontainers/JUnit integration. Keep it as a fallback
  when `gofakes3` lacks an operation or response behavior that the collector must
  verify. It is not the smallest no-Docker Go path.
- **MinIO server:** do not use it as the default local fake. The upstream repository
  is archived, its last push was 2026-04-24, and it is AGPL-3.0 licensed
  ([metadata](https://api.github.com/repos/minio/minio),
  [license](https://raw.githubusercontent.com/minio/minio/master/LICENSE)). This is
  separate from the Apache-2.0 `minio-go` client. The server is also a full object
  store, not a small test seam.

## First compatibility gate

Before confirming the choice, run issue #2 acceptance scenarios 1, 5, 6, and 7
through the executable with `pgx` and `minio-go` against `gofakes3`. If the exact
archive operations fail, record the missing operation and switch only the test fake
to S3Mock or an owned minimal `httptest` handler. Do not replace the real PostgreSQL
part with a SQL mock, and do not claim full S3 compatibility from a fake that warns
that its API is incomplete.

## Project sources

- [`zzzubair/ClashLens#2`](https://github.com/zzzubair/ClashLens/issues/2)
- [Architecture](../architecture.md)
- [ADR 0001](../adr/0001-separate-collection-from-domain-processing.md)
