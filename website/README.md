# Clash Lens website prototype

This directory contains the first self-hosted TypeScript website slice for issue #30.

The app uses React Router 8 Framework Mode with standard SSR. It has one Node
application process and one server-only Python client boundary. The Python server
in `fixture_server.py` is deterministic and is for tests only. It is not the real
private API and it does not call Supercell or PostgreSQL.

## Requirements

- Node.js 24 LTS
- npm with the committed `package-lock.json`
- Python 3.9 or newer for the fixture server
- Playwright Chromium for browser tests
- Podman for the optional container check

## Install

```sh
cd website
npm ci
npx playwright install chromium
```

## Run the fixture

The fixture and the Node server must use the same 32-byte base64url HMAC value.
For a local test-only key, use the first shared golden-vector key:

```sh
export CLASHLENS_FIXTURE_HMAC_SECRET_B64="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
export CLASHLENS_FIXTURE_HMAC_CALLER="typescript-website"
export CLASHLENS_FIXTURE_HMAC_KEY_ID="2026-08-a"
python3 fixture_server.py --host 127.0.0.1 --port 8010
```

The fixture exposes `/healthz` and private screen-ready routes. It keeps refresh
work in memory and returns one work identity for concurrent refreshes of one tag.
Do not use this fixture in production.

## Run the app in development

In a second terminal, set the private API address and a protected test key:

```sh
export CLASHLENS_PYTHON_API_URL="http://127.0.0.1:8010"
export CLASHLENS_PYTHON_HMAC_CALLER="typescript-website"
export CLASHLENS_PYTHON_HMAC_KEY_ID="2026-08-a"
export CLASHLENS_PYTHON_HMAC_SECRET_B64="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
npm run dev
```

Production uses `CLASHLENS_PYTHON_HMAC_SECRET_FILE` instead of an environment
secret. The file must contain one unpadded base64url value for exactly 32 bytes,
with at most one final LF.

## Build and checks

```sh
npm run build:verify
npm run typecheck
npm run lint
npm run format
npm run test:unit
```

`check:browser-assets` scans emitted browser files and fails if server-only client
or secret markers enter the browser bundle.

## Playwright acceptance tests

The acceptance command builds the app, starts the deterministic Python fixture,
starts the built Node app, and tests the HTTP and browser seam:

```sh
npm run test:e2e
```

The tests do not call the real Python application. The same route client can later
point to the approved private API after issue #29 fixes its account-transfer
contract. The website must not connect to that account integration before the
contract gate is resolved.

## Podman

Build one rootless Node application image. Keep the Python fixture outside the
image and run it separately only for tests.

```sh
podman build --file Containerfile --tag clashlens-website:prototype .
podman run --rm --publish 3000:3000 \
  --env CLASHLENS_PYTHON_API_URL=http://host.containers.internal:8010 \
  --env CLASHLENS_PYTHON_HMAC_CALLER=typescript-website \
  --env CLASHLENS_PYTHON_HMAC_KEY_ID=2026-08-a \
  --env CLASHLENS_PYTHON_HMAC_SECRET_FILE=/run/secrets/clashlens-python-hmac \
  --secret clashlens-python-hmac,type=mount,target=/run/secrets/clashlens-python-hmac \
  clashlens-website:prototype
```

The root repository `Containerfile` remains the Go collector image. This
`website/Containerfile` is the separate website image.
