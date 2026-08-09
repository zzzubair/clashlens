# Agent Guide

## Roles

- The maintainer-facing agent uses `openai/gpt-5.6-sol` and owns coordination, decisions, final review, and the final response.
- Delegate heavy implementation, research, or review to `openai/gpt-5.6-luna` or `deepseek/deepseek-v4-flash` through Hermes subagents or the Kanban coder profile.
- Direct maintainer instructions override this split.

## Current state

Clash Lens Phase 1 targets Legend I. Main includes the Go collector, Python application layer, React Router 8 SSR website on Node.js 24, and the Google account experience. PostgreSQL stores structured data; Go collects official API evidence; Python owns domain processing and the private API; TypeScript owns the public website and backend. Detailed specifications, the raw archive, remaining infrastructure, complete deployment, cloud providers, and exact resource budgets remain open.

## Sources

Read only the sources relevant to the task:

- [`docs/product.md`](docs/product.md) for product scope and behavior.
- [`docs/domain.md`](docs/domain.md) for domain rules.
- [`docs/architecture.md`](docs/architecture.md) for runtime boundaries and open technology choices.
- [`docs/deployment.md`](docs/deployment.md), [`docs/collector-prototype.md`](docs/collector-prototype.md), or [`python/README.md`](python/README.md) for their respective implementation areas.

Keep open decisions open. Report conflicts instead of resolving them by assumption.

## Working rules

- Preserve maintainer changes and make the smallest complete change.
- Add or update only the tests needed for changed behavior. Stop when the requested behavior and relevant tests pass.
- Keep feature and prototype worktrees under `../ClashLens-worktrees/<name>/`.
- Use the authenticated `gh` CLI for this private repository.
- Do not commit, push, rebase, or open a pull request unless the maintainer asks.
- Report what changed, what was verified, what was not verified, and what remains open.
