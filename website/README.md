# Clash Lens website

This directory contains the self-hosted TypeScript SSR website. It talks to
the private Python API through one server-only client boundary. The fixture in
`fixture_server.py` is deterministic test infrastructure; it is not the
production API and must not run in production.

## Requirements and setup

- Node.js 24 LTS and npm with the committed `package-lock.json`;
- Python 3.9 or newer for the test fixture; and
- Playwright Chromium for browser tests.

```sh
cd website
npm ci
npx playwright install chromium
```

For local fixture-backed development, run the fixture in one terminal:

```sh
export CLASHLENS_FIXTURE_HMAC_SECRET_B64="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
export CLASHLENS_FIXTURE_HMAC_CALLER="typescript-website"
export CLASHLENS_FIXTURE_HMAC_KEY_ID="2026-08-a"
python3 fixture_server.py --host 127.0.0.1 --port 8010
```

In a second terminal, point the website at that fixture and run the dev
server:

```sh
export CLASHLENS_PYTHON_API_URL="http://127.0.0.1:8010"
export CLASHLENS_PYTHON_HMAC_CALLER="typescript-website"
export CLASHLENS_PYTHON_HMAC_KEY_ID="2026-08-a"
export CLASHLENS_PYTHON_HMAC_SECRET_B64="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
npm run dev
```

Production mounts `CLASHLENS_PYTHON_HMAC_SECRET_FILE` instead of using an
environment secret. It must contain one unpadded base64url value for exactly
32 bytes, with at most one final LF.

## Checks

```sh
npm run build:verify
npm run typecheck
npm run lint
npm run format
npm run test:unit
npm run test:e2e
```

`check:browser-assets` fails if server-only client or secret markers enter the
browser bundle. End-to-end tests use loopback-only deterministic API and OIDC
fixtures and never call Google, Supercell, production data, or the real
Python application.

## Runtime interface

The root deployment starts the website with `website-up` and recovers it with
`website-start`. The website has no database, collector, worker, archive, or
admin secret. It connects to `http://python-api:8000` on the private Podman
network and publishes only the configured host and port.

The application can use Google OpenID Connect when the deployment supplies
the login settings and protected secret files. Local issuer overrides are for
tests only; production requires an exact HTTPS public origin. See
`app.env.example` and [`docs/deployment.md`](../docs/deployment.md) for the
operator configuration.

## Container check

Build the separate website image with:

```sh
podman build --file Containerfile --tag clashlens-website:prototype .
```

The root repository `Containerfile` builds the Go collector; this
`website/Containerfile` builds the Node application.
