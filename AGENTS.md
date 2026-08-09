# Agent Guide

## Roles

- The maintainer-facing agent uses `openai/gpt-5.6-sol` and owns coordination, decisions, final review, and the final response.
- Delegate heavy implementation, research, or review to `openai/gpt-5.6-luna` or `deepseek/deepseek-v4-flash` through subagents or new threads (with codex)
- Direct maintainer instructions override this split.

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
- Treat every added line, file, table, abstraction, and compatibility path as a cost.
- Prefer reuse, replacement, and deletion. Do not add speculative infrastructure or future-proofing.
- For parsing or other input-to-output work, use one direct module and focused tests. Add persistence, replay, deployment, versioning, or research only when explicitly required.
- After the requested behavior and relevant tests pass, stop. Ignore stale asynchronous results unless they expose a current blocker.
- Keep feature and prototype worktrees under `../ClashLens-worktrees/<name>/`.
- Use the authenticated `gh` CLI for this private repository.
- Do not commit, push, rebase, or open a pull request unless the maintainer asks.
- Report what changed, what was verified, what was not verified, and what remains open.
