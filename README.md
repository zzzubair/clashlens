# Clash Lens

Clash Lens democratizes competitive Clash of Clans ranked data. It brings official observations together into trustworthy tracking and analysis so players can make evidence-led decisions.

## Phase 1: Legend I

Phase 1 is global and English first. It covers:

- Broad Legend I army and outcome analysis, with separate offense and defense views.
- Trophy-range, tracked-leaderboard, rank-band, and rank-streak analysis over a stated period.
- Trustworthy personal season tracking with visible evidence and coverage gaps.
- Public player pages, public analytics, and official leaderboard context.
- Optional Discord or Google accounts, multi-account summaries, and saved player groups.
- A website, minimal Discord access, Google Sheets exports, and an OBS browser overlay.

Clash Lens provides data and analysis. Users make the decisions. Phase 1 does not prescribe a specific army or base.

## Status

The Phase 1 product scope and accepted architecture shape are confirmed. PostgreSQL stores structured data. Go collects official API evidence. One Python codebase owns domain processing and the private service API. TypeScript owns the public website and its backend. The Phase 1 Python stack and the React Router 8 SSR website on Node.js 24 are confirmed.

Remaining detailed specifications, most implementation, the raw-archive product, other infrastructure products, the complete Phase 1 deployment, and cloud providers remain open. See [Product scope](docs/product.md) and [Architecture](docs/architecture.md) for the authoritative lists.

## Documentation

Read the document that matches the question:

- [Agent guide](AGENTS.md) — working rules, source pointers, guardrails, and feature workflow.
- [Product scope](docs/product.md) — mission, Phase 1 capabilities, user terms, product rules, out-of-scope work, and open specifications.
- [Domain rules](docs/domain.md) — exact time, event, battle, ranked-day, snapshot, cohort, analytics, and confidence rules.
- [Architecture](docs/architecture.md) — accepted runtime boundaries, security, storage, jobs, deployment, recovery, verification, and open technology choices.
- [Runtime-boundary ADR](docs/adr/0001-separate-collection-from-domain-processing.md) — rationale for separate collection and domain processing.
- [Collector prototype runbook](docs/collector-prototype.md) — current collector behavior and prototype path.
- [Fedora deployment runbook](docs/deployment.md) — deployment procedure and host assumptions.
- [Python application](python/README.md) — the Phase 1 Python application layer: test commands and package layout.

## Product principles

Trust, visible uncertainty, free public access, official data sources, and Supercell policy compliance are product rules. Read [Product scope](docs/product.md) before making a product or user-facing change.

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).
