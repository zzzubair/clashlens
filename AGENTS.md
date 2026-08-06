# Agent Guide

## Agent roles

- The maintainer-facing agent uses `openai/gpt-5.6-sol` and owns decisions, coordination, final review, and the final response.
- Heavy implementation, research, and review work uses `openai/gpt-5.6-luna` with max reasoning through Hermes subagents or the Kanban coder profile.
- A direct maintainer instruction overrides this default role split.
- The repository's former OpenCode agents and configuration are retired.

## Status

Clash Lens is early stage. Phase 1 supports Legend I. The product scope and architecture shape are confirmed: PostgreSQL stores structured data and durable queues; Go collects official API evidence; Python owns domain processing, analytics, accounts, integrations, and the private service API; TypeScript owns the public website and backend; and a separate immutable raw-response archive is part of the shape. The Phase 1 Python stack is confirmed. Phase 1 runs on one Fedora host with about 16 GB available memory and a private rootless Podman network.

Remaining detailed specifications, most implementation, the raw-archive product, detailed module boundaries, the TypeScript framework, remaining infrastructure products, the complete Phase 1 deployment, cloud providers, and exact resource budgets remain open. Present trade-offs and obtain maintainer approval before confirming an open choice.

## Source map

Read the source that matches the branch of work:

- **Product** — [`docs/product.md`](docs/product.md): mission, Phase 1 scope, user and account terms, capabilities, access, surfaces, policy, out-of-scope work, and product specifications.
- **Domain** — [`docs/domain.md`](docs/domain.md): time, players, observations, battles, ranked days, snapshots, cohorts, analytics, and confidence states.
- **Architecture** — [`docs/architecture.md`](docs/architecture.md): runtime ownership, private API security, jobs, storage, collection, deployment, recovery, observability, verification, and open technology choices.
- **Runbooks** — [`docs/collector-prototype.md`](docs/collector-prototype.md) and [`docs/deployment.md`](docs/deployment.md) for the current Go deployment; [`python-prototype/README.md`](python-prototype/README.md) and [`python-prototype/deployment.md`](python-prototype/deployment.md) for the throwaway Python seam.
- **Overview** — [`README.md`](README.md): public orientation and links to the detailed sources.

Report conflicts with exact locations. Keep open decisions open; do not choose silently.

## Fast guards

- Keep Phase 1 on Legend I. Keep other ranked tournaments, war, Clan War League, clan-management, base-management, and prescriptive army or base recommendations out of Phase 1.
- Provide data and analysis so users decide. Keep public player data and analytics free and available without sign-in. Authentication organizes public data and preferences.
- Use official Clash of Clans API sources and user-submitted tags. Comply with the current Supercell Fan Content Policy and API terms. Keep the product unofficial and never imply endorsement.
- Preserve timestamped raw observations, including `armyShareCode`. Keep ingestion idempotent, deduplicate repeated and two-sided observations, and keep derived results reproducible and versioned. Show missing, partial, stale, malformed, unclassified, and uncertain data. Do not claim full coverage or complete accuracy without evidence. Use UTC.
- Go collects and archives evidence. It does not implement canonical battle linking, ranked-day reconciliation, shield or automatic-defense inference, army classification, or analytics. Python owns those rules. The TypeScript backend, Discord bot, and future integrations use the private Python API; browser code uses only the TypeScript backend and does not reproduce Python-owned rules.
- Keep shared meanings, confidence states, and freshness consistent across surfaces. Keep credentials and unnecessary personal data out of logs. Follow the exact domain and verification rules in [`docs/domain.md`](docs/domain.md) and [`docs/architecture.md`](docs/architecture.md).

## Working rules

- Inspect the working tree and read applicable sources before changing behavior. Preserve maintainer changes and keep each change focused.
- Create feature and prototype worktrees as siblings under `../ClashLens-worktrees/<name>/`, outside the main checkout.
- Add proportionate tests when implementation changes begin.
- Use the authenticated `gh` CLI for GitHub actions; this repository is private.
- Use ASD-STE100 Simplified Technical English in maintainer-facing responses. Keep code, commands, identifiers, and required technical terms exact.
- Prefer the simplest complete solution. Preserve correctness, evidence, and visible uncertainty. Never turn an open specification detail into a product rule without maintainer approval.

## Substantial feature workflow

1. **Inspect.** Read source documents and inspect the working tree. Done when applicable rules are identified.
2. **Resolve.** Raise unclear rules or conflicts with the maintainer. Update source documents only when shared meanings change. Done when the rule or open decision is explicit.
3. **Issue.** When asked, create an approved GitHub issue with scope, rules, acceptance criteria, dependencies, and tests. Done when the issue directs the work.
4. **Prototype.** When approved, build in an isolated sibling worktree and record findings and specification changes on the issue. Done when the prototype answers its question and findings are recorded.
5. **Implement.** After approval, plan and implement with migrations, tests, and applicable reviews. Done when requested behavior and tests are in the focused change.
6. **Publish.** Open a pull request only when asked. The maintainer decides whether to approve and merge.

Do not commit, push, rebase, or open a pull request unless a maintainer asks.

## Completion and reporting

Source documents own shared meanings. Approved issues direct scoped work. Merged code, migrations, and tests are executable truth. Update the source document in the same pull request when a shared meaning changes.

Report what changed, what was verified, what was not verified, and what remains open. Report conflicts instead of resolving them by assumption.
