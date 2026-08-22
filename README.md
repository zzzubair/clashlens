# Clash Lens

Clash Lens makes competitive Clash of Clans ranked data accessible to all. It
turns official observations into trustworthy tracking and analysis so players
can make evidence-led decisions.

## Repository map

- `cmd/collector` and `internal/collector` — the Go collector and durable
  source-evidence handoff.
- `python/` — domain processing, the private API, and their tests.
- `website/` — the public TypeScript website and browser tests.
- `deploy/` — migrations, service definitions, and deployment scripts.
- [`docs/domain.md`](docs/domain.md) — durable Legend I game and evidence rules.
- [`AGENTS.md`](AGENTS.md) — contribution rules and source authority.

The code, migrations, fixtures, and tests are authoritative for implemented
behavior. Live GitHub issues are authoritative for current scope and status.
The retained documentation records durable contracts; it is not a backlog.

Clash Lens provides data and analysis. Users make the decisions.

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).
