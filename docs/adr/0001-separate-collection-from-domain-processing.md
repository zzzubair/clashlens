# Separate collection from domain processing

Clash Lens uses Go for official API collection, Python for domain processing, the public API, and analytics, and TypeScript for the website. These are focused runtime roles in one repository and one logical product. Go is responsible for concurrent polling, rate limiting, retries, raw-response archiving, and append-only observation metadata; Python alone turns that evidence into canonical battles, ranked days, classifications, snapshots, and analytics; TypeScript presents data obtained through the Python API.

This split accepts three toolchains because the collection boundary is narrow and operationally distinct, while Python is a better home for the evolving domain and analytics model and TypeScript is the natural website runtime. The boundary prevents collection concerns from duplicating or silently redefining product rules.

## Consequences

- The collector must never infer shields or automatic defense, link canonical battles, classify armies, reconcile ranked days, or calculate product analytics.
- PostgreSQL schema changes use one authoritative migration stream shared by all runtimes.
- Cross-runtime contracts must be explicit, versioned, and tested.
- Raw evidence must be durably recorded before Python processing begins.
- CI and local development must support Go, Python, and TypeScript without turning them into independently designed microservices.
