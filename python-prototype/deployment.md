# Python prototype deployment

This runbook starts only the Python prototype stack with rootless Podman on the Fedora host.

It does not modify the existing Go collector deployment, use its network, use its volume, or reuse its container names.

It does not call the official Clash of Clans API and it does not require Supercell credentials.

## Requirements

- Fedora with rootless Podman 5.8 or later.
- A rootless Podman user session with enough memory for PostgreSQL and the prototype containers.
- `openssl` for local test key generation.
- `curl` for the local readiness check.
- A checkout path available to the user systemd service.

The deployment must run as the `clashlens` user or another non-root user with rootless Podman configured.

The deployment refuses non-rootless Podman and requires a `host:port` archive endpoint with TLS enabled for normal startup.

## Configuration and local secrets

Run these commands from `python-prototype`:

```sh
./deploy.sh init
chmod 600 prototype.env
```

`init` creates `prototype.env` from `prototype.env.example` when the file does not exist.

`init` creates `.prototype-runtime/secrets/` with mode `700` when `CLASHLENS_SECRET_DIR` is not set.

The directory contains the PostgreSQL password, database URL, archive credentials, and current and previous HMAC keys.

The generated files use mode `600` and are ignored by Git.

Do not put secret values in `prototype.env`.

The API and worker receive mounted file secrets and only non-secret configuration environment values.

The worker requires the mounted archive access and secret key files. It has no fallback archive credentials.

The deployment supports one current and one previous HMAC key for the fixed `typescript-website` caller.

The API accepts either key during rotation, but the deployment probe uses the current key.

## Lifecycle commands

```sh
./deploy.sh init
./deploy.sh build
./deploy.sh up
./deploy.sh status
./deploy.sh logs api
./deploy.sh logs worker
./deploy.sh logs postgres
./deploy.sh down
```

`init` creates the isolated network and PostgreSQL volume, starts PostgreSQL, waits for `pg_isready`, and applies the prototype schema.

`build` creates one versioned application image for the API, worker, and temporary archive fixture commands.

`up` rebuilds the image, starts the API and worker, and waits for `GET http://127.0.0.1:18080/readyz` to report `{"ready":true}`.

The API publishes only on loopback and the PostgreSQL and archive services have no host ports.

`down` removes only the prototype API, worker, archive fixture, and PostgreSQL containers.

`down` keeps the `clashlens-python-prototype-network`, `clashlens-python-prototype-postgres-data` volume, and file-backed secret files.

## Synthetic verification

Run:

```sh
./deploy.sh verify
```

The command starts PostgreSQL and the API if needed, then starts a temporary fake archive container from the prototype image.

It starts the worker with the explicit `--archive-insecure-test-only` flag because the fake archive uses HTTP.

It seeds `testdata/legend_i_profile_v1.json` with its computed SHA-256 digest and an `s3://evidence/...` reference.

It waits for the worker to save the profile and checks the signed player route from inside the API container.

It removes the fake archive, recreates the worker with the normal TLS default, and checks the saved-data route again.

A failed verification removes the temporary archive and attempts to restore the normal worker command.

The synthetic fixture never uses real archive credentials and is not a production archive test.

## Resource and security limits

| Area | Default | Behavior |
|---|---:|---|
| Private API request body | `1,048,576` bytes | Requests above the limit return `413 request_body_too_large`. |
| Archive response body | `2,000,000` bytes | The response is rejected before JSON parsing. |
| Archive connect timeout | `5` seconds | The MinIO client has no unbounded connect wait. |
| Archive read timeout | `15` seconds | Slow archive reads fail with a bounded timeout. |
| Archive reader retries | `1` retry | Client-library retries are disabled; the reader owns the bounded retry loop. |
| Processing attempts | `2` by default in the seed command | The durable job state stops retrying after the configured maximum. |
| HMAC proof lifetime | `30` seconds | The existing proof verifier rejects longer lifetimes. |
| API memory | `512m` container limit | The API uses a read-only root filesystem and a small `/tmp` tmpfs. |
| Worker memory | `768m` container limit | The worker uses a read-only root filesystem and a small `/tmp` tmpfs. |
| PostgreSQL memory | `512m` container limit | PostgreSQL has no published host port. |

The API exposes stable JSON error categories and does not return database, archive, request target, or secret-file exception details.

The liveness endpoint returns `{"live":true}` without proof headers.

The readiness endpoint returns `200` only when the PostgreSQL contract row matches the prototype contract version and returns `503` otherwise.

## User systemd service

Copy the tracked unit to the user unit directory after the checkout is available at `%h/clashlens`:

```sh
install -D -m 0644 deploy/systemd/clashlens-python-prototype.service ~/.config/systemd/user/clashlens-python-prototype.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens-python-prototype.service
```

The unit runs only `deploy.sh up` and `deploy.sh down` for the isolated prototype names.

Enable user lingering for the `clashlens` account when the service must start without an interactive login.

The unit does not delete the PostgreSQL volume during stop.

## Known prototype limits

This deployment is not a production migration path and must not be connected to the production collector database.

It does not provide external TLS termination, backups, monitoring, archive retention, official API collection, account resolution, or production access control.

The fixed HTTP fake archive is allowed only inside `verify`; normal worker startup requires the TLS default.
