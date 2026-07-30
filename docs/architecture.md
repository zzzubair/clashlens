# Architecture

## Status

Clash Lens has a confirmed Phase 1 architecture and initial runtime split. PostgreSQL is the primary structured database, Go owns official API collection, Python owns domain processing, the public API, and analytics, and TypeScript owns the website. Frameworks, remaining infrastructure products, hosting, and cloud providers remain open.

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
- Use Python core runtimes for observation processing, canonical battle linking, ranked-day reconciliation, army decoding and classification, snapshot generation, analytics, accounts, integrations, and the public API.
- Run the Python public API and Python background processing as separate runtime roles so worker failures do not take the API or website offline.
- Use a TypeScript website that consumes the Python public API. Do not reproduce domain calculations or confidence rules in browser code.
- The Go collector stores evidence but does not create canonical battles, infer shields or automatic defense, classify armies, reconcile ranked days, or calculate product analytics.
- Keep one authoritative schema-migration stream and shared versioned contracts across Go, Python, and TypeScript. Do not let each runtime invent its own meaning for shared fields.
- The eventual Discord runtime shape remains open because the exact Discord interaction model is not yet specified.
- Treat these as focused runtime roles within one product, not independently designed microservices. Add further service boundaries only after measured scaling or isolation needs prove them.

### Structured Data and Raw Evidence

- Use PostgreSQL as the primary structured datastore for Phase 1 operational data and analytics.
- Give each player a database-generated internal numeric identifier for relational references.
- Keep the normalized Clash player tag unique and use it as the player's public identity. Never expose the internal player identifier through public pages or APIs.
- Use a separate immutable object or blob archive for untouched official API response bodies.
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
- Reserve a fifth API key for newly submitted tags and explicit user-requested live refreshes so interactive traffic is not blocked behind the population-wide collection cycle.
- Route interactive refreshes through the Go collector and the same raw-evidence pipeline; the Python API and TypeScript website must never receive or use a Supercell API key directly.
- Return the latest saved player representation and its freshness immediately from the Python API, then expose refresh progress so the TypeScript website can update after the new observation is processed.
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

- API request volume and per-key throttling.
- Queue depth, oldest due job, retry volume, and failed work.
- Player-profile and battle-log freshness.
- Incomplete polling attempts and unresolved reconciliation.
- Raw-archive write failures or checksum mismatches.
- Snapshot build progress, publication time, coverage, and correction count.
- Database health, backup success, and recovery readiness.

Recovery-time objectives, recovery-point objectives, backup retention, alert thresholds, and monitoring products remain open decisions.

## Open Technology and Integration Decisions

The following choices remain open:

- Raw object or blob archive product.
- Detailed internal module boundaries and dependency rules.
- Go collector libraries and packaging.
- Python backend framework and public API style.
- TypeScript website framework, rendering model, and packaging.
- Discord interaction and runtime model.
- Authentication provider integration and account model.
- Google Sheets integration.
- OBS delivery model.
- Deployment packaging and hosting model.
- Cloud or infrastructure provider.
- Monitoring, logging, and alerting products.
- Backup retention and tested recovery procedures.

Present trade-offs and obtain maintainer approval before confirming any of these choices.
