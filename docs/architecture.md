# Architecture

## Status

No implementation architecture is confirmed.

The product and domain documents define what Clash Lens must do. Architecture work starts later and must present trade-offs before it commits the project to a technology or service boundary.

## Product Constraints

Any architecture must support:

- Broad official API collection for Legend I players.
- Timestamped raw battle observations and raw `armyShareCode` preservation.
- Idempotent ingestion and battle deduplication.
- Reproducible and versioned army classification and analytics.
- Explicit data coverage, freshness, and confidence states.
- Public player pages, meta analytics, and official leaderboard context.
- Optional Discord and Google authentication, multi-account views, and groups.
- Website, Discord, Google Sheets, and OBS surfaces with consistent data meanings.
- Free public access to player data and analytics.

## Open Decisions

The following choices remain open:

- Ingestion language and runtime.
- Analytics language and runtime.
- Database and storage model.
- Service boundaries and integration model.
- Web stack.
- Discord stack.
- Authentication and account model.
- Google Sheets integration.
- OBS delivery model.
- Deployment model.
- Hosting and cloud provider.
- Monitoring, backups, and recovery.

Record hard-to-reverse architecture decisions only after the maintainers review the trade-offs.
