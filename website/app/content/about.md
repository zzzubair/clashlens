# About Clash Lens

<!--
OWNER INTRO PLACEHOLDER: replace this comment with a short introduction in your
own words, such as who you are and why you built Clash Lens. Comments like this
one are not shown on the page.
-->

## What Clash Lens is

Clash Lens is a Legend League tracker for Clash of Clans. It follows Legend League
players through every Legend day, from one 05:00 UTC Reset to the next, and shows
how they are really doing: their attacks, their defenses, their end-of-day
trophies and where they rank.

Everything on Clash Lens comes from public game data. Clash Lens regularly asks
Supercell's official Clash of Clans API for public player profiles, battle logs
and rankings, keeps an exact copy of every answer, and works out each player's
results from those copies. Anyone can look up a public player without signing in.

Clash Lens is a fan project. It is not made by Supercell.

## Thank you

Clash Lens is built on other people's work. Thank you to everyone behind these
projects and services.

### Game data

- [Clash of Clans API](https://developer.clashofclans.com/) by Supercell, the source of every player profile, battle and ranking on this site
- [clashy.py](https://github.com/ClashKingInc/clashy.py) by ClashKing, the source of the troop, spell and hero list used to read armies

### The website

- [React](https://react.dev/) and [React Router](https://reactrouter.com/)
- [Node.js](https://nodejs.org/), [TypeScript](https://www.typescriptlang.org/) and [Vite](https://vite.dev/)
- [openid-client](https://github.com/panva/openid-client) for Google sign-in, and [isbot](https://github.com/omrilotan/isbot)
- The [Barlow](https://github.com/jpt/barlow) and [Bricolage Grotesque](https://github.com/ateliertriay/bricolage) fonts

### Data processing

- [Python](https://www.python.org/), [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://www.uvicorn.org/) and [Pydantic](https://docs.pydantic.dev/)
- [Psycopg](https://www.psycopg.org/) for the database connection
- [MinIO Python client](https://github.com/minio/minio-py) for long-term storage
- [discord.py](https://github.com/Rapptz/discord.py), [urllib3](https://urllib3.readthedocs.io/) and [certifi](https://github.com/certifi/python-certifi)

### Storage and hosting

- [PostgreSQL](https://www.postgresql.org/), the database, with [WAL-G](https://github.com/wal-g/wal-g) for backups
- [Podman](https://podman.io/) on [Fedora Linux](https://fedoraproject.org/), which run every part of Clash Lens
- [Alpine Linux](https://alpinelinux.org/) and [Tinyproxy](https://tinyproxy.github.io/) for the relay that sends requests to the Clash of Clans API
- [Scaleway](https://www.scaleway.com/) for long-term storage and the relay server
- [Cloudflare R2](https://www.cloudflare.com/developer-platform/products/r2/) for database backup storage
- [Tailscale](https://tailscale.com/) for the private link between the server and the relay
- [Google](https://developers.google.com/identity) and [Discord](https://discord.com/) for sign-in

### Building and testing

- [Playwright](https://playwright.dev/), [axe-core](https://github.com/dequelabs/axe-core), [Vitest](https://vitest.dev/), [ESLint](https://eslint.org/) and [Prettier](https://prettier.io/)
- [pytest](https://pytest.org/), [Ruff](https://docs.astral.sh/ruff/) and [uv](https://docs.astral.sh/uv/)

## Fan Content Policy

This material is unofficial and is not endorsed by Supercell. For more information see Supercell's Fan Content Policy: [www.supercell.com/fan-content-policy](https://www.supercell.com/fan-content-policy).
