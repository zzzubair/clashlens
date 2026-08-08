# Fedora deployment

**Status:** executable runbook for the Go collector, PostgreSQL, and the
production Python worker. Discord bot, Google Sheets exports, the OBS
overlay, and the TypeScript website backend remain future roles. This
runbook does not deploy them.

This runbook uses direct rootless Podman commands. It does not use Compose.
[Architecture](architecture.md) owns runtime boundaries, migration order,
and open deployment choices.

## 1. Prepare the host and credentials

Use:

- Fedora with rootless Podman 5.8 or later.
- An unprivileged service account with a running rootless Podman user session.
- Approximately 16 GB of memory for the complete future Phase 1 system.
- Network access to the official API, the external S3-compatible archive, and image registries.
- A restricted CONNECT proxy when the API keys allowlist a different host's fixed public IP.
- Four normal API-key files and one interactive API-key file outside this repository.
- A PostgreSQL password and archive credentials.

The deployment does not create or print credentials. Keep `app.env` and all credential files outside version control. It imports the database password, database URL, archive credentials, and API keys into rootless Podman file secrets. Container metadata contains only secret names and mounted paths.

Use separate archive credentials for the Go collector and the Python worker. Give the collector only its write and immediate integrity-verification access. Give the worker its own read-only archive credential. [Architecture](architecture.md#postgresql-and-archive-ownership) owns the complete credential boundary.

Completion: the service account can run rootless Podman, every required credential file exists with private permissions, and no credential value is in the repository.

## 2. Configure

```bash
cp app.env.example app.env
chmod 600 app.env
$EDITOR app.env
```

Replace all placeholders, including `CHANGE_ME`, `***`, and example hosts. Keep the official API origin on HTTPS and keep `CLASHLENS_ARCHIVE_SECURE=true`. Set `CLASHLENS_OFFICIAL_API_PROXY_URL` when collection must use the fixed-egress proxy. Set `CLASHLENS_API_KEY_HOST_DIR` to a private host directory. Create the five files named by the two `*_API_KEY_FILES` settings and set mode `600` on each file.

The script creates one named rootless Podman network and one PostgreSQL volume.
The network has outbound access for the official API and external archive.
It is private because no database or archive service port is published.
PostgreSQL has no published port.
The archive has no container or host port.
Only `/readyz` is published on `127.0.0.1:8081` by default.

The repository includes `deploy/egress-proxy/` for the narrow `ser5ver` CONNECT proxy. Its policy accepts only the Fedora Tailscale address and only permits `api.clashofclans.com:443`. The proxy host uses Docker. The Fedora collector host continues to use rootless Podman.

Completion: `app.env` has no `CHANGE_ME` value, every key path resolves to a mode-`600` file, and the archive and official API origins use the required secure settings.

## 3. Initialize, start, and inspect

```bash
./deploy.sh init
./deploy.sh up
./deploy.sh status
curl --fail http://127.0.0.1:8081/readyz
```

`init` starts PostgreSQL, waits for readiness, and applies migration 0001
only. The migration is safe to apply again. `up` builds the multi-stage
collector image, starts the bridge collector against contract version 1,
applies migration 0002 to advance the contract to version 2, and checks that
`/readyz` reports `"ready":true`. The bridge-before-migration order keeps
the collector's version-1 observation-job insert valid while the schema
advances. `status` shows the network, volume, collector, and PostgreSQL
containers.

Completion: all four commands exit with status `0`, `status` shows the
collector and PostgreSQL containers, and `/readyz` reports `"ready":true`.

## 4. Start the production Python worker

After `up` has advanced the contract to version 2, start the production
Python worker:

```bash
./deploy.sh python-up
./deploy.sh python-status
```

`python-up` verifies that the contract is version 2, builds the Python
worker image, starts the worker with read-only root filesystem, dropped
capabilities, memory and PID limits, and file-backed secrets, and waits for
its health command to pass. The worker claims leased Python jobs in bounded
batches and writes product data only inside fenced transactions.

Completion: `python-status` shows the worker running, and the worker health
command exits with status `0` against the deployed contract.

## 5. Install the user services

Install the tracked rootless user services after the first successful `up`
and `python-up`:

```bash
install -D -m 0644 deploy/systemd/clashlens.service \
  ~/.config/systemd/user/clashlens.service
install -D -m 0644 deploy/systemd/clashlens-python-worker.service \
  ~/.config/systemd/user/clashlens-python-worker.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens.service clashlens-python-worker.service
```

The host must enable lingering for the `clashlens` account so these user
services start without an interactive login. The Python worker service
requires the collector stack service and starts after it.

Completion: `systemctl --user status clashlens.service` and
`systemctl --user status clashlens-python-worker.service` show active
services, and both services start after a host restart without an
interactive login.

## 6. Operate the current stack

Use only the supported lifecycle commands:

```bash
./deploy.sh logs collector
./deploy.sh logs postgres
./deploy.sh logs python-worker
./deploy.sh restart
./deploy.sh python-down
./deploy.sh down
```

`down` removes the Python worker, collector, and PostgreSQL containers but
keeps the network and data volume. It does not delete evidence or database
data. `restart` restarts the collector only; it does not rebuild or restart
the Python worker.

## 7. Run a one-tag live test

Use one known tag only after `up` reports ready. The command submits a durable interactive refresh and prints its job result. It does not print API keys.

```bash
./deploy.sh enqueue --type live_refresh --tag '#PLAYER_TAG'
./deploy.sh logs collector
curl --fail http://127.0.0.1:8081/readyz
```

Check the collector log for the job result and check the external archive for new immutable response objects. Use the HTTPS official origin and the complete configured key pools.

Completion: the job reaches an inspectable terminal result, `/readyz` remains ready, and the archive contains the new immutable response objects without a credential appearing in output.

## 8. Inspect failures

Run these commands while the collector container is running. The wrapper enters
that container, and the maintenance command uses the database URL and optional
schema version. Use only the supported maintenance commands:

```bash
./deploy.sh maintenance list-failed --limit 20
./deploy.sh maintenance list-leases --limit 20
./deploy.sh maintenance requeue --job-id 123
./deploy.sh maintenance reset-processing --processing-job-id 456
```

Maintenance output contains safe IDs, states, categories, and times. It does not contain API-key values or raw response bodies. Read [the collector runbook](collector-prototype.md#8-inspect-and-recover-durable-failures) before you requeue or reset work.

Completion: each inspection command returns safe JSON-lines output, and each
recovery command changes only the requested durable job state.

## 9. Preserve data during rollback

The deployment does not perform automatic database rollback.
Migrations are forward-only in this first deployment.
A previous collector image can be selected with
`CLASHLENS_COLLECTOR_IMAGE`, then started with `./deploy.sh restart`, only when
that image is compatible with the current schema.

The production Python worker does not run down-migrations. To roll back
application code, stop the current worker image and start the previous
schema-compatible image with `./deploy.sh python-up` after changing
`CLASHLENS_PYTHON_WORKER_IMAGE`. Keep the schema at version 2; the previous
Python image must be compatible with contract version 2.

For an incompatible schema change, stop the collector and worker, keep the
volume, and restore a tested PostgreSQL backup before starting the old image.
Immutable archive objects are not removed by `down` and are not rolled back.
The deployment does not configure backups, point-in-time recovery, systemd lingering, or monitoring. Operators must provide and test them before production use.

The replay contract is owned by [Architecture](architecture.md#replay).
Replay requests require an allowlisted authenticated host operator through
the root-owned wrapper; no application role can insert replay requests.

## 10. Beta support transfer boundary

Install `deploy/support-transfer` as `root:root` with mode `0700` in a
root-owned non-writable directory. Use a narrow `NOSETENV` sudoers rule with
environment reset that permits only this wrapper for each approved host
operator. The support wrapper is the only audited path that transfers a
verified player link between Clash Lens accounts; no application role
transfers links automatically.

Create `/etc/clashlens/support-transfer-operators` as a `root:root` mode-`0600` allowlist. Create `/etc/clashlens/support-transfer.pg_service.conf` as a `root:root` mode-`0600` PostgreSQL service file. The service file must use the dedicated `clashlens_support_transfer` database role and its credential. Do not give that credential to a container or application role.

The wrapper requires the exact opaque verification candidate UUID, player tag, source account public UUID, destination account public UUID, and a non-secret reason. It derives the operator from the sudo audit context. It prints only a sanitized status. It does not accept a player token.

Completion: the wrapper, allowlist, and service file have the required owner and mode. The support role can execute only the transfer function. Application, worker, bot, collector, and public roles cannot execute that function or update its tables directly.

Completion: the PostgreSQL volume and immutable archive remain intact, and the selected collector image is compatible with the current schema before restart.
