# ADR 0001: Separate collection from domain processing

## Decision

**Accepted for Phase 1.** Use one repository and one logical product with three focused runtime roles:

- Go for official API collection, poll scheduling, rate limiting, retries, raw-response archiving, and append-only observation metadata.
- One Python codebase for domain processing, analytics, accounts, integrations, and a private service API.
- TypeScript for the public website and its backend.

Use PostgreSQL for structured data and durable queues. Use a separate immutable raw-evidence archive for untouched official API response bodies.

The private Python API also makes the separately scoped player-token verification request because that request must not enter the durable evidence queue. The browser communicates only with the TypeScript website backend, which obtains product data through the private Python API.

This decision records the runtime-boundary rationale. The exact domain rules live in [`docs/domain.md`](../domain.md). The full accepted architecture and its open choices live in [`docs/architecture.md`](../architecture.md).

## Rationale

The collection boundary is narrow and operationally distinct. Go is a good fit for long-running collection and rate-limited transport work. Python is a better home for the changing domain and analytics model. TypeScript is the natural website runtime.

This split prevents collection code and website code from duplicating or silently redefining product rules. It accepts three toolchains and more contract-test work in return.

These roles are parts of one product, not independently designed microservices. Add another service boundary only after measured scaling or isolation needs prove it.

## Consequences

- Raw evidence is durable before Python processing begins.
- Go and Python coordinate through PostgreSQL queues and observation metadata, not direct service calls.
- Go does not infer shields or automatic defense, link canonical battles, classify armies, reconcile ranked days, or calculate analytics.
- Python alone creates canonical battles, ranked days, classifications, snapshots, and analytics from durable evidence.
- The private Python API may call only the official player-token verification endpoint with its reserved share of the interactive key. It does not use that path for source collection.
- The website backend, Discord bot, and future integrations use the private Python API and do not read PostgreSQL or the raw archive directly.
- The browser uses only the TypeScript website backend.
- PostgreSQL schema changes use one authoritative migration stream. Cross-runtime contracts are explicit, versioned, and tested.
- Python can replay archived evidence with named parser and domain-rule versions without calling the official API or changing collector evidence.
- CI and local development support all three toolchains.

## Related non-decisions

This ADR does not confirm the raw archive product, detailed internal module boundaries, production Go collector libraries and packaging, private HTTP/JSON routes or schema details, the TypeScript framework, authentication integration, Discord commands, Google Sheets integration, OBS delivery, image construction, process or memory budgets, cloud provider, monitoring products, or backup and recovery policy. Those choices remain open in [`docs/architecture.md`](../architecture.md) until trade-offs are presented and the maintainer approves them.
