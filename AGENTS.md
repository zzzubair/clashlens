# Clash Lens Agent Guide

Clash Lens is at an early stage. The Phase 1 product scope, architecture shape, PostgreSQL database, and Go/Python/TypeScript runtime split are confirmed. Detailed specifications, remaining technology choices, and implementation remain open.

## Sources of Truth

- `README.md` gives the repository overview and current status.
- `docs/product.md` owns product scope and product rules.
- `docs/domain.md` owns confirmed domain terminology and invariants.
- `docs/architecture.md` owns architecture status and open decisions.

Report conflicts between these files. Do not choose silently.

## Phase 1 Scope

Phase 1 supports Legend I. It includes:

- Broad Legend I army and outcome analysis.
- Separate offense and defense analytics by trophy range or tracked leaderboard cohort and time period.
- Trustworthy personal season tracking.
- Public player pages and official leaderboard context.
- Optional Discord or Google accounts.
- Multi-account summary pages and saved player groups.
- Website, minimal Discord access, Google Sheets exports, and an OBS browser overlay.

Ranked tournaments other than Legend I come later.

Do not add war, Clan War League, clan-management, base-management, or prescriptive army or base recommendations to Phase 1.

## Product Guardrails

- Clash Lens provides data and analysis. Users make the decisions.
- Keep all public player data and analytics free.
- Do not add subscriptions, paywalls, premium tiers, or paid feature gates.
- Use only official Clash of Clans API sources and user-submitted tags for player discovery.
- Do not scrape competitor services for player tags or player data.
- Comply with the current Supercell Fan Content Policy and API terms.
- Do not imply Supercell endorsement.
- Keep shared data meanings and confidence states consistent across all applicable surfaces.

## Trust and Data

- Preserve raw source observations, including timestamps and `armyShareCode`.
- Make ingestion idempotent and deduplicate repeated or two-sided observations of the same battle.
- Keep derived analytics reproducible and versioned.
- Treat exact events, reconciled ranked days, and complete ranked days as separate states.
- Show missing, partial, stale, malformed, unclassified, or uncertain data.
- Do not claim full coverage or complete accuracy without evidence.
- Use UTC internally.
- `docs/domain.md` owns the exact domain rules.

## Architecture

`docs/architecture.md` defines the confirmed Phase 1 architecture: one repository and logical product, PostgreSQL as the primary structured datastore and durable queue, a Go collector that preserves official API evidence, Python domain-processing/API/analytics runtimes, a TypeScript website, a separate immutable raw-response archive, staggered polling, and versioned precomputed analytics.

The Go collector must not implement canonical battle linking, ranked-day reconciliation, shield or automatic-defense inference, army classification, or product analytics. Python owns those domain interpretations, and TypeScript consumes the Python API without duplicating their rules.

Remaining storage products, detailed module boundaries, frameworks, hosting models, and cloud providers remain open. Do not confirm one without presenting trade-offs and receiving maintainer approval.

## Working Rules

- Read the applicable source documents before changing product, domain, or architecture behavior.
- Preserve maintainer changes and keep each change focused.
- Do not turn open specification details into product rules without maintainer approval.
- Add proportionate tests when implementation changes begin.
- Do not log credentials or unnecessary personal data.
- Do not commit, push, rebase, or open a pull request unless a maintainer asks.
- State what changed, what was verified, what was not verified, and what remains open.

## Simplicity

Understand the problem before building. Prefer the simplest design that fully satisfies confirmed functionality, performance, and trust requirements. Do not add abstractions, services, or features for hypothetical needs. Push back when a simpler complete solution exists, but never trade away correctness, evidence, or visible uncertainty.

## Feature Workflow

For substantial features:

1. Read the source documents and inspect the working tree. Do not commit without maintainer approval.
2. Use `/grilling` to resolve rules. Update the existing product, domain, or architecture documents only when their shared meanings change.
3. When requested, create an approved GitHub issue as the feature specification, including scope, rules, acceptance criteria, dependencies, and tests. GitHub actions are outward-facing.
4. Use `/prototype` in an isolated worktree and non-production `prototypes/` path. Record findings and specification changes on the issue before implementation.
5. After maintainer approval, plan and implement with migrations and tests, then run applicable reviews.
6. Open a PR only when asked. Report verification honestly; the maintainer decides whether to approve and merge.

The repository is the source of truth. Existing product/domain/architecture documents own shared meanings; the approved issue directs the change; merged code, migrations, and tests become its executable implementation. Update the existing documents in the same PR whenever implementation changes their meanings.
