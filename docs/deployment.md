# Fedora deployment

**Status:** current operator runbook for the merged main deployment: the Go
collector, PostgreSQL, the production Python worker, the private Python API,
and the website on one Fedora host. Google login code is merged into main, but
the root deployment cannot enable login. `deploy.sh` and `app.env.example` do
not pass `CLASHLENS_LOGIN_ENABLED`, the public origin, the Google client ID,
the client-secret file, or the login-secret file. Production login stays
disabled until the deployment passes this configuration and the Python service
enforces the strict inappropriate-name filter. The global Top-200 collector
code exists on main, but the deployment validation keeps it default-off during
beta. The Discord bot, Google Sheets exports, the OBS overlay, public hosted
ingress, and backups and monitoring remain unfinished under
[Issue 31](https://github.com/zzzubair/ClashLens/issues/31). This runbook does
not deploy a hosted service.

This runbook uses direct rootless Podman commands. It does not use Compose.
This runbook was exercised on a Fedora host. The maintained documents describe
merged main. Unmerged throughput work and live deployment drift are separate
and do not redefine this baseline. Reconcile live drift before release.

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
- One admin PostgreSQL password and three runtime role passwords.
- Collector and worker archive credential pairs.

The deployment does not create or print credentials. Keep `app.env` and all
credential files outside version control. It imports the admin password, the
role passwords, the archive credentials, and the API keys into rootless
Podman file secrets. Container metadata contains only secret names and
mounted paths. Role passwords reach PostgreSQL through admin `psql` stdin
and never appear in command arguments or logs.

Use separate archive credentials for the Go collector and the Python
worker. Give the collector only its write and immediate
integrity-verification access. Give the worker its own read-only archive
credential. [Architecture](architecture.md#postgresql-and-archive-ownership)
owns the complete credential boundary.

Use separate database role passwords for the collector, the worker, and the
API. Each role password must contain 32-128 URL-safe unreserved characters
(`A-Za-z0-9_-`). `POSTGRES_USER` remains the admin role that applies
migrations and rotates the three role passwords.

Completion: the service account can run rootless Podman, every required
credential file exists with private permissions, and no credential value is
in the repository.

## 2. Configure

```bash
cp app.env.example app.env
chmod 600 app.env
$EDITOR app.env
```

Replace all placeholders, including `CHANGE_ME` and example hosts. Keep the
official API origin on HTTPS and keep `CLASHLENS_ARCHIVE_SECURE=true`. Set
`CLASHLENS_OFFICIAL_API_PROXY_URL` to the real fixed-egress proxy; the value
must be an HTTP or HTTPS origin with a plain host and optional port. Set
`CLASHLENS_API_KEY_HOST_DIR` to a private host directory. Create the five
files named by the two `*_API_KEY_FILES` settings, the HMAC secret file, and
the optional previous HMAC secret file, and set mode `600` on each file.

The maintainer must select the explicit per-role resource budgets
(`CLASHLENS_POSTGRES_MEMORY`, `CLASHLENS_COLLECTOR_*`, `CLASHLENS_API_*`,
`CLASHLENS_WORKER_*`). The deployment has no chosen default and refuses to
run with a placeholder. Use measured values that preserve headroom for the
host and for temporary workload spikes. The worker lease
(`CLASHLENS_WORKER_LEASE_SECONDS`) sets how long a claimed job may run; the
deployment derives the worker stop grace from it.

The dedicated 16-core Fedora host uses `CLASHLENS_POSTGRES_MEMORY=8g`,
`CLASHLENS_POSTGRES_SHM_SIZE=1g`, and `CLASHLENS_POSTGRES_CPUS=8`. The previous
2 GB / 4 CPU budget constrained the collector's database path while the Go
process remained mostly idle. A 64 MB container shared-memory mount also
caused PostgreSQL `DsmAllocate` failures under the 32-connection collector
stage. Keep the 8 GB / 1 GB / 8 CPU values host-specific; smaller hosts must
select smaller explicit budgets.

Set `CLASHLENS_WORKER_REPLICAS` to the number of identical Python worker
containers. The default of 1 is safe for non-production hosts; production
explicitly sets 6. The valid range is 1 to 16, bounded by the 16-core host
(at most one replica per core), and the deployment rejects any other value
before changing runtime state. Every replica claims jobs from the same
shared queue. The worker resource budgets apply per container, so the
combined worker budget is the replica count times the per-replica budget.

Each worker replica also runs bounded in-process concurrency.
`CLASHLENS_WORKER_CONCURRENCY` (1 to 32) sets the parallel job lanes per
replica; the default 1 preserves the sequential behavior of a single worker.
`CLASHLENS_WORKER_DATABASE_POOL_SIZE` and `CLASHLENS_WORKER_ARCHIVE_POOL_SIZE`
(1 to 64 each) size each replica's PostgreSQL and archive HTTP connection
pools; the defaults 4 and 4 match the sequential worker. The initial
production settings are 20 lanes, 8 database connections, and 20 archive
connections per replica on the 16-core host. Production measurement must
confirm or adjust them. The deployment rejects any out-of-range value before
changing runtime state. These three settings are worker-only and are never
forwarded to the collector.

`CLASHLENS_COLLECTOR_DATABASE_POOL_SIZE` (1 to 64) explicitly bounds the
collector PostgreSQL pool. The default is 16. The initial production stage is
48 database connections with `CLASHLENS_WORKERS_PER_KEY=12`: four normal keys
give 48 normal request workers, and the interactive workers share the same
pool. Keep the six Python replicas at four database connections each for this
stage. With PostgreSQL `max_connections=100`, this budgets 48 collector and 24
worker connections and leaves approximately 25 connections for the API,
administration, and short workload spikes. Production measurement must confirm
the pool sizes before any further increase; do not increase PostgreSQL
`max_connections` or CPU limits as part of this stage.

The script creates one named rootless Podman network and one PostgreSQL
volume. The network has outbound access for the official API and external
archive. It is private because no database or archive service port is
published. PostgreSQL has no published port. The archive has no container or
host port. The collector publishes `/readyz` on `127.0.0.1:8081` by default.
The website publishes only its configured beta ingress host and port.

The repository includes `deploy/egress-proxy/` for the narrow CONNECT proxy
that runs on the `ser5ver` host. Its policy accepts only the Fedora Tailscale
address and only
permits `api.clashofclans.com:443`. The proxy host uses Docker. The proxy
container runs with Docker host networking and no published port, so
Tinyproxy sees the client's real source address and listens on the
configured `PROXY_LISTEN_IP` and `PROXY_PORT` directly instead of a
bridge-mapped container port. The Fedora collector host continues to use
rootless Podman.

Completion: `app.env` has no `CHANGE_ME` value, every key path resolves to a
mode-`600` file, and the archive and official API origins use the required
secure settings.

## 3. Lifecycle

The database carries an explicit contract version. The deployment reads it
before every state change and accepts only `absent`, `1`, or `2`. An unknown
version is rejected without side effects.

```bash
./deploy.sh init
./deploy.sh up
./deploy.sh status
curl --fail http://127.0.0.1:8081/readyz
```

For the reset-baseline lock-seam release, stop every existing Python worker
before `up` reapplies migration 0002, then start the new Python image only
after `up` completes:

```bash
./deploy.sh worker-down
./deploy.sh up
./deploy.sh build-python
./deploy.sh worker-start
```

Migration 0002 removes the temporary worker `UPDATE (id)` privilege on the
collector-owned reset-baseline table. A Python image built before the narrow
`clashlens_lock_reset_baseline_v2` function was added still uses direct
`SELECT ... FOR UPDATE` and is not compatible with the migrated privilege
contract. This order prevents that old image from restarting between the
migration and the new Python start. It does not stop or rebuild the private
Python API or website.

- `init` starts PostgreSQL, waits for readiness, and applies migration 0001
  only on an absent database. On a version-1 or version-2 database it fails
  without side effects.
- `up` builds the collector image, then migrates the contract to version 2.
  On an absent database it applies migration 0001 first. It starts the
  bridge collector against contract version 1, applies migration 0002,
  configures the three runtime role passwords, removes the bridge, and
  starts the required collector. On a version-1 database it uses the same
  bridge path. On a version-2 database it reapplies migration 0002 and the
  role passwords without a bridge. The bridge-before-migration order keeps
  the collector's version-1 observation-job insert valid while the schema
  advances. `up` finishes only when `/readyz` reports `"ready":true`.
- `build-collector` and `build-python` build the immutable images only.
- `python-up` builds the Python image, then starts the private API and the
  production worker and waits for both health checks. It requires contract
  version 2.
- `restart` is the start-only recovery path for an existing version-2
  stack. It never builds and never runs SQL. It requires the collector image
  to exist.
- `python-start` starts the API and worker without building.
  `api-start` and `worker-start` start one role each without building.
  They require the Python image to exist.
- `status` shows the network, volume, every container with its health, and
  the worker queue line without loading unrelated secrets. It shows every
  configured worker replica (`python-worker-1` through
  `python-worker-<N>`) and flags any surplus or legacy worker container
  that a later start or down command will remove.

Completion: `init` and `up` exit with status `0`, `status` shows the
containers, and `/readyz` reports `"ready":true`.

## 4. Private Python API and worker

After `up` has advanced the contract to version 2:

```bash
./deploy.sh python-up
./deploy.sh status
./deploy.sh queue-status
```

`python-up` verifies that the contract is version 2, builds the Python
image, and starts the private API plus `CLASHLENS_WORKER_REPLICAS` worker
containers from the same image. All run with a read-only root filesystem,
dropped capabilities, explicit resource budgets, and file-backed secrets.

The private API listens on `0.0.0.0:8000` inside the private network under
the stable alias `python-api`. It has no published host port; approved
callers reach it only through the private network. It receives its own
database role, its HMAC proof files, one interactive official key file, and
the fixed-egress proxy URL. It never receives archive credentials or the
normal key pool.

The worker runs as identical replicas named
`clashlens-python-worker-1` through `clashlens-python-worker-<N>` (the
name base comes from `CLASHLENS_PYTHON_WORKER_CONTAINER`). Every replica
claims any supported Python job from the same shared queue — no replica is
bound to a job type or endpoint — and each replica has its own container
name and its own `--owner` value. All replicas share the worker database
role and the read-only archive credential through the same three
file-backed secrets. Replicas never receive official API keys, HMAC files,
or the API role. Every job writes product data only inside its fenced
transaction.

Every replica receives the configured `--concurrency`,
`--database-pool-size`, and `--archive-pool-size` bounds from
`CLASHLENS_WORKER_CONCURRENCY`, `CLASHLENS_WORKER_DATABASE_POOL_SIZE`, and
`CLASHLENS_WORKER_ARCHIVE_POOL_SIZE`. These worker-only settings are never
forwarded to the collector environment.

The deployment replaces the shared worker secrets only after every worker
container has stopped, so no running replica ever mounts a replaced
secret. When the configured count is reduced, surplus replica containers
are stopped and removed on the next `python-start`, `worker-start`, or
down command; a legacy single-worker container from before replicas is
stopped and removed the same way.

`queue-status` prints the shared queue summary from inside replica 1.
`status` includes the same summary line when a replica runs, shows every
configured replica with its health, and flags any surplus or legacy worker
container. `logs python-worker` shows replica 1; `logs python-worker-N`
shows that specific replica.

Completion: `status` shows the API and worker as healthy, and `queue-status`
prints the queue summary without secrets.

## 5. Website foundation

After `python-up` reports a healthy private API, run:

```bash
./deploy.sh website-up
./deploy.sh status
curl --fail http://127.0.0.1:3000/healthz
```

`website-up` builds the website image, then replaces, starts, and waits for the website. `website-start` is the recovery path. It never builds an image or runs SQL. Both require contract version 2, the private network, a running healthy Python API, and the website image for start-only recovery.

The website connects only to `http://python-api:8000` on the private Podman network. It mounts only `clashlens-python-api-hmac-current` as `/run/secrets/clashlens-python-hmac`. It gets no database, official API, archive, worker, collector, or admin secret. It uses the image `node` user, a read-only root filesystem, a hardened `/tmp` tmpfs, dropped capabilities, no new privileges, explicit resource budgets, and a bounded Node health check.

The beta ingress initially uses `CLASHLENS_WEBSITE_HOST` and `CLASHLENS_WEBSITE_PORT`. The host accepts `127.0.0.1`, `0.0.0.0`, or a plain IPv4 address. No hosted service is configured. The website image contains the Google login and account code, but the deployment does not pass the login configuration: the enable flag, the public origin, the Google client ID, the client-secret file, and the login-secret file. The root deployment cannot enable login, so login stays disabled. Analytics and other remaining website surfaces remain future work. Keep ingress and hosted-service choices open.

Completion: `status` shows `website: running (healthy)`, and the configured host and port return `/healthz`.

## 6. Install the user services

Install the tracked rootless user services after the first successful `up`
and `python-up`:

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

The host must enable lingering for the `clashlens` account so these user
services start without an interactive login.

Boot is start-only. The collector stack unit runs `deploy.sh restart`; the
API unit runs `deploy.sh api-start`; the worker unit runs
`deploy.sh worker-start`; and the website unit runs `deploy.sh website-start`.
No unit builds an image or runs SQL at boot. The website unit requires the API
and collector stack units, and starts after them. On shutdown, systemd stops
the website and Python units before the collector stack, which drains
dependents before PostgreSQL stops.

Completion: `systemctl --user status clashlens.service`,
`systemctl --user status clashlens-python-api.service`,
`systemctl --user status clashlens-python-worker.service`, and
`systemctl --user status clashlens-website.service` show active services, and
all services start after a host restart without an interactive login.

## 7. Operate the current stack

Use only the supported lifecycle commands:

```bash
./deploy.sh logs collector
./deploy.sh logs postgres
./deploy.sh logs python-api
./deploy.sh logs python-worker
./deploy.sh logs python-worker-2
./deploy.sh logs website
./deploy.sh restart
./deploy.sh python-start
./deploy.sh website-start
./deploy.sh api-start
./deploy.sh worker-start
./deploy.sh queue-status
./deploy.sh website-down
./deploy.sh api-down
./deploy.sh worker-down
./deploy.sh python-down
./deploy.sh stack-down
./deploy.sh down
```

Shutdown is graceful and ordered. Every stop sends SIGTERM with a grace
period before removal: worker `lease + 10` seconds, API and collector 30
seconds, PostgreSQL 60 seconds. `down` stops the website, then every
worker replica, then the API, then the collector, then PostgreSQL, and
removes all containers while keeping the network and the data volume.
`stack-down` stops the collector and PostgreSQL only. `python-down`,
`api-down`, and `worker-down` stop one Python scope only; `worker-down`
stops every worker replica, including surplus and legacy worker
containers. None of the down commands deletes evidence or database data.

Completion: each down command exits with status `0`, and `status` shows the
expected remaining containers.

## 8. Run a one-tag live test

Use one known tag only after `up` reports ready. The command submits a
durable interactive refresh and prints its job result. It does not print
API keys.

```bash
./deploy.sh enqueue --type live_refresh --tag '#PLAYER_TAG'
./deploy.sh logs collector
curl --fail http://127.0.0.1:8081/readyz
```

Check the collector log for the job result and check the external archive
for new immutable response objects. Use the HTTPS official origin and the
complete configured key pools.

Completion: the job reaches an inspectable terminal result, `/readyz`
remains ready, and the archive contains the new immutable response objects
without a credential appearing in output.

## 9. Inspect failures

Run these commands while the collector container is running. The wrapper
enters that container, and the maintenance command uses the database URL
and optional schema version. Use only the supported maintenance commands:

```bash
./deploy.sh maintenance list-failed --limit 20
./deploy.sh maintenance list-leases --limit 20
./deploy.sh maintenance requeue --job-id 123
./deploy.sh maintenance reset-processing --processing-job-id 456
```

Maintenance output contains safe IDs, states, categories, and times. It does
not contain API-key values or raw response bodies. Before you requeue or reset
work, inspect the failure category and preserve the evidence that caused it.

Completion: each inspection command returns safe JSON-lines output, and each
recovery command changes only the requested durable job state.

## 10. Rollback and secret rotation

The deployment does not perform automatic database rollback. Migrations are
forward-only in this first deployment.

- Application rollback is start-only. Select a previous compatible image
  tag with `CLASHLENS_COLLECTOR_IMAGE` or `CLASHLENS_PYTHON_IMAGE`, then run
  `./deploy.sh restart` or `./deploy.sh python-start`. These commands never
  build and never run SQL; `python-start` starts every worker replica from
  the selected image. The previous image must be compatible with contract
  version 2. A Python image that directly locks
  `collector_reset_baseline_sweeps` is not compatible after migration 0002
  revokes the temporary worker column privilege. Restoring that image also
  requires the tested pre-migration PostgreSQL backup path below.
- For an incompatible schema change, stop the collector and Python
  containers, keep the volume, and restore a tested PostgreSQL backup before
  starting the old image. Immutable archive objects are not removed by
  `down` and are not rolled back.
- Role password rotation: change the role password in `app.env`, then run
  `./deploy.sh up`. `up` always reapplies migration 0002 and reconfigures
  the three role passwords through admin `psql` stdin. `restart` does not
  touch passwords. The admin password rotation requires a separate
  `init`-style admin operation and is not part of this runbook.
- HMAC rotation: configure `CLASHLENS_HMAC_PREVIOUS_KEY_ID` and
  `CLASHLENS_HMAC_PREVIOUS_SECRET_FILE` together, run `python-up` or
  `api-start` to mount both secrets, wait longer than the proof lifetime
  and clock-skew allowance, then remove the previous pair and restart the
  API.
- Official API key rotation: replace the host key files, then run `up` or
  `restart` so the collector secrets are recreated from the new files.
- Archive credential rotation: change the archive settings in `app.env`,
  then run `up` and `python-start` so the collector and worker secrets are
  recreated. `python-start` stops every worker replica before it replaces
  the shared worker secrets, so rotation is safe with replicas running.

Secret rotation scope: the deployment replaces every Podman file secret on
each start (`secret create --replace`). No rotation command prints or logs a
secret value.

The deployment does not configure backups, point-in-time recovery, or
monitoring. Operators must provide and test them before production use.

The replay contract is owned by [Architecture](architecture.md#replay).
Replay requests require an allowlisted authenticated host operator through
the root-owned wrapper; no application role can insert replay requests.

Completion: the PostgreSQL volume and immutable archive remain intact, and
the selected image is compatible with the current schema before restart.

## 11. Beta support transfer boundary

Install `deploy/support-transfer` as `root:root` with mode `0700` in a
root-owned non-writable directory. Use a narrow `NOSETENV` sudoers rule
with environment reset that permits only this wrapper for each approved
host operator. The support wrapper is the only audited path that transfers
a verified player link between Clash Lens accounts; no application role
transfers links automatically.

Create `/etc/clashlens/support-transfer-operators` as a `root:root`
mode-`0600` allowlist. Create
`/etc/clashlens/support-transfer.pg_service.conf` as a `root:root`
mode-`0600` PostgreSQL service file. The service file must use the
dedicated `clashlens_support_transfer` database role and its credential. Do
not give that credential to a container or application role.

The wrapper requires the exact opaque verification candidate UUID, player
tag, source account public UUID, destination account public UUID, and a
non-secret reason. It derives the operator from the sudo audit context. It
prints only a sanitized status. It does not accept a player token.

Completion: the wrapper, allowlist, and service file have the required
owner and mode. The support role can execute only the transfer function.
Application, worker, bot, collector, and public roles cannot execute that
function or update its tables directly.
