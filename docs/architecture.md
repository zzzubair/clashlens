# Architecture

Clash Lens is one product in one repository with explicit runtime boundaries.
This document describes those durable boundaries; it is not a product roadmap
or a record of current implementation status.

The code, migrations, fixtures, and tests show implemented behavior. Zubair
defines product behavior, and open GitHub issues record agreed requirements.
[`domain.md`](domain.md) defines game meanings and evidence rules. The
deployment runbook defines host operations. Report disagreements between
requirements, documentation, and code rather than silently choosing one.
The [dated product map](product-status.md) links agreed changes to their current
implementation gaps and launch issues.

## Runtime ownership

### Python collector

The single Python asyncio collector owns official API transport: scheduling,
key-rate limiting, retries, and request/response handling. It hashes and writes
each retained response to the bounded local spool, records the observation
metadata and durable processing handoff, and uploads the raw body to the immutable
archive. The [collection and storage rules](collector-polling.md#spool-archive-and-rate-enforcement)
determine which responses are retained. The collector must not interpret battle
meaning, reconcile ranked days, infer shields or automatic defenses, decode
armies, or calculate product analytics; the Python worker owns that interpretation.

### Python application

Python owns interpretation of durable observations and all product-domain
behavior: canonical battles, ranked days, classifications, snapshots,
analytics, accounts, integrations, and the private service API. It also owns
replay of archived evidence with explicit parser and rule versions. Replay
never calls the official API or changes the original observation.

The private API and background workers may run as separate processes from the
same Python codebase. A worker or integration failure must not take the API
offline. Player-token verification runs in the private API and the
maintainer-only Discord recovery command.

### TypeScript website and browser

The TypeScript backend owns browser sessions, provider login, and
presentation-oriented calls to the private Python API. Logout records a SHA-256
fingerprint of that login cookie through the private API, and the backend asks
the API before trusting any login cookie, so a copied cookie stops working once
its login logs out. The API keeps each fingerprint for 25 hours, one hour past
the cookie's own lifetime. Removing a sign-in connection records when it was
removed, also for 25 hours, and the check then refuses every login cookie
issued through that connection at or before that millisecond, on every
browser. A login cookie records when it was issued to the millisecond, taken
after the provider confirms the sign-in, so a fresh login is accepted even in
the same second as the removal; cookies from before that field count from the
start of their second. The backend sends the cookie's issue time with each
check for this, so the website and the API must be deployed together. The browser talks only
to that backend. Neither the browser nor TypeScript may access PostgreSQL or
the raw archive directly, or reimplement Python-owned domain calculations,
confidence rules, rankings, or cohort membership.

Integrations needing Clash Lens data use the private API rather than reading
the database or archive. The agreed Discord setup uses a private incoming
webhook for operator alerts and an existing ticket bot for support. Neither
needs access to the product database or archive. No custom product bot is part
of the current stack. Isolate future integrations only where availability or
measured resource use requires it.

## Durable seams

The collector and worker coordinate through PostgreSQL observation metadata and
durable queues, not through in-process calls. A response is not eligible for
worker processing until its untouched bytes and observation metadata are
durable. The handoff is idempotent: retries and process restarts must not create
duplicate observations or derived records.

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
body by a cryptographic hash while allowing many observation occurrences to
reference it. Recollection after retirement uses a new immutable object location
so an outstanding deletion cannot remove the new copy. The collector and Python
workers share a bounded UID/GID-10001 spool at `sha256/<prefix>/<hash>`. A shared
file lock and publication/cleanup barrier protect its files. Durable sidecars
bridge the file-to-database handoff across crashes. Workers verify local size and
SHA-256 before processing; archive upload runs independently in the background.
A referenced spool body becomes deletable only after processing and upload
succeed. Observation metadata is
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

For live profile and battle-log work, the worker selects each player's newest
waiting job that is due, uses supported versions and has attempts left. It keeps
that player's profile and battle log together, with the profile first, starting
with players whose leaderboard entry is oldest, including its latest profile
confirmation. Players with no observation or confirmation time go first.
Every fourth claim keeps the existing oldest-first order so daily results and
other derived work keep moving; on a response-only thread, described below,
that order covers responses only. If all planned claims are rejected, that call
falls back to the existing order without refreshing the plan again. Every
other job each thread claims takes Reset-priority work first, so on that turn
its planned claim yields to Reset work it could take; on the other turn the
planned claim goes ahead, so live pages keep moving during a Reset backlog;
see [`deployment.md`](deployment.md).

Worker threads share the newest-job plan, but each thread claims its own next
job, including when the queue is idle. An empty plan is refreshed at most once
per second; threads still use the ordinary claim query when it has no candidate.
That query measured about 4 ms when it found nothing, so idle claims stay per
thread.

With `--run-forever` and more than one thread, the worker has no batches and
ignores `--max-jobs`. Each thread keeps claiming until the worker stops; a
thread that finds the queue empty or the spool unreadable waits
`--poll-interval-seconds` and tries again, so one long job never leaves the
other threads idle. Queue maintenance and the Reset publication checks run on
their own timer thread every 10 seconds while the spool is readable, with
their own two database connections, so they neither
wait for a job or a connection nor hold one a thread needs. A failed
round is logged as `worker_maintenance` with only its error type and retried
10 seconds later. Any other worker still claims `--max-jobs` jobs per batch
and runs maintenance between batches.

Those threads are split by kind of work so long jobs cannot hold them all.
About two thirds, 8 of the production worker's 12, claim only responses; they
alone use the newest-job plan. The rest claim derived work: daily results,
builds and army redecodes. Only one derived thread may claim a snapshot,
analytics or army build. It looks for a build first and takes other derived
work only when none is ready, so one build runs at a time and the other
derived threads keep daily results moving. The Reset publication checks and
the correction sweep take a derived thread's turn before they start, and skip
that tick, staying due, when no turn is free. Queue maintenance does not wait
for a turn. This worker checks Reset publications on its timer's first tick
rather than before its threads start.

All threads still share one `--database-pool-size` pool; giving response and
derived threads separate connection limits is deferred. A thread that waits
30 seconds without getting a connection from that pool affects only itself.
If it was claiming, it logs `worker_claim` with `pool_busy`, waits
`--poll-interval-seconds` and claims again. If it was running a job, that job
returns `retrying` with `database_pool_timeout`, as described in
[Failed work](deployment.md#failed-work).

A job holds a lock on its queue row from the start of its work until it
commits. Claims and maintenance skip locked rows, so the
job keeps its claim even if the work outlasts the lease time.

An ordinary check's job is finished as `superseded`, without being
applied, when newer processed evidence from the same player and Legend day
already covers it: a later accepted profile, or a newer battle log while every
row of the older one is already stored and each battle's currently selected
attacker or defender report was confirmed at or after the older log. The last
profile before a Reset, profiles read in the first 30 minutes after a Reset
(they can settle the ended day's end), Reset sweep, Refresh, first lookup,
discovery and replay responses always run. Superseded responses keep their
observation and raw bytes, so they can still be replayed.
