# Fedora deployment

This is the operator runbook for the rootless Podman deployment: the Go
collector, PostgreSQL, the private Python API and workers, and the website on
one Fedora host. It describes commands that exist in `deploy.sh`; it is not a
product roadmap. Current release work is tracked in
[Issue 31](https://github.com/zzzubair/ClashLens/issues/31).

The deployment uses direct rootless Podman commands, not Compose. Runtime
boundaries and data ownership are documented in
[architecture.md](architecture.md). Stable game and evidence rules belong in
[domain.md](domain.md).

## Prerequisites and secrets

Use an unprivileged service account with a running rootless Podman session,
Fedora with Podman 5.8 or newer, network access to the official API, the
external S3-compatible archive, and the image registry, and enough memory for
the selected resource budgets. A fixed-egress CONNECT proxy is required when
the official API key allowlist uses a different public IP.

Keep `app.env` and all credential files outside version control with mode
`600`. The deployment imports credentials into Podman file secrets and does
not print their values. Required inputs include:

- the admin PostgreSQL password and the collector, worker, and API role
  passwords;
- normal and interactive official API-key files;
- separate collector-write and worker-read archive credentials;
- the current HMAC secret, plus an optional previous HMAC secret during
  rotation; and
- for browser login: the browser session key file, the Google client-secret
  file, and the Discord client-secret file, plus the non-secret Google and
  Discord client IDs and the exact public https origin. The website starts
  with login disabled when none of these are configured, and a partial login
  configuration is rejected before any container change.

`POSTGRES_USER` is the admin role used for migrations and role configuration.
Role passwords must be 32–128 URL-safe characters (`A-Z`, `a-z`, `0-9`, `_`,
or `-`). The collector receives only its API keys and archive-write
credentials. Workers receive only the archive-read credential. The private
API receives its database role, HMAC files, one interactive key, and proxy
settings. The website receives only the private API HMAC secret.

## Configure

```bash
cp app.env.example app.env
chmod 600 app.env
$EDITOR app.env
```

Replace every `CHANGE_ME` value and example host. Keep the official API origin
on HTTPS, set `CLASHLENS_ARCHIVE_SECURE=true`, configure the fixed-egress
`CLASHLENS_OFFICIAL_API_PROXY_URL`, and point `CLASHLENS_API_KEY_HOST_DIR` at
a private directory containing the files named by the API-key settings.
Choose explicit resource budgets for PostgreSQL, collector, API, workers, and
website; the deployment rejects placeholders and invalid bounds before it
changes runtime state.

The 8-core/16-thread Fedora host profile is PostgreSQL `8g` memory, `1g`
shared memory, and 8 CPUs. Smaller hosts must choose smaller measured values.
Worker replicas are 1–16; each replica supports concurrency and database and
archive pool sizes from 1–64. The collector database pool is also 1–64. The
worker settings apply per replica and are not forwarded to the collector.
The relevant settings are `CLASHLENS_WORKER_REPLICAS`,
`CLASHLENS_WORKER_CONCURRENCY`, `CLASHLENS_WORKER_DATABASE_POOL_SIZE`,
`CLASHLENS_WORKER_ARCHIVE_POOL_SIZE`, and
`CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE`. A worker replica claims work from
the shared queue; increasing the replica count multiplies its per-container
resource budget.

### Fixed-egress proxy

`deploy/egress-proxy/` deploys the narrow CONNECT proxy on the fixed-egress
host with Docker. Set `PROXY_LISTEN_IP` to its private Tailscale address and
`PROXY_CLIENT_IP` to the Fedora host's Tailscale address, then run its
`deploy.sh up` command. The generated Tinyproxy policy accepts only that
client and permits only `api.clashofclans.com:443`.

The proxy uses host networking so it sees the real client address. Before it
starts, configure the proxy-host firewall to allow its port only from the
configured Fedora Tailscale address and to deny that port on every other
source address and interface. Point `CLASHLENS_OFFICIAL_API_PROXY_URL` at the
private listener; never expose it as an open proxy.

PostgreSQL containers use the `step6-v1` metrics profile, preload
`pg_stat_statements`, and enable statement, I/O, and WAL I/O timing. Migration
0003 installs the extension in the Clash Lens database.

## Lifecycle and migrations

The collector contract version is separate from the schema migration number.
The production contract is version 2. The current forward-migration set is
0001 through 0007, with 0006 permitting both Phase 1 login providers and 0007
adding player discovery and the durable Global Top-200 cycle guard. `up` applies
only missing forward migrations recorded in `clash_lens_schema_migrations`; it
never replays an applied migration. An unknown contract version is rejected
without side effects.

```bash
./deploy.sh init
./deploy.sh up
./deploy.sh status
curl --fail http://127.0.0.1:8081/readyz
```

- `init` starts PostgreSQL and applies migration 0001 only to an absent
  database. It refuses an initialized database.
- `up` builds the collector image, advances the database through all missing
  migrations (0001–0007 on a fresh database), configures runtime role
  passwords, and stages the required collector with Global Top-200 disabled.
  A contract-v1 upgrade uses the bridge collector while migrations 0002–0007
  are applied, then replaces it with the disabled required collector.
- `build-collector`, `build-python`, and `build-website` build images only.
- `restart` is the start-only recovery path for a contract-v2 stack. It does
  not build or run SQL and always stages Global Top-200 disabled.
- `status` shows the network, volume, containers, health, and worker queue
  status without loading unrelated secrets.

After `up` has completed all pending migrations, start the Python layer and
website:

```bash
./deploy.sh python-up
./deploy.sh status
./deploy.sh queue-status
./deploy.sh website-up
curl --fail http://127.0.0.1:3000/healthz
```

`python-up` requires contract version 2, builds the Python image, and starts
the private API and `CLASHLENS_WORKER_REPLICAS` identical worker containers.
After the compatible workers report healthy, it recreates the collector with
Global Top-200 enabled. `python-start` follows the same order without building;
`worker-start` also enables rankings only after worker health. A start-only
worker rollback without a local collector image reports that enablement was
skipped instead of claiming rankings are enabled. `api-start`
does not change collector enablement. The collector restart policy preserves
its last deployment-owned state; systemd recovery runs `restart` (disabled)
before `worker-start` recreates it enabled after worker health. The setting is
deployment-owned and is rejected in `app.env`. `website-up` requires a healthy private API;
`website-start` is its start-only recovery path. The website connects to
`http://python-api:8000` on the private network and publishes only the
configured ingress address.

When the PostgreSQL shared-memory or metrics-profile label on an existing
container does not match `app.env`, `up` stops before migration and instructs
the operator to run `stack-down`, then `up`. The named database volume is
preserved.

## User services

Install the tracked rootless user services after a successful `up` and
`python-up`:

```bash
install -D -m 0644 deploy/systemd/clashlens.service \
  ~/.config/systemd/user/clashlens.service
install -D -m 0644 deploy/systemd/clashlens-python-api.service \
  ~/.config/systemd/user/clashlens-python-api.service
install -D -m 0644 deploy/systemd/clashlens-python-worker.service \
  ~/.config/systemd/user/clashlens-python-worker.service
install -D -m 0644 deploy/systemd/clashlens-website.service \
  ~/.config/systemd/user/clashlens-website.service
systemctl --user daemon-reload
systemctl --user enable --now clashlens.service \
  clashlens-python-api.service clashlens-python-worker.service clashlens-website.service
```

Enable lingering for the service account. Units use only start-only commands
at boot: `restart`, `api-start`, `worker-start`, and `website-start`. They do
not build images or run SQL.

## Install support recovery

Install the recovery wrapper as root, but configure it to enter the existing
rootless Podman context owned by the deployment service account. Replace the
example account and checkout path below; `DEPLOY_SCRIPT` must name the same
`deploy.sh` used for the running stack, so its `app.env` supplies any Python
API container-name override and Podman resolves in the service account's fixed
system path.

```bash
sudo install -o root -g root -m 0700 deploy/support-recovery \
  /usr/local/sbin/clashlens-support-recovery
sudo install -d -o root -g root -m 0755 /etc/clashlens
printf '%s\n' maintainer1 | sudo tee /etc/clashlens/support-recovery-operators >/dev/null
sudo chmod 0600 /etc/clashlens/support-recovery-operators
sudo tee /etc/clashlens/support-recovery.conf >/dev/null <<'EOF'
SERVICE_ACCOUNT=clashlens
DEPLOY_SCRIPT=/srv/clashlens/deploy.sh
EOF
sudo chmod 0600 /etc/clashlens/support-recovery.conf
printf '%s\n' '%clashlens-support ALL=(root) /usr/local/sbin/clashlens-support-recovery *' \
  | sudo tee /etc/sudoers.d/clashlens-support-recovery >/dev/null
sudo chmod 0440 /etc/sudoers.d/clashlens-support-recovery
sudo visudo -cf /etc/sudoers.d/clashlens-support-recovery
```

The allowlist contains one login name per line. Add those operators to the
`clashlens-support` host group used by the sudo rule. The configured service
account needs lingering enabled and its rootless runtime at
`/run/user/<uid>` available. With the private API healthy, an allowlisted
operator runs:

```bash
sudo /usr/local/sbin/clashlens-support-recovery \
  --target-account-public-id ACCOUNT_UUID \
  --player-tag '#PLAYER_TAG' \
  --discord-user-id DISCORD_USER_ID \
  --reason 'support ticket and proof summary'
```

Enter the current in-game API token only at the private prompt. The wrapper
passes it over stdin to the existing API container and prints only a bounded
`support_recovery_status` value.

## Operate and inspect

```bash
./deploy.sh logs collector
./deploy.sh logs postgres
./deploy.sh logs python-api
./deploy.sh logs python-worker
./deploy.sh logs python-worker-2
./deploy.sh logs website
./deploy.sh restart
./deploy.sh python-start
./deploy.sh api-start
./deploy.sh worker-start
./deploy.sh website-start
./deploy.sh queue-status
./deploy.sh status
```

Shutdown is graceful and ordered. `stack-down` stops the collector and
PostgreSQL only. `python-down`, `api-down`, and `worker-down` stop their
respective Python scope; `worker-down` stops every worker replica. `down`
stops the website, workers, API, collector, and PostgreSQL and removes the
containers while retaining the network and data volume. None of these
commands deletes database data or immutable archive objects.

The corresponding supported commands are:

```bash
./deploy.sh stack-down
./deploy.sh python-down
./deploy.sh api-down
./deploy.sh worker-down
./deploy.sh website-down
./deploy.sh down
```

For a one-tag live check after `/readyz` reports ready:

```bash
./deploy.sh enqueue --type live_refresh --tag '#PLAYER_TAG'
./deploy.sh logs collector
curl --fail http://127.0.0.1:8081/readyz
```

Inspect or recover failed work with the supported maintenance commands:

```bash
./deploy.sh maintenance list-failed --limit 20
./deploy.sh maintenance list-leases --limit 20
./deploy.sh maintenance requeue --job-id 123
./deploy.sh maintenance reset-processing --processing-job-id 456
```

These commands return safe identifiers, states, categories, and times; they
do not print API keys or raw response bodies.

## Rollback and rotation

Migrations are forward-only. Application rollback is start-only: select a
previous image compatible with contract version 2 and every applied migration
with `CLASHLENS_COLLECTOR_IMAGE` or `CLASHLENS_PYTHON_IMAGE`, then run
`restart` or `python-start`. For an incompatible schema change, stop the
containers and restore a tested PostgreSQL backup before starting the old
image. `down` does not remove immutable archive objects.

Change a role password in `app.env`, then run `./deploy.sh up`; `restart` does
not change passwords. Replace official API-key files and run `up` or
`restart`. Change archive settings and run `up` and `python-start`.

For HMAC rotation, configure the previous key ID and file together, run
`python-up` or `api-start`, wait longer than the proof lifetime and allowed
clock skew, then remove the previous pair and restart the API. Podman file
secrets are replaced on each start without logging their values.

Backups, point-in-time recovery, and monitoring are outside this script.
Production protection must provide automatic daily and operator-triggered
PostgreSQL backups to encrypted off-host storage, plus a named recovery point
after a completed Legend day is reconciled and frozen. A failed or delayed
freeze must not suppress automatic protection indefinitely. Operators must
define retention and recovery targets and test restoration before relying on
the deployment for production recovery. The existing Google Cloud raw archive
is canonical evidence and is not duplicated merely to label the copy a
backup.
