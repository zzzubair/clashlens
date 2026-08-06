# Python prototype deployment

**Status:** executable prototype-only deployment runbook.

This runbook starts only the Python prototype stack with direct rootless Podman
commands on a Fedora host. It does not use the Go collector's network, volume,
or container names. It does not call the official Clash of Clans API and it
does not require Supercell credentials.

The source of truth for lifecycle behavior is [`deploy.sh`](deploy.sh). The
prototype-only database contract is
[`src/clashlens_prototype/schema.sql`](src/clashlens_prototype/schema.sql); it
is not a production migration.

## Safety boundary

The script uses the resource prefix `clashlens-python-prototype-`:

- network: `clashlens-python-prototype-network`;
- volume: `clashlens-python-prototype-postgres-data`;
- containers: `clashlens-python-prototype-postgres`,
  `clashlens-python-prototype-api`,
  `clashlens-python-prototype-worker`, and the temporary
  `clashlens-python-prototype-archive-fixture`;
- image: `localhost/clashlens-python-prototype:prototype`.

It verifies rootless Podman, labels resources as prototype-owned, and refuses
to reuse an unowned resource with one of these names. PostgreSQL and the
archive fixture have no host ports. The API publishes only on loopback.
Application containers use read-only root filesystems, bounded `/tmp` tmpfs
mounts, memory and PID limits, dropped Linux capabilities, and
`no-new-privileges`.

Completion check: `podman ps --all` shows only resources with the prototype
prefix, and no collector resource is attached to this network or volume.

## 1. Prepare the host and configuration

Run as `clashlens` or another non-root user with a working rootless Podman
session. Provide:

- Fedora with rootless Podman. The maintained target is Podman 5.8 or later.
- `openssl` for local key generation.
- `curl` for the loopback readiness check.
- A checkout path available to the user systemd service.

Run these commands from `python-prototype`:

```sh
./deploy.sh init
chmod 600 prototype.env
```

`init` creates `prototype.env` from
[`prototype.env.example`](prototype.env.example) when the configured file does
not exist. The configuration path is `DEPLOY_ENV_FILE` when set; otherwise it
is `python-prototype/prototype.env`. The script creates
`CLASHLENS_SECRET_DIR` when set, or `.prototype-runtime/secrets` by default.
Both paths must be private. The script creates parent directories with mode
`700` and configuration and secret files with mode `600`.

`prototype.env` contains non-secret settings only. The deployment rejects
inline `CLASHLENS_DATABASE_URL`, `CLASHLENS_ARCHIVE_ACCESS_KEY`, and
`CLASHLENS_ARCHIVE_SECRET_KEY` values. It mounts the generated files instead.
The generated secret files are:

- `postgres-password`;
- `database-url`;
- `archive-access-key`;
- `archive-secret-key`;
- `typescript-current`; and
- `typescript-previous`.

The HMAC files contain one unpadded Base64URL value that decodes to 32 bytes
and one final LF. The deployment supports only caller
`typescript-website`, key ID `current`, and previous key ID `previous`.

The settings used by the script are:

| Variable | Default or rule |
|---|---|
| `POSTGRES_DB` | `clashlens_prototype`; lower-case PostgreSQL name. |
| `POSTGRES_USER` | `clashlens_prototype`; lower-case PostgreSQL role name. |
| `CLASHLENS_API_PORT` | `18080`; the API is published as `127.0.0.1:${CLASHLENS_API_PORT}:8000`. |
| `CLASHLENS_API_MAX_BODY_BYTES` | `1048576`; maximum `16777216`. |
| `CLASHLENS_ARCHIVE_ENDPOINT` | `archive-fixture:9000`; must be `host:port`. |
| `CLASHLENS_ARCHIVE_BUCKET` | `evidence`. |
| `CLASHLENS_ARCHIVE_SECURE` | `true`; normal startup requires this value. |
| `CLASHLENS_ARCHIVE_MAX_BODY_BYTES` | `2000000`; maximum `67108864`. |
| `CLASHLENS_ARCHIVE_CONNECT_TIMEOUT_SECONDS` | `5`; maximum `60`. |
| `CLASHLENS_ARCHIVE_READ_TIMEOUT_SECONDS` | `15`; maximum `300`. |
| `CLASHLENS_ARCHIVE_MAX_RETRIES` | `1`; maximum `5`. |
| `CLASHLENS_ARCHIVE_RETRY_BACKOFF_SECONDS` | `0.1`; maximum `30`. |
| `CLASHLENS_HMAC_CALLER` | `typescript-website`. |
| `CLASHLENS_HMAC_KEY_ID` | `current`. |
| `CLASHLENS_HMAC_PREVIOUS_KEY_ID` | `previous`. |

The full parser and limits are in `deploy.sh`; the CLI's environment mapping is
in [`src/clashlens_prototype/cli.py`](src/clashlens_prototype/cli.py).

Completion check: the configured file has mode `600`, every generated secret
has mode `600`, the secret directory has mode `700`, and `init` reports that the
prototype database, network, volume, and file-backed secrets are ready.

## 2. Build and start the stack

Run the lifecycle commands in this order when inspecting a new checkout:

```sh
./deploy.sh init
./deploy.sh build
./deploy.sh up
./deploy.sh status
./deploy.sh logs api
./deploy.sh logs worker
./deploy.sh logs postgres
```

- `init` creates or verifies the owned network and volume, starts PostgreSQL,
  waits for `pg_isready`, and applies the prototype schema with
  `ON_ERROR_STOP=on`.
- `build` creates one application image for the API, worker, and temporary
  archive-fixture entrypoint.
- `up` rebuilds the image, refreshes application and HMAC Podman secrets after
  removing their consumers, starts the API and worker, and waits for
  `GET http://127.0.0.1:18080/readyz` to return `{"ready":true}`.
- `status` lists only prototype-prefixed containers and reports the isolated
  network and volume.
- `logs` accepts `api`, `worker`, or `postgres`; the default is `api`.

The API readiness wait covers the API and PostgreSQL contract. The worker also
needs its configured archive endpoint and mounted archive credentials before it
can process a job.

Completion check: `up` exits with status `0`, the readiness check returns
`{"ready":true}`, `status` shows the prototype resources, and the API and
worker containers are running.

## 3. Run synthetic verification

Run:

```sh
./deploy.sh verify
```

The command performs one complete synthetic path:

1. initialize PostgreSQL and apply the prototype schema;
2. build the prototype image and start the API;
3. start a temporary fake archive container from the prototype image;
4. start the worker with the explicit `--archive-insecure-test-only` flag and
   `archive-fixture:9000` endpoint;
5. compute the SHA-256 of `testdata/legend_i_profile_v1.json`;
6. seed one `profile` observation with the computed hash and an
   `s3://evidence/...` reference;
7. poll the signed saved-data API until `#2PP` is available;
8. remove the fake archive, recreate the worker with the normal TLS default,
   and poll the saved-data API again.

The fake archive is an HTTP test fixture. The explicit insecure flag belongs
only to this command; normal worker startup requires the TLS default. On a
failure, the exit cleanup removes the temporary archive and attempts to restore
the normal worker command.

Completion check: the command prints
`synthetic profile verified through the temporary archive and saved-data API`,
returns status `0`, and leaves no archive-fixture container.

## 4. Rotate file-backed secrets safely

The API and worker receive mounted Podman secrets. Recreate consumers before
replacing a secret they use.

- After changing either HMAC key file, run `./deploy.sh up` or
  `./deploy.sh verify`. The script removes the API before replacing the HMAC
  secrets.
- After changing the database URL, archive access key, or archive secret key,
  run `./deploy.sh up` or `./deploy.sh verify`. The script removes both API
  and worker containers before replacing these application secrets.
- `init` preserves running application consumers when it only discovers an
  existing Podman secret. Use `up` or `verify` to refresh changed application
  secrets.

Completion check: the recreated consumer has the new mounted secret, and no
secret replacement occurs while its old consumer is still running.

## 5. Stop and preserve prototype data

Run:

```sh
./deploy.sh down
```

`down` removes the API, worker, fake archive, and PostgreSQL containers. It
keeps the prototype network, PostgreSQL volume, and file-backed secret files.
It does not remove the volume and does not delete database data.

Completion check: all prototype containers are absent, the named volume still
exists, and a later `./deploy.sh up` can reuse the database and secrets.

## 6. Install the user service (optional)

After the checkout is available at `%h/clashlens`, install the tracked unit:

```sh
install -D -m 0644 deploy/systemd/clashlens-python-prototype.service ~/.config/systemd/user/clashlens-python-prototype.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens-python-prototype.service
```

The unit sets `DEPLOY_ENV_FILE=%h/.config/clashlens-python-prototype/prototype.env`,
runs `deploy.sh up` at start, and runs `deploy.sh down` at stop. Enable user
lingering for the `clashlens` account when the unit must start without an
interactive login. The unit does not remove the PostgreSQL volume during stop.

Completion check: the user unit starts the isolated `up` command and the unit
stop leaves the named volume intact.

## Resource and security limits

These values come from `deploy.sh` and the Python archive/API implementation:

| Area | Default | Hard maximum or behavior |
|---|---:|---|
| Private API request body | `1,048,576` bytes | Requests above the limit return `413 request_body_too_large`; configured value cannot exceed `16 MiB`. |
| Archive response body | `2,000,000` bytes | The response is rejected before JSON parsing; configured value cannot exceed `64 MiB`. |
| Archive connect timeout | `5` seconds | Bounded; configured value cannot exceed `60` seconds. |
| Archive read timeout | `15` seconds | Bounded; configured value cannot exceed `300` seconds. |
| Archive reader retries | `1` retry | Client-library retries are disabled; reader-owned retries cannot exceed `5`. |
| Seed processing attempts | `2` | The durable job stops retrying at its configured maximum. |
| HMAC proof lifetime | up to `30` seconds | The proof verifier accepts a lifetime from `1` through `30` seconds, with its clock-skew window. |
| API container memory | `512m` | Read-only root filesystem, `/tmp` tmpfs, 256 PID limit, dropped capabilities. |
| Worker container memory | `768m` | Read-only root filesystem, `/tmp` tmpfs, 256 PID limit, dropped capabilities. |
| PostgreSQL container memory | `512m` | No published host port, 256 PID limit, dropped capabilities. |
| Fake archive memory | `128m` | Used only by `verify`; 64 PID limit and read-only root filesystem. |

The API emits stable JSON error categories and does not return database,
archive, request-target, or secret-file exception details. `GET /livez`
returns `{"live":true}` without proof headers. `GET /readyz` returns `200` only
when the PostgreSQL row matches prototype contract version `2`; otherwise it
returns `503`.

Completion check: configured values stay within the hard maxima, the API is
loopback-only, and no container receives a secret through a normal environment
variable.

## Failure, rollback, and prototype limits

Keep the following rules with the operation that fails:

- The fake archive is allowed only inside `verify`. Normal worker startup uses
  TLS and the configured `host:port` endpoint.
- The prototype schema is guarded. It refuses to alter an existing Clash Lens
  contract with another version. Do not point it at the production collector
  database.
- `down` removes containers but preserves the volume and secret files. It is
  the safe first step when recreating application containers.
- There is no production migration rollback, backup, monitoring, archive
  retention, external TLS termination, official API collection, production
  access control, or account resolver in this slice.
- The candidate signed route is not a production API contract.
- The production collector still uses contract version `1`. A tested bridge to
  the accepted version-2 shared contract is open work; do not treat this
  prototype schema as that bridge.
- The current `ensure_host_secrets` function writes the literal masked password
  `***` into `database-url`, while PostgreSQL starts with the generated
  `postgres-password`. The fake deployment seam asserts this masked value, but
  it does not prove that a real PostgreSQL container can authenticate the API
  and worker. Treat real Podman `up` and `verify` as unverified until this
  source mismatch is resolved.

See the [Python prototype README](README.md) for the full production-gap list
and [architecture.md](../docs/architecture.md) for the accepted production
security boundaries.

Completion check: a failed run leaves the volume and evidence available for
inspection, and the operator can state which prototype-only limitation blocked
the run.
