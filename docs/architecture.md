# Architecture

Clash Lens is one product in one repository with explicit runtime boundaries.
This document describes those durable boundaries; it is not a product roadmap
or a record of current implementation status.

The code, migrations, fixtures, and tests are the source of truth for behavior.
[`domain.md`](domain.md) defines game meanings and evidence rules. The
deployment runbook defines host operations. If prose conflicts with executable
behavior, report the conflict and follow the executable contract.

## Runtime ownership

### Go collector

Go owns official API transport: scheduling, key-rate limiting, retries, and
request/response handling. It hashes and writes each exact response to the raw
archive, then appends the observation metadata and durable processing handoff.
Go must not interpret battle meaning, reconcile ranked days, infer shields or
automatic defenses, decode armies, or calculate product analytics.

### Python application

Python owns interpretation of durable observations and all product-domain
behavior: canonical battles, ranked days, classifications, snapshots,
analytics, accounts, integrations, and the private service API. It also owns
replay of archived evidence with explicit parser and rule versions. Replay
never calls the official API or changes the original observation.

The private API and background workers may run as separate processes from the
same Python codebase. A worker or integration failure must not take the API
offline. The private API is also the only process allowed to make the
separately scoped player-token verification request.

### TypeScript website and browser

The TypeScript backend owns browser sessions, provider login, and
presentation-oriented calls to the private Python API. The browser talks only
to that backend. Neither the browser nor TypeScript may access PostgreSQL or
the raw archive directly, or reimplement Python-owned domain calculations,
confidence rules, rankings, or cohort membership.

Discord and future integrations use the private API rather than reading the
database or archive. They remain separate processes where their availability
or resource use requires isolation.

## Durable seams

Go and Python coordinate through PostgreSQL observation metadata and durable
queues, not through in-process calls. A response is not eligible for Python
processing until its untouched bytes and observation metadata are durable. The
handoff is idempotent: retries and process restarts must not create duplicate
observations or derived records.

Python is the single owner of domain interpretation. Other runtimes may pass
validated inputs and format returned values, but they must not create a second
set of rules for battles, ranked days, eligibility, classifications,
confidence, snapshots, or analytics.

Cross-runtime requests use explicit, versioned contracts and validated
screen-oriented operations. A valid private-API caller proof identifies a
caller, but operation authorization and end-user ownership are enforced by
Python. Do not expose a generic database-query API or accept a
caller-supplied internal account identifier as proof of ownership.

Add another service boundary only when measured scaling, resource, or failure
isolation needs justify it. Runtime roles are parts of one product, not
independently designed microservices.

## Security boundary

The private network is an additional barrier, not an identity mechanism. Each
approved private-API caller has its own authenticated proof and authorization
allowlist. Resolve signed-in users to Python-owned account records from the
validated provider subject; never trust a browser-supplied account ID.

Keep API keys, archive credentials, signing secrets, and database credentials
in protected process or host secret files. Do not put them in source,
arguments, logs, metrics, traces, or durable request data. Player verification
tokens are request-only secrets: use them in memory, never persist or archive
them, and return only a sanitized result.

The browser receives public or screen-ready data only. The TypeScript backend,
Discord bot, and other integrations use the private API; they do not receive
the official collection key or archive credentials. The collector and Python
worker are the only archive readers, with access limited to their collection
and processing paths.

## Structured data and evidence

PostgreSQL owns normalized product data, durable queues, leases, migrations,
accounts, and precomputed analytics. Derived records carry the parser or
domain-rule version needed to reproduce them. Use database-generated internal
identifiers only for relations; public APIs expose stable domain identities,
not internal IDs.

The raw archive owns untouched official response bodies. Content-address each
body by a cryptographic hash and retain one immutable body per hash while
allowing many observation occurrences to reference it. Observation metadata is
append-only and records request scope, timing, status, response hash, archive
reference, and source/collector provenance. Never overwrite evidence with a
later response; track processing state separately.

Keep inferred or reconstructed facts as versioned derived states with links to
the observations that support them. Preserve uncertainty, partial coverage,
and source-contract changes instead of silently filling gaps. Data contracts
must distinguish official observations from Clash Lens-derived rankings or
analytics, while the public leaderboard remains one tracked list without
source badges.

Use PostgreSQL-backed durable queues in the initial system. Workers claim work
with leases and fencing, and handlers are safe to run more than once. Keep
backups and recovery procedures in [`deployment.md`](deployment.md); do not
make the runtime boundary depend on an unowned queue or warehouse.
