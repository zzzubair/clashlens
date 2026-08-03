# Clash Lens Agent Guide

## Shared Agent Workflow

Hermes Agent owns the shared agent workflow:

- The maintainer-facing orchestrator uses `openai/gpt-5.6-sol`. It owns decisions, delegation, final review, verification, and the final response.
- Implementation, research, and review subagents use `openai/gpt-5.6-luna` with max reasoning.
- Kanban can coordinate the coder profile. The coder profile also uses Luna with max reasoning.
- Do not use the repository's former OpenCode agents or configuration.

For delegated work, the Sol orchestrator gives the Luna subagent a bounded, self-contained task with context, scope, constraints, acceptance criteria, and verification requirements. The subagent reports its work, verification, limits, and open items. The orchestrator inspects the result and repository state, resumes the same subagent with specific corrections when needed, and independently verifies the final state. It does not finish while required work is active, incomplete, or unverified.

Luna can make reasonable choices inside the assigned bounds but returns open product, domain, architecture, technology, or scope decisions to the Sol orchestrator. The orchestrator handles small or decision-heavy work directly and does not delegate when delegation costs more than the work.

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

`docs/architecture.md` defines one logical product: PostgreSQL for structured data and durable queues, Go for official API collection and raw evidence, Python for domain processing, the API, and analytics, TypeScript for the website, and a separate immutable raw-response archive.

The Go collector must not implement canonical battle linking, ranked-day reconciliation, shield or automatic-defense inference, army classification, or product analytics. Python owns those domain interpretations, and TypeScript consumes the Python API without duplicating their rules.

Remaining storage products, detailed module boundaries, frameworks, hosting models, and cloud providers remain open. Do not confirm one without presenting trade-offs and receiving maintainer approval.

## Working Rules

- Read the applicable source documents before changing product, domain, or architecture behavior.
- Create feature and prototype worktrees as siblings outside the main checkout under `../ClashLens-worktrees/<name>/`. Do not nest worktrees inside the main checkout.
- Preserve maintainer changes and keep each change focused.
- Do not turn open specification details into product rules without maintainer approval.
- Add proportionate tests when implementation changes begin.
- Do not log credentials or unnecessary personal data.
- This repository is private. Use the authenticated `gh` CLI for all GitHub actions.
- Do not commit, push, rebase, or open a pull request unless a maintainer asks.
- State what changed, what was verified, what was not verified, and what remains open.
- Use ASD-STE100 Simplified Technical English in every response to the maintainer. Prefer short sentences, common words, active voice, and consistent terms. Keep code, commands, identifiers, and required technical terms exact.
- Prefer the simplest complete solution. Do not trade away correctness, evidence, or visible uncertainty.

## Feature Workflow

For substantial features:

1. Read the source documents and inspect the working tree.
2. Resolve unclear rules and decisions with the maintainer. Update source documents only when shared meanings change.
3. When asked, create an approved GitHub issue with scope, rules, acceptance criteria, dependencies, and tests.
4. When a prototype is approved, build it in an isolated sibling worktree under `../ClashLens-worktrees/`. Record findings and specification changes on the issue.
5. After approval, plan and implement with migrations, tests, and applicable reviews.
6. Open a PR only when asked. The maintainer decides whether to approve and merge.

The repository is the source of truth. Source documents own shared meanings; the approved issue directs the work; merged code, migrations, and tests are executable. Update source documents in the same PR when meanings change.
