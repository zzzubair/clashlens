# Clash Lens website

This directory contains the self-hosted Phase 1 TypeScript website.

The app uses React Router 8 Framework Mode with standard SSR. It has one Node
application process and one server-only Python client boundary. Main contains
the Google account experience: login, account setup and management, saved
players, groups, verified player links, and public user pages. The Python server
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

## Configure Google login

Google login is optional. Public pages continue to work when
`CLASHLENS_LOGIN_ENABLED=false`.

When production login is enabled, configure:

- `CLASHLENS_LOGIN_ENABLED=true`.
- `CLASHLENS_PUBLIC_ORIGIN`: the exact HTTPS origin, with no path, query, or fragment.
- `CLASHLENS_GOOGLE_CLIENT_ID`: the Google OpenID Connect client ID.
- `CLASHLENS_GOOGLE_CLIENT_SECRET_FILE`: a protected file that contains the client secret.
- `CLASHLENS_LOGIN_SECRET_FILE`: a protected file that contains one unpadded base64url value for exactly 32 bytes, with at most one final LF.

Register this exact Google redirect URI:

```text
<CLASHLENS_PUBLIC_ORIGIN>/auth/google/callback
```

The application requests only the `openid` scope and retains only Google’s stable
private subject. It does not request or store email or profile claims. Production
rejects HTTP origins, environment-based secrets, and
`CLASHLENS_GOOGLE_ISSUER_URL`. The issuer override is for local tests only; local
HTTP overrides must use `localhost`, `127.0.0.1`, or `::1`.

The signed `HttpOnly`, `Secure`, `SameSite=Lax` login cookie contains only the
Google subject, issue time, and expiry. It expires 24 hours after login and does
not slide when the user loads a page or submits a mutation. The server validates
enabled production login configuration before it listens.

Keep `CLASHLENS_LOGIN_ENABLED=false` in production until the Python service
enforces the accepted strict inappropriate-name filter for usernames, display
names, and group names, and the root deployment passes the login configuration.
The browser and TypeScript checks are early feedback; they are not the
authoritative release gate.

The root deployment cannot enable login yet. `deploy.sh` and `app.env.example`
do not pass `CLASHLENS_LOGIN_ENABLED`, `CLASHLENS_PUBLIC_ORIGIN`,
`CLASHLENS_GOOGLE_CLIENT_ID`, `CLASHLENS_GOOGLE_CLIENT_SECRET_FILE`, or
`CLASHLENS_LOGIN_SECRET_FILE`. Do not work around this by injecting secrets
into a running website container.

## Account routes

- `/login`, `/auth/google`, and `/auth/google/callback` own Google sign-in.
- `/account/setup` collects the required username and display name on first login.
- `/account` shows the account summary and verified player links.
- `/account/profile` updates the username and display name.
- `/account/saved-players` manages saved public player tags.
- `/account/verify-player` submits a one-time player verification token.
- `/account/groups` manages private named groups.
- `/users/:username` is the public user page and shows only the display name,
  username, and verified player links.
- `/logout` accepts same-origin `POST` only.

All account mutations include a fresh UUID idempotency key and work without
browser JavaScript. Setup and profile forms keep invalid non-secret values visible
for correction. The one-time player verification token is always cleared and is
never returned.

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

The acceptance command builds the app and starts three loopback-only processes:
the deterministic Python API fixture on port 8010, the deterministic OIDC provider
on port 8011, and the built Node app on port 5173. It then tests the complete HTTP
and browser seam:

```sh
npm run test:e2e
```

The acceptance tests do not call Google, Supercell, production data, or the real
Python application. The OIDC provider uses an ephemeral RSA key and one stable
test subject. The API fixture keeps account, saved-player, verification, and group
state in memory. Test values are defined in `tests/fixtures/test-values.ts`; they
are not real credentials. Both fixtures expose loopback-only reset endpoints and
must never run in production.

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
