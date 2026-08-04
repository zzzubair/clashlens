# Architecture

## Status

Clash Lens has a confirmed Phase 1 architecture and runtime split. PostgreSQL is the primary structured database, Go owns official API collection, Python owns domain processing, analytics, accounts, integrations, and a private service API, and TypeScript owns the public website and its backend. The Phase 1 Python stack is confirmed. The TypeScript framework, remaining infrastructure products, and cloud providers remain open.

The accepted shape is one repository and one logical product with independently running roles, a separate raw-evidence archive, durable staggered ingestion, and versioned precomputed analytics. Technology choices must preserve the product and domain rules rather than redefine them.

The runtime-boundary rationale is recorded in [ADR 0001](adr/0001-separate-collection-from-domain-processing.md).

## Product and Domain Constraints

Any implementation must support:

- Broad official API collection for actively tracked Legend I players.
- Timestamped raw battle observations and untouched response-body preservation.
- Idempotent ingestion and battle deduplication.
- Reproducible and versioned trophy allocation, army classification, and analytics.
- Explicit data coverage, freshness, and confidence states.
- Frozen and live Tracked Players leaderboard views with clear observation provenance.
- Public player pages, meta analytics, and official top-200 rank provenance on the Tracked Players leaderboard.
- Optional Discord and Google authentication, multi-account views, and groups.
- Website, Discord, Google Sheets, and OBS surfaces with consistent data meanings.
- Free public access to player data and analytics.

`docs/domain.md` owns the exact meanings of players, observations, events, adjustments, snapshots, cohorts, and confidence states.

## Accepted Phase 1 Shape

### Repository and Runtime Roles

- Use one repository and one logical product with explicit internal module and runtime boundaries.
- Use a Go collector runtime for poll scheduling, API-key rate limiting, official API requests, retries, raw-response archiving, and append-only observation recording.
- Use one Python codebase for observation processing, canonical battle linking, ranked-day reconciliation, army decoding and classification, snapshot generation, analytics, accounts, integrations, and a private service API.
- Run the private Python API, one general background worker, and the Discord bot as separate processes from the same Python codebase. A worker or bot failure must not take the private API or website offline.
- Let the general Python worker handle different background job types, including observation processing, replay, snapshot generation, analytics, and exports. Add specialized worker programs only when measured delay, resource use, or isolation needs justify them.
- Use private HTTP with JSON between the Python API and its trusted consumers. Do not publish the Python API outside the private Podman network, and do not add a service token while all callers remain on that trusted network.
- Use FastAPI for the private HTTP API, Pydantic for request and response validation, psycopg 3 with direct SQL for PostgreSQL access, discord.py for the bot, pytest for tests, and uv for dependency locking. Do not add Django, Celery, Redis, or an SQLAlchemy ORM in Phase 1.
- Design the private API around product operations that return screen-ready data: player pages and daily logs, refresh submission and status, live and frozen leaderboards, analytics, account and group management, verified player links, and exports. Do not add a generic database-query API.
- Use a TypeScript website with a backend. The browser communicates only with the TypeScript website backend, which calls the private Python API. The browser must never call Python directly.
- Require the TypeScript website backend, Discord bot, and future product integrations to use the private Python API. They must not access PostgreSQL or the raw archive directly.
- Keep product meanings and calculations in Python. The TypeScript website may format and present returned results, but it must not reproduce domain calculations, confidence rules, cohort membership, rankings, or analytics.
- Let the TypeScript website backend own Discord or Google login and browser sessions. Let Python own Clash Lens account records, unique usernames, display names, saved tags, verified player-account links, groups, permissions, and other account-domain data.
- Let only the private Python API process call the official `POST /players/{playerTag}/verifytoken` endpoint to verify a player-account link. It may use the interactive Supercell API key only for this endpoint. The Python worker, Discord bot, and TypeScript website must not receive that key.
- Treat the one-time player API token as a request-only secret. Do not persist, archive, log, or include it in metrics or error details. Persist only the verification result, verification time, player tag, and Clash Lens account link.
- The Go collector stores evidence but does not create canonical battles, infer shields or automatic defense, classify armies, reconcile ranked days, or calculate product analytics.
- Let Go and Python coordinate through PostgreSQL queues and stored observation metadata. Do not add direct Go-to-Python or Python-to-Go service calls.
- Keep one authoritative schema-migration stream and shared versioned contracts across Go, Python, and TypeScript. Do not let each runtime invent its own meaning for shared fields.
- Keep the Discord bot in the Python codebase and run it as a separate process that consumes the private Python API. Its commands and response formats remain open.
- Treat these as focused runtime roles within one product, not independently designed microservices. Add further service boundaries only after measured scaling or isolation needs prove them.

### Deployment and Resource Limits

- Run the Phase 1 system on one Fedora host with approximately 16 GB of available memory and one private rootless Podman network.
- Keep the whole system memory-efficient. Use bounded queues, batches, connection pools, concurrency, and caches. Do not keep duplicate full datasets in process memory when PostgreSQL or the raw archive already owns them.
- Measure memory use for PostgreSQL, Go, each Python process, and the TypeScript website under realistic collection, replay, analytics, and website load before production activation.
- Set explicit per-process memory limits and preserve headroom for Fedora, rootless Podman, PostgreSQL maintenance, and temporary workload spikes. Exact budgets require measured evidence and remain open until load testing.
- Split a process or move a role to another host only when measured latency, memory pressure, throughput, or failure isolation proves that the single-host model is insufficient.
- Build one versioned Python container image and run the private API, general worker, and Discord bot from it with different commands.
- Apply the shared SQL migrations before starting a new application version. Keep schema changes compatible with the previous Python image for at least one release.
- Roll back application code by starting the previous compatible image. Do not automatically reverse database migrations.

### Structured Data and Raw Evidence

- Use PostgreSQL as the primary structured datastore for Phase 1 operational data and analytics.
- Give each player a database-generated internal numeric identifier for relational references.
- Keep the normalized Clash player tag unique and use it as the player's public identity. Never expose the internal player identifier through the private Python API, website responses, or public pages.
- Use a separate immutable object or blob archive for untouched official API response bodies.
- Give raw-archive access only to the Go collector and Python background worker. The private Python API, TypeScript website, Discord bot, and other integrations use processed PostgreSQL data only.
- Content-address raw response bodies by a cryptographic hash. Store one immutable body for an identical hash while allowing every observation occurrence to reference it.
- Record each completed API response as append-only observation metadata, including player, endpoint, request and response times, HTTP status, response hash, archive reference, and collector version.
- Never overwrite an earlier observation with a later response. Keep evidence fields immutable and track processing or retry state separately.
- Keep recent observation metadata in partitioned PostgreSQL storage. Older occurrence history may be compacted into the immutable archive when every timestamp, response reference, and provenance link remains reproducible.
- Process an observation only after its raw body and observation metadata are durably recorded.
- Keep poll schedules, processing queues, deduplicated battles, ranked days, leaderboard snapshots, confidence states, accounts, saved tags, groups, classifications, and precomputed summaries in PostgreSQL.
- Store inferred shielded days as versioned derived states with references to the profile and battle-log observations that support the inference.
- Keep initial analytics in the same relational database rather than introducing a separate data warehouse.
- Protect the relational database with automated backups and point-in-time recovery.
- PostgreSQL extensions may be considered individually when they solve a measured need; confirming PostgreSQL does not pre-approve any extension.
- The raw-archive product remains open.

### Player Registry and Collection Eligibility

- Seed the known-player registry with the existing approximately 12,370 tags.
- Keep known-player identity and history when a player is no longer eligible for active Legend I collection.
- Regularly poll only players currently confirmed for active Legend I tracking.
- Add every valid new tag discovered through official API observations or user submissions to the known-player registry after normalization and deduplication.
- Use Legend I validation only to decide whether a known player enters active Phase 1 collection.
- Re-evaluate inactive known tags during the Monday promotion and demotion transition and when they are rediscovered or submitted.

### Poll Scheduling and API Capacity

- Use PostgreSQL-backed durable queues for staggered per-player polling and pending observation processing rather than adding a separate queue product in Phase 1.
- Claim queue work in bounded batches with transactional leases and skip-locked semantics so multiple Go collector or Python worker instances do not claim the same available job concurrently.
- Persist unfinished and retryable collection work so collector restarts do not lose polling intent.
- Spread requests across an authorized API-key pool and enforce a configurable ceiling of 30 requests per second for each key.
- Do not impose an artificial 30-request-per-second limit across the whole system when several authorized keys are healthy.
- Slow, quarantine, or retry an individual key independently when it receives rate-limit or authentication failures.
- Do not log API keys or other credentials.
- Under normal operation, use a staggered 5-minute polling cycle and fetch both the player profile and battle log for each actively tracked player.
- Assign four API keys to normal collection, providing up to 120 requests per second while keeping each key at or below 30 requests per second.
- Share a fifth API key between Go interactive collection and Python player-token verification. Reserve at most 29 requests per second for Go and at most 1 request per second for Python so their combined configured ceiling remains at or below 30 requests per second.
- Route newly submitted tags and explicit user-requested live refreshes through the Go collector and the same raw-evidence pipeline. The only Python exception is the private API's player-token verification call. The TypeScript website must never receive or use a Supercell API key directly.
- Return the latest saved player representation and its freshness immediately through the TypeScript website backend and private Python API, then expose refresh progress so the website can update after the new observation is processed.
- Do not block the initial player-page response on a live Supercell request.
- Reset-baseline sweeps and failed-request retries may run at higher priority without changing the normal 5-minute cadence.
- Preserve a successful endpoint response when its paired request fails, mark the polling attempt incomplete, and prioritize the missing request for retry.
- Let Python workers claim durably recorded, unprocessed observations and transform them idempotently into canonical domain records.
- Apply bounded retries and backoff without discarding valid evidence or generating duplicate structured events.
- Exact worker counts, queue schemas, lease durations, retry schedules, interactive-refresh coalescing and cooldown rules, and priority weights remain open.

### Reset Baselines and Snapshot Publication

- Give reset-baseline collection higher priority than normal polling.
- During the daily no-attack window, collect profile observations for all actively tracked players and retain the latest accepted official observation available for each baseline entry.
- Record entry-level observation time, freshness, and confidence. A player without an accepted current-window observation remains explicitly stale or incomplete; atomic publication must not be treated as proof that every entry is complete or equally fresh.
- Continue serving the previously published frozen leaderboard while constructing its replacement.
- Publish a new leaderboard snapshot and its precomputed summaries atomically so no surface observes a mixture of snapshot versions.
- Preserve official API ordering where official ranks are available. Beyond those ranks, order by trophies descending and use the versioned deterministic player-tag hash defined in `docs/domain.md` for equal-trophy ties.
- Record the snapshot's ordering-rule version. Do not use per-snapshot randomness.
- Target publication at approximately 05:05 UTC on normal days and approximately 05:10 UTC on Mondays.
- Keep the domain day boundary at exactly 05:00 UTC; delayed publication does not move battle attribution into a different ranked day.
- Record corrections as new snapshot versions when later battle and trophy evidence proves an accepted baseline inconsistent.
- Keep live leaderboard calculations separate from frozen snapshots.

### Analytics

- Use versioned Python analytics jobs to precompute shared frozen-snapshot analytics once when each new snapshot is published.
- Store the precomputed summaries in the relational database so the website, Discord, Sheets, and OBS surfaces can reuse the same results.
- Support the cumulative tracked-player cohorts, rank bands, rank streaks, and centered trophy ranges defined in `docs/domain.md`.
- Persist ordered membership for every frozen snapshot so rank bands and consecutive-snapshot rank streaks are reproducible.
- Derive a rank streak by intersecting the selected rank population across every frozen daily snapshot in its period; do not flatten changing daily Top-N populations into one multi-day cohort.
- Exclude a player from a confirmed rank streak when any required daily entry is stale, missing, or uncertain.
- Keep an inferred shielded day eligible when its frozen rank is fresh and inside the selected population, and store the streak's shielded-day count.
- Keep offense and defense analytics separate and use battle-time trophy values.
- Store sample size, coverage, freshness, classification version, classification confidence, unclassified count, analytics-rule version, and snapshot identity with every summary.
- Preserve previously published analytics under their original version labels when rules change.
- Live analytics may be calculated or cached separately but must never modify a frozen snapshot's published results.
- A dedicated analytics database may be considered later only when measured workload shows that the relational database and precomputation model are insufficient.

### Recovery and Observability Requirements

The implementation must make these conditions observable even though the specific monitoring stack remains open:

- Private service request volume and official API per-key throttling.
- Queue depth, oldest due job, retry volume, and failed work.
- Player-profile and battle-log freshness.
- Incomplete polling attempts and unresolved reconciliation.
- Raw-archive write failures or checksum mismatches.
- Snapshot build progress, publication time, coverage, and correction count.
- Database health, backup success, and recovery readiness.

Recovery-time objectives, recovery-point objectives, backup retention, alert thresholds, and monitoring products remain open decisions.

### Python Verification Requirements

- Test parsers, domain rules, trophy allocation, battle deduplication, ranked-day reconciliation, account permissions, and API response contracts.
- Test worker claims, leases, fencing, idempotency, and atomic completion against real PostgreSQL rather than a SQL mock.
- Test raw-archive reads with synthetic archived responses, including missing objects and checksum mismatches.
- Run an end-to-end test from a stored observation and processing job through canonical product data and a private API response.
- Run realistic memory and load tests for the API, worker, bot, replay, analytics, and database connection pools before production activation.

## Open Technology and Integration Decisions

The following choices remain open:

- Raw object or blob archive product.
- Detailed internal module boundaries and dependency rules.
- Go collector libraries and packaging.
- Exact private HTTP/JSON routes, schema details, dependency versions, and dependency update policy.
- TypeScript website framework, server rendering details, and packaging.
- Discord commands and response formats.
- Authentication provider integration and session implementation.
- Google Sheets integration.
- OBS delivery model.
- Exact image construction, process limits, and memory budgets.
- Cloud or infrastructure provider.
- Monitoring, logging, and alerting products.
- Backup retention and tested recovery procedures.

Present trade-offs and obtain maintainer approval before confirming any of these choices.
