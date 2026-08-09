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

Main contains the merged Phase 1 baseline. The Go collector implements contract version 2 with bridge-before-migration. The production Python worker and the private API run from the Python package. The public website runs on React Router 8 with server-side rendering on Node.js 24. Main also contains the Google account code: login, account setup and management, saved players, groups, verified player links, and public user pages. PostgreSQL stores structured data. The raw-evidence archive keeps untouched official response bodies.

Login stays disabled on the root deployment. These blockers remain open:

- The Python service does not yet enforce the strict inappropriate-name filter for usernames, display names, and group names.
- The root deployment does not yet pass the login configuration: the enable flag, the public origin, the Google client ID, the client-secret file, and the login-secret file.
- Battle-log issue [#35](https://github.com/zzzubair/ClashLens/issues/35) is not complete: the player API does not expose complete per-battle offense and defense logs.
- The public analytics route does not exist yet.
- Performance issue [#38](https://github.com/zzzubair/ClashLens/issues/38) is open: collector and Python hot-path amplification.
- A clean merged-main release candidate has not passed the complete live release gate.

Official Top-200 activation and its public view, the raw-archive product, the Discord bot, Google Sheets exports, the OBS overlay, hosted ingress, backups, monitoring, and cloud providers remain open. See [Product scope](docs/product.md) and [Architecture](docs/architecture.md) for the authoritative lists.

## Documentation

Read the document that matches the question:

- [Agent guide](AGENTS.md) — working rules, source pointers, guardrails, and feature workflow.
- [Product scope](docs/product.md) — mission, Phase 1 capabilities, user terms, product rules, out-of-scope work, and open specifications.
- [Domain rules](docs/domain.md) — exact time, event, battle, ranked-day, snapshot, cohort, analytics, and confidence rules.
- [Architecture](docs/architecture.md) — accepted runtime boundaries, security, storage, jobs, deployment, recovery, verification, and open technology choices.
- [Runtime-boundary ADR](docs/adr/0001-separate-collection-from-domain-processing.md) — rationale for separate collection and domain processing.
- [Collector prototype runbook](docs/collector-prototype.md) — historical evidence for the closed Issue #2 prototype; not current operator guidance.
- [Fedora deployment runbook](docs/deployment.md) — current operator path for the merged main deployment: deployment procedure and host assumptions.
- [Python application](python/README.md) — the production functional-beta Python layer: test commands and package layout.

## Product principles

Trust, visible uncertainty, free public access, official data sources, and Supercell policy compliance are product rules. Read [Product scope](docs/product.md) before making a product or user-facing change.

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).
