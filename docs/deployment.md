# Fedora deployment

This runbook deploys the Go collector and PostgreSQL with rootless Podman.
It uses direct Podman commands. It does not use Compose.

## Requirements

- Fedora with rootless Podman 5.8 or later.
- A running rootless Podman user session.
- Network access to the official API, the external S3-compatible archive, and the image registries.
- A restricted CONNECT proxy when the API keys allowlist a different host's fixed public IP.
- Five API-key files outside this repository: four normal keys and one interactive key.
- A PostgreSQL password and archive credentials.

The deployment does not create or print credentials.
Keep `app.env` and the API-key files outside version control.
The deployment imports the database password, database URL, archive HMAC pair,
and API keys into rootless Podman file secrets. Container metadata contains
only secret names and mounted file paths, not those credential values.

## Configure

```bash
cp app.env.example app.env
chmod 600 app.env
$EDITOR app.env
```

Replace all `CHANGE_ME` values.
Keep the official API origin on HTTPS.
Keep `CLASHLENS_ARCHIVE_SECURE=true`.
Set `CLASHLENS_OFFICIAL_API_PROXY_URL` when collection must use the fixed-egress proxy.
Set `CLASHLENS_API_KEY_HOST_DIR` to a private host directory.
Create the five files named by the two `*_API_KEY_FILES` settings.
Use mode `600` for each file.

The script creates one named rootless Podman network and one PostgreSQL volume.
The network has outbound access for the official API and external archive.
It is private because no database or archive service port is published.
PostgreSQL has no published port.
The archive has no container or host port.
Only `/readyz` is published on `127.0.0.1:8081` by default.

The repository includes `deploy/egress-proxy/` for the narrow `ser5ver`
CONNECT proxy. Its policy accepts only the Fedora Tailscale address and only
permits `api.clashofclans.com:443`.
The `ser5ver` proxy host intentionally uses Docker. Docker is required on that
host; the Fedora collector host continues to use rootless Podman.

## Start and inspect

```bash
./deploy.sh init
./deploy.sh up
./deploy.sh status
curl --fail http://127.0.0.1:8081/readyz
```

`init` starts PostgreSQL, waits for readiness, and applies the deployment-owned
initial migration. The migration is safe to apply again.
`up` builds the multi-stage collector image, starts one collector container, and
checks that `/readyz` reports `"ready":true`.

Install the tracked rootless user service after the first successful `up`:

```bash
install -D -m 0644 deploy/systemd/clashlens.service \
  ~/.config/systemd/user/clashlens.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens.service
```

The host must enable lingering for the `clashlens` account so this user service
starts without an interactive login.

Useful commands:

```bash
./deploy.sh logs collector
./deploy.sh logs postgres
./deploy.sh restart
./deploy.sh down
```

`down` removes the two containers but keeps the network and data volume.
It does not delete evidence or database data.

## Manual one-tag live test

Use one known tag only.
Do this after `up` reports ready.
The command submits a durable interactive refresh and prints its job result.
It does not print API keys.

```bash
./deploy.sh enqueue --type live_refresh --tag '#PLAYER_TAG'
./deploy.sh logs collector
curl --fail http://127.0.0.1:8081/readyz
```

Check the collector log for the job result and check the external archive for
new immutable response objects.
Do not use an HTTP test origin.
Do not use reduced API-key pools for this live test.

For maintenance output, use only the supported collector commands:

```bash
./deploy.sh maintenance list-failed --limit 20
./deploy.sh maintenance list-leases --limit 20
```

## Rollback limits

The deployment does not perform automatic database rollback.
Migrations are forward-only in this first deployment.
A previous collector image can be selected with
`CLASHLENS_COLLECTOR_IMAGE`, then started with `./deploy.sh restart`, only when
that image is compatible with the current schema.

For an incompatible schema change, stop the collector, keep the volume, and
restore a tested PostgreSQL backup before starting the old image.
Immutable archive objects are not removed by `down` and are not rolled back.
The deployment does not configure backups, point-in-time recovery, systemd
linger, or monitoring. Those remain operator responsibilities.
