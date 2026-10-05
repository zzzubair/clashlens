# Clash Lens

Clash Lens makes competitive Clash of Clans ranked data accessible to all. It
turns official observations into trustworthy tracking and analysis so players
can make evidence-led decisions.

## Saved Players

When signed in, use "Add to Saved Players" on a player page, then "Remove from
Saved Players" to undo it. These controls need JavaScript and are hidden when
signed out. "View Saved Players" opens your private list at
`/account/saved-players`, where the player-tag box still lets you add players
directly.

The list shows at most 500 players, ordered by tag. A saved player outside that
list still has the correct save/remove state on their profile. If that state
cannot load, the profile stays visible and "Retry saved players" retries it.

## Repository map

- `python/` — the single Python asyncio collector, domain processing, the
  private API, workers, and their tests.
- `website/` — the public TypeScript website and browser tests.
- `deploy/` — migrations, service definitions, and deployment scripts.
- [`docs/domain.md`](docs/domain.md) — durable Legend I game and evidence rules.
- [`docs/ranked-leagues.md`](docs/ranked-leagues.md) — sourced Ranked and Legend League rules, history, capacity and shields.
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
synthetic Clashers by default; use `./dev up --players 12500` for the launch
live-tracking population. The four supplied lists contain 22,157 unique tags
in the total known-player pool, including the live-tracked players. Automatic
weekly eligibility checks are [implemented behind a switch that defaults to
off](docs/collector-polling.md#weekly-eligibility-switch). The known pool is not
a 22,157-player continuous collection load. Verify weekly checks alongside
12,500 live players using the existing tools. No cloud credentials are needed,
and the ready message reports the
measured startup time.

```sh
./dev status
./dev logs worker
./dev check
./dev down
```

The website is available at <http://127.0.0.1:5173>. `down` keeps the local
database, raw-response archive, and spool; the isolated `check` stack removes
its data when the checks finish. The single Python asyncio collector revisits
player profiles every 90 seconds or as fast as the keys allow, and battle logs
when they can have changed. It writes exact raw responses to the bounded local
spool, uploads them to the immutable archive, and hands durable observations to
the Python worker. Use
`./dev trial --players 12500 --minutes 30` to measure capacity and storage
growth against the local fixtures. Trials accept 1–13,500 players and default to
12,500 players for 30 minutes. Profiles include ignored fields and battle logs
include non-Legend entries to resemble the size and processing cost of live
responses. Earlier trials used smaller responses and do not establish live
capacity. Each trial removes its isolated stack and data when it finishes.

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

> This material is unofficial and is not endorsed by Supercell. For more information see Supercell's Fan Content Policy: [www.supercell.com/fan-content-policy](https://www.supercell.com/fan-content-policy).
