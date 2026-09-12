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

## Local development

With rootless Podman installed, start the whole product from a fresh checkout:

```sh
./dev up
```

This starts PostgreSQL 18, the collector, Python API and worker, website, and
loopback-only Clash, archive, Google, and Discord fixtures. It uses 200
synthetic Clashers by default; use `./dev up --players 12500` for the capacity
population. No cloud credentials are needed, and the ready message reports
the measured startup time.

```sh
./dev status
./dev logs worker
./dev check
./dev down
```

The website is available at <http://127.0.0.1:5173>. `down` keeps the local
database, raw-response archive, and spool; the isolated `check` stack removes
its data when the checks finish. The local collector repeats once per hour:
that is at most 401 official fixture requests per hour with 200 Clashers, or
25,001 with the optional capacity population. Using Issue #82's production
projection as a conservative ceiling, persistent data can grow by about 2 MB
per day at 200 Clashers or 90 MB per day at 12,500 (about 16 GB over six
months); the database and archive volumes are not size-capped. Run `down` when
you are finished to stop local data growth; use the matching
`./dev down --players 12500` after a capacity run.

Clash Lens provides data and analysis. Users make the decisions.

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).
