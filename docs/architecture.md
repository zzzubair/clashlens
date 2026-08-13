# Architecture

## Ownership

This file owns runtime responsibilities, cross-runtime contracts, confirmed technology choices, deployment shape, and open architecture decisions.

- [`docs/product.md`](product.md) owns product scope and product rules.
- [`docs/domain.md`](domain.md) owns exact domain meanings and invariants.
- [ADR 0001](adr/0001-separate-collection-from-domain-processing.md) records the reason for the Go/Python boundary.

Report a conflict between these sources. Keep an open technology choice open until the maintainer approves it.

## Status

Clash Lens has a confirmed Phase 1 architecture shape and runtime split. PostgreSQL is the primary structured database, Go owns official API collection, Python owns domain processing, analytics, accounts, integrations, and a private service API, and TypeScript owns the public website and its backend. The Phase 1 Python stack is confirmed. The website uses React Router 8 Framework Mode with server-side rendering on Node.js 24. Remaining infrastructure products and cloud providers remain open.

The accepted shape is one repository and one logical product with independently running roles, a separate raw-evidence archive, durable staggered ingestion, and versioned precomputed analytics. Technology choices must preserve the product and domain rules rather than redefine them.

The `python/` directory is the production functional-beta application layer, not a throwaway prototype. It runs the private API and the general worker from one codebase against the schema in the production migrations. The root deployment applies those migrations. The collector on main accepts contract version 2, and the deployment performs bridge-before-migration. The website on main contains the Google account code. The root deployment does not pass the login configuration, so production login stays disabled. The Discord bot role, exports, the OBS overlay, and hosted ingress remain unfinished.

## Product and domain constraints

The architecture must preserve untouched official evidence, idempotent ingestion, reproducible versioned results, visible coverage and confidence, separate frozen and live views, consistent meanings across surfaces, and free public access. `docs/product.md` owns the product commitments. `docs/domain.md` owns the exact meanings. This file owns how runtimes preserve and expose them.

## 1. Accepted runtime shape

### One product, explicit roles

- Use one repository and one logical product with explicit internal module and runtime boundaries.
- Use a Go collector runtime for poll scheduling, API-key rate limiting, official API requests, retries, raw-response archiving, and append-only observation recording.
- Use one Python codebase for observation processing, canonical battle linking, ranked-day reconciliation, army decoding and classification, snapshot generation, analytics, accounts, integrations, and a private service API.
- Run the private Python API, one general background worker, and the Discord bot as separate processes from the same Python codebase. A worker or bot failure must not take the private API or website offline.
- Let one PostgreSQL Python job list contain observation processing, replay, ranked-day reconciliation, snapshot generation, analytics, and export jobs. Let the general Python worker claim all of these job types. Add specialized worker programs only when measured delay, resource use, or isolation needs justify them.
- Treat these as focused runtime roles within one product, not independently designed microservices. Add further service boundaries only after measured scaling or isolation needs prove them.

### Runtime ownership

| Runtime or store | Owns | Must not own |
|---|---|---|
| Go collector | Poll schedules, API-key limits, official requests, retries, raw-response archiving, and append-only observation metadata | Canonical battle linking, ranked-day reconciliation, shield inference, automatic-defense inference, army classification, or product analytics |
| Python codebase | Observation processing, domain interpretation, ranked days, battles, classifications, snapshots, analytics, accounts, integrations, private API, and the Discord bot process | Official collection except the separately scoped player-token verification request |
| TypeScript website backend | Browser sessions, Discord or Google login, and presentation-oriented calls to the private Python API | PostgreSQL access, raw-archive access, or Python-owned domain calculations |
| Browser | Calls to the TypeScript website backend | Direct calls to Python, PostgreSQL, or the archive; duplicate domain rules |
| PostgreSQL | Structured product data, queues, migrations, leases, versions, and precomputed analytics | Raw untouched response bodies |
| Immutable archive | Untouched official response bodies addressed by cryptographic hash | Product queries or mutable derived state |

- Keep product meanings and calculations in Python. The TypeScript website may format and present returned results, but it must not reproduce domain calculations, confidence rules, cohort membership, rankings, or analytics.
- Keep the Discord bot in the Python codebase and run it as a separate process that consumes the private Python API. Its commands and response formats remain open.

### Private network and API transport

- Use private HTTP with JSON between the Python API and its approved consumers. Do not publish the Python API outside the private Podman network. The private network is an extra barrier, but it is not caller or user identity.
- Require the TypeScript website backend, Discord bot, and future product integrations to use the private Python API. They must not access PostgreSQL or the raw archive directly.
- Let the TypeScript website backend own Discord or Google login and browser sessions. Let Python own Clash Lens account records, unique usernames, display names, saved tags, verified player-account links, groups, permissions, and other account-domain data.
- During beta, use Google OpenID Connect Authorization Code flow with PKCE S256, `state`, `nonce`, and the `openid` scope only. Use only the validated immutable Google subject. Do not request, store, or forward Google email, profile claims, provider tokens, or a browser-supplied Clash Lens account ID.
- Keep beta browser login stateless in TypeScript. Store only the Google subject, issue time, and fixed expiry in an HMAC-protected `HttpOnly`, `Secure`, `SameSite=Lax` cookie. Expire it 24 hours after Google login and never extend it through activity. Do not add a TypeScript session row, Redis session, or provider-token store.
- Require the exact HTTPS public origin and protected Google client-secret and login-secret files when production login is enabled. Validate them before the Node server listens. Keep public pages available when login is disabled.
- Treat TypeScript name checks as early feedback only. Python must enforce the strict inappropriate-name filter for usernames, display names, and group names before production account creation or name changes are enabled.
- Design the private API around product operations that return screen-ready data: player pages and daily logs, refresh submission and status, live and frozen leaderboards, analytics, account and group management, verified player links, and exports. The player-page operation must return the current day and every available ranked-day summary in the identified current Legend season; a fixed latest-N window is not a current-season contract. Do not add a generic database-query API.

## 2. Private Python API contract

### HMAC proof

Require a new short-lived HMAC-SHA-256 proof for each private API request. Give the TypeScript website backend, Discord bot, and each future approved caller a different secret and key ID. Keep these secrets in protected process secret files. Configure the operations each caller may use; a valid signature proves caller identity but does not grant every operation.

Use the literal proof version `clashlens-hmac-v1` and literal audience `clashlens-python-private-api`. Each caller secret file contains exactly one unpadded base64url value that decodes to 32 bytes, followed by at most one LF. Reject padding, CR, other whitespace, invalid encoding, and every other decoded length.

Send exactly one of each of these headers: `X-ClashLens-Proof-Version`, `X-ClashLens-Caller`, `X-ClashLens-Key-Id`, `X-ClashLens-Issued-At`, `X-ClashLens-Expires-At`, `X-ClashLens-Request-Id`, `X-ClashLens-Provider`, `X-ClashLens-Provider-Subject`, and `X-ClashLens-Signature`. Reject a missing or duplicate proof header. The caller, key ID, provider, and provider-subject header values are the unpadded base64url encodings of their UTF-8 values. Always send both provider headers; use an empty header value for each when no user is signed, and require both to be non-empty when a user is signed. Send the version literal, decimal Unix times, canonical lowercase UUID, and unpadded base64url signature without other encoding or surrounding whitespace.

Compute the HMAC-SHA-256 over the exact ASCII bytes below. Each shown line separator is one LF byte. Use the field labels, colon separators, and field order exactly as shown. Do not add a final LF. `target` is the unpadded base64url encoding of the exact raw ASCII request-target bytes (`escaped-path`, plus `?` and the unchanged raw query string when present). `body-sha256` is the 64-character lowercase hexadecimal SHA-256 of the exact received body bytes, including the SHA-256 of zero bytes for an empty body. Method is uppercase ASCII `[A-Z]+`.

```text
clashlens-hmac-v1
caller:<caller-b64url>
key-id:<key-id-b64url>
audience:clashlens-python-private-api
method:<METHOD>
target:<raw-request-target-b64url>
body-sha256:<lowercase-hex>
issued-at:<decimal-unix-seconds>
expires-at:<decimal-unix-seconds>
request-id:<canonical-lowercase-uuid>
provider:<provider-b64url-or-empty>
provider-subject:<provider-subject-b64url-or-empty>
```

- Derive the request target in Python from the ASGI raw path and raw query-string bytes without decoding, sorting, or normalizing them. Reject a non-ASCII target, malformed field, unknown version, audience, caller, key ID, or signing method. Encode the final HMAC as unpadded base64url and compare it in constant time.
- Commit language-neutral v1 golden vectors with fixed decoded key bytes, every input field, the complete signing bytes in hexadecimal, and the expected signature. Include empty and signed-in provider fields, an empty body, percent escapes, repeated query keys, and query order. Require the TypeScript signer and Python verifier to pass the same vectors and reject a final LF, CRLF, field reordering, request-target normalization, duplicate proof headers, noncanonical base64url, and changed body bytes. The committed fixture is `testdata/private-api-hmac-v1.json`.
- Parse issued-at and expiry as canonical non-negative decimal Unix seconds with no leading zero unless the value is zero. At verification start, set `now` to the API process UTC Unix clock rounded down to an integer second. Require `1 <= expires_at - issued_at <= 30` and `issued_at - 5 <= now <= expires_at + 5`; both time-window bounds are inclusive. Reject every proof outside either inequality.
- Permit at most a current and previous key ID for one caller during rotation. Add the new key to Python, deploy the caller with the new key ID, wait longer than the proof lifetime and clock-skew allowance, then remove the old key. Never select a key only by trying every configured secret.

### Caller and operation authorization

- For a request made for a signed-in beta user, Python must resolve only a Google provider user ID to the Python-owned Clash Lens account and enforce the operation there. Do not accept a caller-supplied Clash Lens account ID as proof of ownership.
- Deny every caller-operation pair unless this Phase 1 matrix allows it. A valid signature never widens the row.
- During beta, the TypeScript backend without a user may use public player and user pages, leaderboards, analytics, and tag or refresh submission and status. With a resolved Google user, it may also manage only that user's account, saved tags, groups, and verified player links. Exports are disabled.
- Discord login and the Discord bot are disabled during beta. They have no private-API operations.
- Future integrations start with no operations until an explicit reviewed row is added.
- Operator maintenance has no private-API operation and uses the host-local boundary below.

Consume each request ID once for account changes, player-account verification, refresh submission, export submission, and other state-changing operations. Store a non-secret operation binding and sanitized outcome. A matching retry returns that outcome or current status without repeating the effect; reuse with a different caller, user, operation, target, or non-secret identity is a conflict. Read-only public operations still require caller proof, but they do not require an end-user identity.

### Player-token verification

- Let only the private Python API process call the official `POST /players/{playerTag}/verifytoken` endpoint to verify a player-account link. It may use the interactive Supercell API key only for this endpoint. The Python worker, Discord bot, and TypeScript website must not receive that key.
- Treat the one-time player API token as a request-only secret. Check the signed body hash in memory, but do not persist, archive, log, or include the token, body hash, or any token-derived value in metrics, traces, errors, or idempotency data.
- Before the official call, atomically reserve the request ID with the trusted caller and user identity, method, request target, and player tag. A reserved verification request ID permanently names that non-secret operation, not the submitted token bytes.
- A matching reuse returns the stored sanitized result or an `in_progress` or `verification_unavailable` status without parsing or using a newly supplied token and without calling Supercell again. A caller that wants to submit a different token must use a new request ID.
- A crash after reservation or an ambiguous official result becomes `verification_unavailable`; it is never retried with that token. Reuse with a different non-secret binding is a conflict and changes nothing.
- During beta, a valid verification for a player tag already linked to another account does not move the link. Preserve the current owner and return `support_required` with one bounded opaque candidate identity. Do not return an internal numeric ID.

### Support transfer

- A support transfer is a separate fresh host action. It requires the exact opaque candidate identity, player tag, source and destination account public UUIDs, and a non-secret reason.
- Allow it only through the root-owned `deploy/support-transfer` wrapper. The wrapper must run through a narrow `sudo` rule, derive the operator identity from `SUDO_USER` and `SUDO_UID`, verify a root-owned operator allowlist, and return only a sanitized status.
- The wrapper uses a root-only PostgreSQL service file with the dedicated `clashlens_support_transfer` role. It calls only the transfer function. It does not receive a player token, API role credential, worker role credential, bot role credential, or collector role credential.
- The transfer function locks the candidate and current link, checks the exact candidate binding and expiry, moves the link once, consumes the candidate, and writes one audit row with the operator identity and reason. The same operator and reason may repeat safely. A changed operator or reason is a conflict.
- Grant the support role only schema usage and execute access to the security-definer transfer function. Do not grant it direct table or sequence access. Do not grant the function to application, worker, bot, collector, browser, or public roles.

Use FastAPI for the private HTTP API, Pydantic for request and response validation, psycopg 3 with direct SQL for PostgreSQL access, discord.py for the bot, pytest for tests, and uv for dependency locking. Do not add Django, Celery, Redis, or an SQLAlchemy ORM in Phase 1.

## 3. Deployment and resource limits

- Run the Phase 1 system on one Fedora host with approximately 16 GB of available memory and one private rootless Podman network.
- Keep the whole system memory-efficient. Use bounded queues, batches, connection pools, concurrency, and caches. Do not keep duplicate full datasets in process memory when PostgreSQL or the raw archive already owns them.
- Measure memory use for PostgreSQL, Go, each Python process, and the TypeScript website under realistic collection, replay, analytics, and website load before production activation.
- Set explicit per-process memory limits and preserve headroom for Fedora, rootless Podman, PostgreSQL maintenance, and temporary workload spikes. Exact budgets require measured evidence and remain open until load testing.
- Split a process or move a role to another host only when measured latency, memory pressure, throughput, or failure isolation proves that the single-host model is insufficient.
- Build one versioned Python container image and run the private API, general worker, and Discord bot from it with different commands.
- Apply the shared SQL migrations before starting a new application version. Keep schema changes compatible with the previous Python image for at least one release.
- The Go collector accepts contract versions 1 and 2. The deployment applies migration 2 with bridge-before-migration: it starts a bridge collector that accepts version 1, applies migration 2, then starts the required collector. Do not run an exact-version-1 collector after the contract advances to version 2.
- Roll back application code by starting the previous compatible image. Do not automatically reverse database migrations.

## 4. Structured data and raw evidence

### PostgreSQL and archive ownership

- Use PostgreSQL as the primary structured datastore for Phase 1 operational data and analytics.
- Give each player a database-generated internal numeric identifier for relational references.
- Keep the normalized Clash player tag unique and use it as the player's public identity. Never expose the internal player identifier through the private Python API, website responses, or public pages.
- Use a separate immutable object or blob archive for untouched official API response bodies.
- Give archive credentials only to the Go collector and Python background worker. Go may read archived bytes only to verify an object during its append-only write and deduplication path; it must not read the archive for parsing, replay, or product queries. The Python worker owns archived-evidence reads for processing and replay. The private Python API, TypeScript website, Discord bot, and other integrations use processed PostgreSQL data only.
- Content-address raw response bodies by a cryptographic hash. Store one immutable body for an identical hash while allowing every observation occurrence to reference it.
- Record each completed API response as append-only observation metadata, including endpoint scope, optional player, request and response times, HTTP status, response hash, archive reference, and collector version.
- Never overwrite an earlier observation with a later response. Keep evidence fields immutable and track processing or retry state separately.
- Keep recent observation metadata in partitioned PostgreSQL storage. Older occurrence history may be compacted into the immutable archive when every timestamp, response reference, and provenance link remains reproducible.
- Process an observation only after its raw body and observation metadata are durably recorded.
- Keep poll schedules, the Python job list, deduplicated battles, ranked days, leaderboard snapshots, confidence states, accounts, saved tags, groups, classifications, and precomputed summaries in PostgreSQL.
- Store inferred shielded days as versioned derived states with references to the profile and battle-log observations that support the inference.
- Keep initial analytics in the same relational database rather than introducing a separate data warehouse.
- Protect the relational database with automated backups and point-in-time recovery.
- PostgreSQL extensions may be considered individually when they solve a measured need; confirming PostgreSQL does not pre-approve any extension.
- The raw-archive product remains open.

### Migration 2 boundary

Migration version 2 follows the unchanged collector migration 0001. The deployment applies it with the bridge-before-migration order. Migration 2 implements:

- extension of `python_processing_jobs` as the one Python job list;
- backfill of each existing row as `process_observation`;
- a unique observation handoff so the current and previous compatible Go collector versions continue to create exactly one initial processing job for each observation;
- an observation reference that is optional only for checked non-observation work types;
- a stable deduplication key, type-specific validated input, target versions, full job states, leases, fencing fields, and a separate execution-attempt table;
- no requirement for Go to know Python domain rules.

Migration 2 also lets collector jobs, endpoint results, observations, and transport failures represent a checked `global_player_rankings` endpoint with global scope and no player ID or normalized tag. Player-scoped profile and battle-log rows still require their player identity. Migration 2 preserves the exact request method, path, query, response timing, HTTP status, response hash, archive reference, safe paging-envelope state, collector version, and source-adapter version for the global attempt.

## 5. Python job and replay contract

### Job records and states

- Keep one durable row for each Python job and a separate durable row for each execution attempt. The job records its work type, stable input identity, priority, due time, state, target code and rule versions, safe failure summary, and creation and completion times. An attempt records its owner, opaque lease token, lease times, outcome, and safe failure category.
- Use the states `pending`, `leased`, `waiting_retry`, `complete`, `failed`, and `cancelled`. An expired `leased` job becomes eligible for a new fenced claim. Do not change a terminal job back to runnable without an explicit operator action that creates an audit record.
- Claim only work types and target versions that the running image supports. A previous compatible Python image must leave newer unsupported work unclaimed instead of failing or changing it.
- Check the dependencies required by the selected work type immediately before claim. In particular, observation processing and replay require PostgreSQL and archive-read access. Leave the job pending when a required dependency is unavailable.
- Require the current lease token and a lease expiry later than the PostgreSQL clock for every renewal, product write, retry, failure, cancellation, and completion. Commit all product effects, dependent job requests, and the completed job state in one PostgreSQL transaction.
- Apply the same claim, lease, fencing, retry, cancellation, and completion rules to every Python job type. Use priority plus age and a bounded claim batch so old or lower-priority work cannot remain blocked forever.
- Make each handler safe to run more than once. Use a stable job key to combine duplicate active requests. A retry repeats the same job and versions after a temporary failure; it is not a replay.
- Retry only named temporary failures with bounded backoff and a hard attempt limit. Keep malformed evidence, checksum failures, unsupported source data, invalid work input, and exhausted temporary failures as inspectable terminal failures. Keep safe failure details and never store response bodies or secrets in them.

### Replay

- Replay means reading existing archived evidence again with named parser and domain-rule versions. Replay must not change the collector observation, change the archived body, enqueue collection, or call the official API.
- Allow replay requests only through a root-owned host wrapper executed by an allowlisted authenticated host administrator through `sudo`. The wrapper derives the operator identity from the trusted sudo audit context, requires a reason, and uses a separate PostgreSQL replay-request role and secret that no application container receives.
- The API, worker, bot, and normal service database roles cannot create replay requests. Reject absent or unauthorized operator identity, unsupported target versions, and every replay request through HTTP or Discord without inserting a job.
- Record the operator identity, reason, selected observation or bounded selection, target versions, request time, and replay status.
- Start the prototype with one-observation replay. Make its result safe to repeat. Preserve prior derived versions, write the target version, and atomically request only the ranked-day, snapshot, and analytics rebuilds affected by a changed result. A frozen published result remains unchanged until its replacement publishes atomically.
- Do not start replay automatically during deployment or application startup. Add wider player, time-range, or rule-version replay only through bounded, resumable child jobs after prototype measurements set safe batch and concurrency limits.

## 6. Player registry and collection eligibility

- Seed the known-player registry with the existing approximately 12,370 tags.
- Keep known-player identity and history when a player is no longer eligible for active Legend I collection.
- Regularly poll only players currently confirmed for active Legend I tracking.
- Add every valid new tag discovered through official API observations or user submissions to the known-player registry after normalization and deduplication.
- Use Legend I validation only to decide whether a known player enters active Phase 1 collection.
- Let Python validate active Legend I membership from the official profile `leagueTier` field. Adapter version 1 accepts tier ID `105000036` with the expected name `Legend I`. Deactivate only from a tier ID and name that the accepted adapter table explicitly recognizes as non-Legend-I. An unknown ID, a known ID with an unexpected name, or missing or malformed tier evidence keeps the last confirmed eligibility state and creates a visible source-contract conflict or uncertainty. Do not use the older `league` field for this decision.
- Let Python preserve `currentLeagueSeasonId` and `previousLeagueSeasonId` from accepted profiles and validate them against the confirmed 28-day Monday 05:00 UTC contract in `docs/domain.md`. Go must preserve these fields as raw evidence but must not calculate seasons or ranked days.
- Re-evaluate inactive known tags during the Monday promotion and demotion transition and when they are rediscovered or submitted.

## 7. Poll scheduling and API capacity

### Durable collection work

- Use PostgreSQL-backed durable queues for staggered per-player polling and pending observation processing rather than adding a separate queue product in Phase 1.
- Claim queue work in bounded batches with transactional leases and skip-locked semantics so multiple Go collector or Python worker instances do not claim the same available job concurrently.
- Persist unfinished and retryable collection work so collector restarts do not lose polling intent.
- Under normal operation, use a staggered 5-minute polling cycle and fetch both the player profile and battle log for each actively tracked player.
- Route newly submitted tags and explicit user-requested live refreshes through the Go collector and the same raw-evidence pipeline. The only Python exception is the private API's player-token verification call. The TypeScript website must never receive or use a Supercell API key directly.
- Return the latest saved player representation and its freshness immediately through the TypeScript website backend and private Python API, then expose refresh progress so the website can update after the new observation is processed.
- Do not block the initial player-page response on a live Supercell request.
- Preserve a successful endpoint response when its paired request fails, mark the polling attempt incomplete, and prioritize the missing request for retry.
- Let Python workers claim durably recorded, unprocessed observations and transform them idempotently into canonical domain records.
- Apply bounded retries and backoff without discarding valid evidence or generating duplicate structured events.

### Normal keys and internal safety budget

- Spread requests across an authorized API-key pool and enforce the Phase 1 internal safety budget of at most 30 requests per rolling second for each key. This value is an operator policy inherited from the collector prototype, not a published Supercell limit. Keep it configurable so provider evidence can require a lower value.
- Do not impose an artificial 30-request-per-second limit across the whole system when several authorized keys are healthy.
- Slow, quarantine, or retry an individual key independently when it receives rate-limit or authentication failures.
- Do not log API keys or other credentials.
- On the same 5-minute cycle, use a normal collection key to request one official global player leaderboard response from `GET /v1/locations/global/rankings/players?limit=200`. Give the request reset-baseline priority at 05:00 UTC. Do not use the interactive key for it.
- Archive the untouched response through the normal Go evidence path. The leaderboard observation is global and is not owned by one player tag; the collector schema and work types must represent that scope without a fake player tag.
- Preserve the exact request method, path, query, response timing, HTTP status, response hash, archive reference, safe paging-envelope state, collector version, and source-adapter version for each global attempt.
- Treat the official Top-200 attempt as complete only when one valid response contains exactly 200 entries, exactly 200 unique valid normalized player tags, and official ranks 1 through 200 once each. Treat a short response, malformed entry, duplicate normalized tag, duplicate or missing rank, or unexpected paging requirement as partial or invalid. Do not combine separate attempts or fill missing ranks from profiles.
- Let Python atomically publish a newer complete official Top-200 view and retain the previous complete view when collection or validation fails. Record the failed or partial attempt and expose its status without clearing the last good ranks.
- Assign four API keys to normal collection, providing an internal configured maximum of 120 requests per second while keeping each key at or below the current 30-request safety budget. Do not describe either number as an official allowance.

### Shared interactive key

- Share a fifth API key between Go interactive collection, collector crash recovery, and Python player-token verification. Both runtimes must acquire permits from one PostgreSQL-backed traffic gate before they use this key.
- Give the shared traffic gate three Phase 1 internal, non-borrowing budgets: 28 requests per rolling second for user-driven Go collection, 1 for collector recovery after an expired lease, and 1 for Python player-token verification. It must also enforce the internal combined maximum of 30 requests per rolling second. Run collector recovery in one dedicated worker so crashed-job retries cannot occupy user-interaction workers. Keep the job's original capacity pool for history, but label the retry as recovery work. These are Clash Lens safety budgets, not official API limit claims.
- Identify the shared credential in PostgreSQL by the full SHA-256 fingerprint of the exact ASCII bearer-token bytes, never by a label and never by the secret itself. A secret file may end in one LF or CRLF, which is removed before validation and use. Reject other leading or trailing whitespace, non-ASCII token bytes, and conflicting registrations for the same fingerprint.
- Use PostgreSQL `clock_timestamp()` as the traffic-gate clock. Lock the credential state, count unexpired permit rows in the preceding rolling second for the caller and total budgets, and insert one permit atomically only when all applicable budgets allow it. Return the next eligible database time when denied.
- Reserve the permit immediately before the official request. Do not refund it after a crash, timeout, or ambiguous response. Keep permit rows long enough to enforce the window, then remove them in bounded cleanup batches.
- Keep the shared key's quarantine, cooldown, and recovery state in the traffic gate so a key failure seen by either runtime stops both runtimes from using it. If PostgreSQL or the gate is unavailable, both runtimes must fail closed for this key. The four normal Go keys keep their existing per-key Go limiter and ownership contract.
- An official API-key authentication failure quarantines the shared key until an operator reset or approved key replacement. A provider rate-limit response creates a bounded cooldown. An invalid player token changes neither key state. Do not clear quarantine or recent permits on process restart.
- Before Python receives the shared key, commit sanitized adapter fixtures for a valid token, invalid token, API-key authentication failure, rate limit, malformed response, provider failure, and transport ambiguity. Only the adapter's exact recognized valid or invalid token response may set the verification result; neither changes key health. HTTP 429 creates cooldown. HTTP 401 or 403 quarantines only when its sanitized official reason matches the accepted API-key or fixed-egress authentication fixture. Every other 4xx, 5xx, malformed, unknown, timeout, connection failure, or interrupted response is `verification_unavailable`; it creates no link, does not change key health, and is not retried with the same token. An unknown response must never be guessed as invalid-token or API-key failure.
- Store only a non-secret key identifier and limiter state in PostgreSQL. Keep the API-key secret in the approved process secret files.

### Reset-baseline work

- Migration 2 must replace legacy profile-only reset work with one durable per-player reset-baseline sweep identity that requests both profile and battle-log endpoints after the boundary and links all initial and retried endpoint results to that sweep and boundary. Reset-baseline sweeps and failed-endpoint retries may run at higher priority without changing the normal 5-minute cadence. Legacy profile-only reset attempts cannot prove complete evidence coverage.
- Give reset-baseline collection higher priority than normal polling.
- During the daily no-attack window, collect paired profile and battle-log observations for all actively tracked players. Preserve a successful endpoint while retrying its missing pair under the same sweep identity. Use the start sweep's battle log, normal poll battle logs, and the next boundary's end-sweep battle log to prove the continuous chain defined in `docs/domain.md`.
- Exact worker counts, physical queue column types and indexes beyond the fixed invariants above, lease durations, retry schedules, interactive-refresh coalescing and cooldown rules, and priority weights remain open.

## 8. Frozen and live snapshots

- Record entry-level observation time, age, freshness, and confidence. Use the newest accepted valid trophy observation for each actively tracked player even when no current-window request succeeds. Keep that entry in the ordering and mark it stale in the data contract. The public Live Leaderboard and player page show the observation time as Last updated without technical freshness or confidence panels. Atomic publication must not be treated as proof that every entry is complete or equally fresh.
- Continue serving the previously published frozen leaderboard while constructing its replacement.
- Publish a new leaderboard snapshot and its precomputed summaries atomically so no surface observes a mixture of snapshot versions.
- Order every Live Leaderboard entry by its newest accepted trophy value, highest first, then use the versioned deterministic player-tag hash defined in `docs/domain.md` for equal-trophy ties. Label this position Rank on the public website. An official Top-200 rank remains separate source evidence, is not a public leaderboard column, and must not change this ordering.
- Order the official Top-200 data view by the supplied official rank. Join that rank to a Live Leaderboard entry only from the most recent complete official leaderboard observation. Store that observation identity and time with the joined rank. Never substitute a Live Leaderboard position for a missing official rank.
- Record the snapshot's ordering-rule version. Do not use per-snapshot randomness.
- Target publication at approximately 05:05 UTC on normal days and approximately 05:10 UTC on Mondays.
- Keep the domain day boundary at exactly 05:00 UTC; delayed publication does not move battle attribution into a different ranked day.
- Record corrections as new snapshot versions when later battle and trophy evidence proves an accepted baseline inconsistent.
- Keep live leaderboard calculations separate from frozen snapshots.

## 9. Analytics

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

## 10. Recovery and observability requirements

The implementation must make these conditions observable even though the specific monitoring stack remains open:

- Private service request volume and official API per-key throttling.
- Queue depth, oldest due job, retry volume, and failed work.
- Player-profile and battle-log freshness.
- Incomplete polling attempts and unresolved reconciliation.
- Raw-archive write failures or checksum mismatches.
- Snapshot build progress, publication time, coverage, and correction count.
- Database health, backup success, and recovery readiness.

Recovery-time objectives, recovery-point objectives, backup retention, alert thresholds, and monitoring products remain open decisions.

## 11. Verification requirements

- Test parsers, domain rules, trophy allocation, battle deduplication, ranked-day reconciliation, account permissions, and API response contracts.
- Test worker claims, leases, fencing, idempotency, and atomic completion against real PostgreSQL rather than a SQL mock.
- Test raw-archive reads with synthetic archived responses, including missing objects and checksum mismatches.
- Run an end-to-end test from a stored observation and processing job through canonical product data and a private API response.
- Run realistic memory and load tests for the API, worker, bot, replay, analytics, and database connection pools before production activation.

## 12. Open technology and integration decisions

The following choices remain open:

- Raw object or blob archive product.
- Detailed internal module boundaries and dependency rules.
- Production Go collector libraries and packaging.
- Exact private HTTP/JSON routes, schema details, dependency versions, and dependency update policy.
- Remaining TypeScript website packaging and deployment details.
- Discord commands and response formats.
- Post-beta provider integration and provider-link management.
- Google Sheets integration.
- OBS delivery model.
- Exact image construction, process limits, and memory budgets.
- Cloud or infrastructure provider.
- Monitoring, logging, and alerting products.
- Backup retention and tested recovery procedures.

Present trade-offs and obtain maintainer approval before confirming any of these choices.
