# Clash Lens

Clash Lens makes competitive Clash of Clans ranked data accessible to all. It
turns official observations into trustworthy tracking and analysis so players
can make evidence-led decisions.

## Repository map

- `python/` — the single Python asyncio collector, domain processing, the
  private API, workers, and their tests.
- `website/` — the public TypeScript website and browser tests.
- `deploy/` — migrations, service definitions, and deployment scripts.
- [`docs/domain.md`](docs/domain.md) — durable Legend I game and evidence rules.
- [`docs/product-status.md`](docs/product-status.md) — September 25 product decisions,
  implementation gaps and the tracking/website launch order, linked to open issues.
- [`AGENTS.md`](AGENTS.md) — contribution rules and source authority.

The code, migrations, fixtures, and tests are authoritative for implemented
behavior. Live GitHub issues are authoritative for current scope and status.
The domain and operations documents record durable contracts. The dated product
map links those contracts to the live issue backlog; it does not replace it.

## Local development

With rootless Podman installed, start the whole product from a fresh checkout:

```sh
./dev up
```

This starts PostgreSQL 18, the collector, Python API and worker, website, and
loopback-only Clash, archive, Google, and Discord fixtures. It uses 200
synthetic Clashers by default; use `./dev up --players 12500` for the capacity
population supported by the current tool. The October plan starts with about
22,000 supplied tags plus later lists; the trial currently caps at 12,500 and
must be extended and rerun before claiming that capacity. No cloud credentials
are needed, and the ready message reports
the measured startup time.

```sh
./dev status
./dev logs worker
./dev check
./dev down
```

The website is available at <http://127.0.0.1:5173>. `down` keeps the local
database, raw-response archive, and spool; the isolated `check` stack removes
its data when the checks finish. The single Python asyncio collector revisits
player profiles and battle logs on a five-minute cadence, writes exact raw
responses to the bounded local spool, uploads them to the immutable archive,
and hands durable observations to the Python worker. Use
`./dev trial --players 12500 --minutes 30` to measure capacity and storage
growth against the local fixtures; run the matching `./dev down --players 12500`
after a capacity run.

## Fedora operation

Build a pinned local release, then start its rootless system services as a
separate command:

```sh
./ops build --fixture
./ops up --fixture
./ops status
```

Fixture mode is explicit and uses no live credentials. Production uses
`./ops build` followed by `./ops up` with a private `app.env`. `./ops down`
keeps retained data and disables restart after reboot. See
[`docs/deployment.md`](docs/deployment.md) for Fedora setup and production
configuration.

Clash Lens provides data and analysis. Users make the decisions.

## Fan Content Notice

> Clash Lens is unofficial and is not affiliated with or endorsed by Supercell. For more information, see [Supercell's Fan Content Policy](https://supercell.com/en/fan-content-policy/).
