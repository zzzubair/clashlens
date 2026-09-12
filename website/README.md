# Clash Lens website

This directory contains the self-hosted TypeScript SSR website. It talks to
the private Python API through one server-only client boundary.

## Requirements and setup

- Node.js 24 LTS and npm with the committed `package-lock.json`; and
- rootless Podman for the full local stack and browser tests.

```sh
cd website
npm ci
```

Start the product from the repository root:

```sh
./dev up
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
browser bundle. End-to-end tests start the root development stack and exercise
the real Python application against loopback-only Clash, archive, Google, and
Discord fixtures. They never call Google, Discord, Supercell, production data,
or cloud storage.

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
