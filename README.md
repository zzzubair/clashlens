# Clash Lens

Clash Lens democratizes competitive Clash of Clans ranked data. It gives players trustworthy tracking and analysis so they can make their own evidence-led decisions.

## Phase 1

Phase 1 supports Legend I. It combines:

- Broad Legend I army and outcome analysis.
- Trustworthy personal season tracking.
- Official leaderboard context.
- Public player pages and analytics.
- Optional accounts, multi-account views, and groups.
- Website, Discord, Google Sheets, and OBS access.

Clash Lens provides data. It does not prescribe a specific army or base.

## Status

The Phase 1 product scope and initial architecture are defined: PostgreSQL for structured data, Go for official API collection, one Python codebase for domain processing and a private service API, and TypeScript for the public website and its backend. Frameworks, remaining infrastructure products, and implementation details remain open.

## Documentation

- [Product scope](docs/product.md)
- [Domain rules](docs/domain.md)
- [Architecture](docs/architecture.md)
- [Collector prototype runbook](docs/collector-prototype.md)
- [Fedora deployment runbook](docs/deployment.md)

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).

## Principles

- Clash Lens must be trustworthy.
- All public player data and analytics remain free.
- Missing, partial, stale, or uncertain data must be visible.
- Clash Lens uses official Clash of Clans API sources and does not scrape competitor services for player data.
- Public surfaces must comply with the current Supercell Fan Content Policy and must not imply Supercell endorsement.
