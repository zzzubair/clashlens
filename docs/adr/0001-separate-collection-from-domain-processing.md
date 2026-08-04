# Separate collection from domain processing

Clash Lens uses Go for official API collection, one Python codebase for domain processing, analytics, accounts, integrations, and a private service API, and TypeScript for the public website and its backend. These are focused runtime roles in one repository and one logical product. Go is responsible for collection polling, rate limiting, retries, raw-response archiving, and append-only observation metadata. Python alone turns that evidence into canonical battles, ranked days, classifications, snapshots, and analytics. The private Python API makes the separately scoped player-token verification request because that request must not enter the durable evidence queue. The browser communicates only with the TypeScript website backend, which obtains product data through the private Python API.

This split accepts three toolchains because the collection boundary is narrow and operationally distinct, while Python is a better home for the evolving domain and analytics model and TypeScript is the natural website runtime. The boundary prevents collection concerns from duplicating or silently redefining product rules.

## Consequences

- The collector must never infer shields or automatic defense, link canonical battles, classify armies, reconcile ranked days, or calculate product analytics.
- PostgreSQL schema changes use one authoritative migration stream shared by all runtimes.
- Cross-runtime contracts must be explicit, versioned, and tested.
- Raw evidence must be durably recorded before Python processing begins.
- Go and Python coordinate through PostgreSQL queues and observation metadata, not direct service calls.
- The private Python API may call only the official player-token verification endpoint with its reserved share of the interactive API key. It must not collect profiles, battle logs, or other source evidence.
- The private Python API, one general background worker, and the Discord bot run as separate processes from the same Python codebase.
- The website backend, Discord bot, and future integrations use the private Python API and do not access PostgreSQL or the raw archive directly.
- CI and local development must support Go, Python, and TypeScript without turning them into independently designed microservices.
