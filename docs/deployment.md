# Fedora collector deployment

**Status:** executable runbook for the current Go collector and PostgreSQL deployment. It is not the complete Phase 1 deployment.

This runbook uses direct rootless Podman commands. It does not use Compose. The private Python API, Python worker, Discord bot, and TypeScript website backend are accepted future roles but are not deployed by this script. [Architecture](architecture.md) owns their boundaries, migration order, and open deployment choices.

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

Use separate archive credentials for the Go collector and the future Python worker. Give the current collector only its write and immediate integrity-verification access. [Architecture](architecture.md#postgresql-and-archive-ownership) owns the complete credential boundary.

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

`init` starts PostgreSQL, waits for readiness, and applies the deployment-owned initial migration. The migration is safe to apply again. `up` builds the multi-stage collector image, starts one collector container, and checks that `/readyz` reports `"ready":true`.

Completion: all four commands exit with status `0`, `status` shows the collector and PostgreSQL containers, and `/readyz` reports `"ready":true`.

## 4. Install the user service

Install the tracked rootless user service after the first successful `up`:

```bash
install -D -m 0644 deploy/systemd/clashlens.service \
  ~/.config/systemd/user/clashlens.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens.service
```

The host must enable lingering for the `clashlens` account so this user service starts without an interactive login.

Completion: `systemctl --user status clashlens.service` shows an active service, and the service starts after a host restart without an interactive login.

## 5. Operate the current stack

Use only the supported lifecycle commands:

```bash
./deploy.sh logs collector
./deploy.sh logs postgres
./deploy.sh restart
./deploy.sh down
```

`down` removes the two containers but keeps the network and data volume. It does not delete evidence or database data.

## 6. Run a one-tag live test

Use one known tag only after `up` reports ready. The command submits a durable interactive refresh and prints its job result. It does not print API keys.

```bash
./deploy.sh enqueue --type live_refresh --tag '#PLAYER_TAG'
./deploy.sh logs collector
curl --fail http://127.0.0.1:8081/readyz
```

Check the collector log for the job result and check the external archive for new immutable response objects. Use the HTTPS official origin and the complete configured key pools.

Completion: the job reaches an inspectable terminal result, `/readyz` remains ready, and the archive contains the new immutable response objects without a credential appearing in output.

## 7. Inspect failures

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

## 8. Preserve data during rollback

The deployment does not perform automatic database rollback.
Migrations are forward-only in this first deployment.
A previous collector image can be selected with
`CLASHLENS_COLLECTOR_IMAGE`, then started with `./deploy.sh restart`, only when
that image is compatible with the current schema.

For an incompatible schema change, stop the collector, keep the volume, and
restore a tested PostgreSQL backup before starting the old image.
Immutable archive objects are not removed by `down` and are not rolled back.
The deployment does not configure backups, point-in-time recovery, systemd lingering, or monitoring. Operators must provide and test them before production use.

Do not apply migration 2 or start future Python roles from this runbook. [Architecture](architecture.md#3-deployment-and-resource-limits) owns the required version-1-and-2 bridge order and application rollback contract. [The replay contract](architecture.md#replay) owns replay authorization.

Completion: the PostgreSQL volume and immutable archive remain intact, and the selected collector image is compatible with the current schema before restart.
