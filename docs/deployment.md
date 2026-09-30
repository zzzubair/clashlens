# Fedora operation

For daily health checks, responses to each alert and the restore entry point,
see [Operating Clash Lens on rogue](operating.md).

## Launch order agreed on September 27

The [product map](product-status.md) and [#140](https://github.com/zzzubair/clashlens/issues/140)
separate collection readiness from public website launch. Tracking must be
complete before the October 5 05:00 UTC season boundary, targeting a September 30
start, with 12,500 live players within a total known pool of 22,157 supplied
tags. Check known-player eligibility weekly at the Monday transition,
automatically from the October 12 Reset, reusing fresh live profiles and that
week's completed checks. Add any new list before tracking starts and the
October 5 list during collection, with one identity per tag. New tags need
their first check. Starting a cold import at 05:00 is not proof of complete
Day 1 data.

Before real traffic: prove backups/restores, delivered private Discord alerts,
repeated imports, accuracy and capacity for 12,500 live players plus weekly
known-player checks. #128 owns the bounded
real rehearsal; its resulting cost/storage evidence must be accepted before
ongoing collection. The complete public website and clan discovery need not
delay starting from supplied lists. Public release separately needs the real
domain/logins, full feature checks, invited testing and #131 handover.

Current commands below describe implemented behavior. The old bootstrap command
caps at 20,000 tags and rejects duplicate input and later new imports. Launch
lists will use the [verified manual operator procedure](manual-list-import-validation.md),
which preserves existing identities and queues checks through existing functions.
No reusable import feature is needed. Live import and weekly scheduling/reuse
remain pending. `./dev trial` supports the 12,500 active-player target, and
`./ops` loads four regular API keys. #128 must measure that active workload
alongside the 22,157-tag known pool and weekly checks. Raising the live-player
trial limit or adding keys is not required merely because the known pool is
larger. The collector supports more keys and Zubair can supply them if measured
demand requires additional production wiring. Keep existing safeguards.

## Existing service lifecycle

`./ops` runs Clash Lens as rootless Podman containers managed by the user's
systemd service manager. The tracked files under `deploy/quadlet/` are
Quadlets: Podman turns them into ordinary system services. PostgreSQL, the
collector, private API, worker, and website are owned by one
`clashlens.target`, so they start together after reboot and stop together.

Building and running are separate operations. `up` never builds or pulls an
image. `build` records the exact image IDs and a fingerprint of every source,
migration, and unit input; `up` refuses to mix those images with changed init
files. A new build is only staged: operator and recovery commands keep using
the last successfully started release until `up` promotes the new one.

## Clean Fedora fixture

Install Git and rootless Podman, clone this repository as the service account,
then run:

```sh
sudo dnf install git podman
./ops build --fixture
./ops up --fixture
./ops status
```

The fixture is explicit. It uses the same application images, PostgreSQL 18,
migrations, role separation, and initialization as production, with local
Clash, archive, Google, and Discord substitutes. It makes no official Clash
request and needs no cloud or OAuth credential. Its published endpoints are
loopback only: the website at `http://127.0.0.1:15174`, collector health at
`http://127.0.0.1:18081`, and login providers at ports 8011 and 8012.

`up` enables systemd lingering for the service account, using `sudo loginctl`
when needed, so rootless services run without an interactive login and return
after reboot. On a host where sudo is restricted, an administrator can run
`loginctl enable-linger SERVICE_ACCOUNT` first.

Check the actual reboot state after the machine returns:

```sh
./ops status
curl --fail http://127.0.0.1:15174/players/%232PP
```

To run the existing browser suite against this stack, build its existing check
image and set `CLASHLENS_E2E_ORIGIN=http://127.0.0.1:15174`. No separate test
harness is needed.

Stopping keeps PostgreSQL, spool, and fixture archive data, and disables the
target so it stays stopped after later reboots:

```sh
./ops down
./ops status
```

## Production configuration

Copy `app.env.example` to `app.env`, replace every `CHANGE_ME`, and make it
private:

```sh
cp app.env.example app.env
chmod 600 app.env
```

Create the configured spool as a dedicated directory owned by the service
account with mode 700. Keep it outside the account's home, checkout, ops state
and unit directories, and secret directory; `up` refuses overlapping paths
before stopping the current stack. Under
`CLASHLENS_API_KEY_HOST_DIR`, create five mode-600 files named
`clashlens-normal-1` through `clashlens-normal-4` and
`clashlens-interactive-1`, plus the HMAC file named by
`CLASHLENS_HMAC_SECRET_FILE`. `./ops` transfers their values into Podman's
private secret store. API keys are supplied to the collector as an environment
secret because its current CLI accepts `label=value` key pools; they are not
written to a unit, environment file, or process argument. The API receives
only the interactive key as a mounted file.

Set `CLASHLENS_OFFICIAL_API_PROXY_URL` to the fixed-address relay's HTTP origin
on Tailscale, reachable from inside the pod. `ops` supplies this setting to
both collection and private player-token verification. The standalone collector
defaults to direct access when this setting is empty; `--official-proxy-url`
overrides it. Explicit proxy settings ignore ambient `NO_PROXY`, and a relay
failure never falls back to a direct connection. Token verification retains its
existing requirement for a proxy outside local tests.

The archive credentials have separate duties. The collector credential creates
immutable raw responses and may read back the marker or one exact object to
prove a write; the worker credential can only read. Neither runtime credential
may list, overwrite, delete, or broadly browse archive objects.
The database also has separate collector, worker, and API roles. The admin
database URL exists only as a short-lived Podman secret during fixture
bootstrap or while an operator explicitly handles a failed item.

### Paris fixed-address relay

`clashlens-egress-paris` is a Scaleway DEV1-S in `fr-par-1`: two processor cores,
2 GiB memory, a 10 GB local boot disk and a 200 Mbps connection. Its server ID
is `11bee593-3b31-40c0-80b1-28e24614c9ec`. The reserved public IPv4 is
**163.172.188.40**, allocation `38ccd5b5-6185-46f9-8312-cd1720688832`.
Scaleway reports the allocation as non-dynamic; keep it allocated when rebuilding
the server. This address is what all five Clash API keys must allow.

The September 30 quote totals **EUR 10.56/month before tax** at 730 hours:
EUR 6.55 compute, EUR 3.65 IPv4 and EUR 0.36 for the 10 GB local disk. This fits
the approved EUR 11–15/month budget. Outgoing instance
traffic is included. At 83.3 requests/second, response sizes of 10, 25 or 50 KB
mean approximately 2.7, 6.75 or 13.5 TB/month with a 25% traffic allowance.
These are sizing assumptions, not measured successful response sizes.
See [instance pricing](https://www.scaleway.com/en/pricing/virtual-instances/)
and [the June 2026 IPv4 price update](https://www.scaleway.com/en/blog/a-transparent-update-on-scaleway-pricing/).
The [public product catalog](https://www.scaleway.com/en/developers/api/product-catalog/public-catalog)
quotes local disk product `/instance/volume/l_ssd/fr-par-1` at EUR 0.000049/GB/hour.

The private relay address is **100.122.10.22**, named
`clashlens-egress-paris.tail54c4a2.ts.net`, with tailnet tag `tag:clashlens-egress`.
The server runs Ubuntu 24.04, Docker and Tailscale. The relay code is in
`/opt/clashlens/egress-proxy`; generated configuration is in
`/root/.local/share/clashlens-egress-proxy`. `deploy/egress-proxy/deploy.sh`
runs Tinyproxy without root privileges, with a read-only filesystem, a 64 MiB
memory limit, at most 50 connections, and warning logs capped at three 10 MB
files. It holds no API keys or response archive. Thirty collector connections
plus token-verification traffic fit below that connection limit.

The filter allows only `CONNECT api.clashofclans.com:443`, an encrypted tunnel
whose API certificate the caller still checks. Ordinary HTTP requests, other
hosts and other ports are rejected. The proxy binds only its private Tailscale
address on port 3128 and accepts only rogue's Tailscale address `100.115.149.49`.
The cloud firewall denies inbound traffic except UDP 41641 for Tailscale and
the restricted SSH administration rule. Password login is disabled. Tailscale's
host firewall chain accepts private-interface traffic before UFW rules;
Tinyproxy's client allowlist enforces rogue-only proxy access. A request from
another tailnet machine was rejected with proxy status 403.

The collector keeps its existing reusable connection pool, request-start limits,
certificate checks, request deadlines and cancellation behavior through the
relay. A failed connection does not quarantine a key. A genuine API 401/403
still does. An outage makes requests fail or time out; the existing stopped
tracker alert takes about 10–11 minutes without successful fetches. A relay
does not remove the dependency on rogue's home connection.

For recovery, connect from rogue with
`ssh -o HostKeyAlias=163.172.188.40 ubuntu@100.122.10.22`. The alias checks
the same SSH host key already verified at the public address. On the relay, inspect `systemctl status tailscaled docker`, `sudo docker logs
clashlens-egress-proxy`, and `sudo ss -lntp`. Restart only the relay container
after diagnosing it. Docker and Tailscale start on boot, and the container has
`unless-stopped` restart behavior. If the machine must be rebuilt, retain the
IPv4 allocation above, attach it to the replacement in the same zone, install
Docker and Tailscale, authorize the new Tailscale machine, and copy this checkout's
`deploy/egress-proxy` directory to `/opt/clashlens/egress-proxy`. Then run:

```sh
sudo env PROXY_LISTEN_IP=100.122.10.22 PROXY_CLIENT_IP=100.115.149.49 \
  /opt/clashlens/egress-proxy/deploy.sh up
```

For a replacement, substitute its actual Tailscale address and repeat the connectivity,
destination-filter and latency checks before changing callers. Never release
the public IPv4 as part of routine recovery. A replacement private address
requires updating both callers through `CLASHLENS_OFFICIAL_API_PROXY_URL`.

#### Measured relay behavior

On September 30, 2026, after rogue's temporary network outage ended, paired
HTTP/1.1 requests from rogue measured the following times in milliseconds.
The warm rows exclude the first connection; each run reused one connection for
all 300 measured requests. The cold runs opened 100 connections each and disabled
TLS session reuse. Median is the middle sample; p95 and p99 are the times within
which 95% and 99% of samples finished, using nearest-rank percentiles.

| Path | Samples | Mean ms | Median ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct, reused connection | 300 | 80.837 | 80.767 | 82.694 | 87.690 |
| Paris relay, reused connection | 300 | 93.590 | 93.492 | 95.504 | 99.727 |
| Direct, new connection | 100 | 242.892 | 240.870 | 246.028 | 324.346 |
| Paris relay, new connection | 100 | 348.790 | 378.425 | 387.203 | 388.355 |

The warm mean added **12.753 ms**, inside the proposed 60 ms allowance. New
connections added **105.898 ms** on average, so retaining connection reuse is
essential. Runs started at 19:56:26 UTC for warm requests and 19:57:27 for cold
requests, paced at five starts/second per path. All 802 requests completed
without network errors and returned the expected unauthenticated HTTP 403 with
a 59-byte `Missing authorization` response. Cold totals include connection and
encryption setup; they do not meet the 200 ms warm full-response target.

Separate 30-second runs used 24 warmed client slots with a shared ceiling of
120 request starts/second. The direct path completed 3,530 requests, 117.67
starts/second, with mean 80.645 ms and p95/p99 82.692/89.913 ms. The relay
completed 3,552 requests, 118.40 starts/second, with mean 92.935 ms and p95/p99
95.139/99.033 ms. All responses had the same expected 403 and 59-byte body;
there were no transport failures. Each path also made 24 warm-up requests.
The relay's observed container use during that run was 3.527 MiB of its 64 MiB
limit and 2.48% CPU, with 24 established proxy connections. These are one-time
resource samples, not long-term maxima.

A separate relay-to-rogue transfer sent 2 GiB in **81.842 seconds**, averaging
**209.91 Mbps** over Tailscale and SSH with compression disabled. An earlier
768 MiB transfer took 22.756 seconds, 283.12 Mbps, showing a short burst above
the advertised rate. Budget against the advertised 200 Mbps rather than the
burst result. This checks the private network's capacity, not transfer of real
API response bodies through Tinyproxy. Both transfers generated bytes from
`/dev/zero` and discarded them on rogue, without writing large files.

Rogue's `tailscale ping` and active peer address confirmed direct traffic to
`163.172.188.40:41641`, with 10–14 ms round-trip delay. The DERP fallback path
was not forced or benchmarked. Actual private-address binding, rejection of
another tailnet client, rejection of other destinations/ports and plain HTTP,
and automatic recovery of the proxy and Tailscale after reboot were verified.
The reserved public IPv4 remained unchanged across both provisioning reboots.

The load check measures transport of small unauthenticated responses, using
Python's standard HTTPS client with CONNECT tunneling and certificate checking;
it is not a successful collector workload. The collector's own connection reuse,
certificate rejection, proxy-down behavior, request pacing, timeout and
cancellation are covered by local tests with a real HTTPS origin and CONNECT
relay. Successful profile/battle response sizes, provider processing time,
day-long loss, full collection/storage load, Reset gaps and outage alerts remain
unverified. Do not treat the unauthenticated rate as proof of launch capacity.

#### Production switch-over, separate approval required

1. Confirm `tailscale ping clashlens-egress-paris` from rogue reports a direct
   connection. A result using DERP, Tailscale's public fallback relay, needs a
   new latency/throughput check. Verify an unauthenticated request through
   `http://100.122.10.22:3128` returns the official API's missing-authorization
   response with certificate checks enabled.
2. Add **163.172.188.40/32** to each of the four regular keys and the interactive
   key in the Clash developer portal. If this requires replacement key values,
   put them in the existing five private key files. Do not print them or put
   them in a command line, this document or the relay.
3. Deploy the reviewed release to rogue through the existing release procedure.
   Set `CLASHLENS_OFFICIAL_API_PROXY_URL=http://100.122.10.22:3128` in its
   private `app.env`, then run `./ops build` and `./ops up`. This is the production
   restart step and has not been authorized or performed by the relay task.
4. Confirm successful collection and private token verification, healthy keys,
   and the relay's direct connection mode from inside the running pod's network.
   Measure full successful profile/battle responses under the intended workload:
   aim for at most 60 ms extra relay delay and 200 ms mean request time. Check
   per-player gaps across Reset separately. Unauthenticated probes do not prove
   these behaviors.
5. If checks fail, stop the switch and diagnose it. A return to direct collection
   requires a working home address on the keys and a separately agreed route
   for token verification; removing the proxy setting alone is not a recovery
   plan. Never allow an automatic direct fallback to an unapproved address.

### Production archive: Scaleway Object Storage

The raw-response archive is the Scaleway bucket `clashlens-raw-evidence` in
`nl-ams` (Standard Multi-AZ, private, versioning off). Three Scaleway IAM
applications each hold one API key:

- `clashlens-archive-collector` — IAM grants object read/write; the bucket
  policy narrows it to `GetObject` plus `PutObject` only when the
  `If-None-Match` conditional-create header is present, so it can never
  overwrite. Its key pair goes in `app.env` as `CLASHLENS_ARCHIVE_*`.
- `clashlens-archive-worker` — object read only. Its key pair goes in
  `app.env` as `CLASHLENS_WORKER_ARCHIVE_*`.
- `clashlens-archive-operator` — object delete only, for `prune-archive`.
  Keep its key pair in a separate mode-600 file outside `app.env`, the
  checkout, and the generated unit environment; it is used only by explicit
  operator runs.

The bucket policy is an allowlist: anything not granted there is denied for
the scoped credentials, which is what makes list, delete, and unconditional
writes fail for the runtime identities. Bucket configuration changes (policy
updates, versioning checks) need the account's own credential; while the
policy is attached, even it cannot read bucket configuration, so remove the
policy, make the change, and re-apply it.

The marker object at `clashlens/archive-instance.json` pins the archive
identity. `CLASHLENS_ARCHIVE_MARKER_HASH` is the lowercase SHA-256 of its
exact bytes; `up` records the whole contract in `archive_instances` and
refuses to start against a changed one.

IAM API keys expire one year after creation (organization policy). Rotate
each pair by creating a new key on the same application, updating `app.env`,
running `up`, and deleting the old key once the stack is healthy.

Keep `CLASHLENS_GLOBAL_RANKINGS_ENABLED=false` until real collection is
approved; with it off and no tracked players, the collector makes no
official API calls at all.

Build from the checkout to be released, review the resulting commit, then run
the already-built release:

```sh
./ops build
./ops up
./ops status
```

The website and collector health endpoints bind to `127.0.0.1`; PostgreSQL and
the private API have no host port. Production discovery and the global Top-200
request remain disabled until real collection is approved.

`up` first disables and stops the whole target. It then starts PostgreSQL by
itself, applies every missing numbered migration in order, verifies the fixed
archive contract, rotates the admin and runtime-role passwords through standard
input, and only then enables the application target. A failed migration leaves application
services disabled for the next reboot. Re-running `up` applies only migrations
whose recorded version is absent.

## PostgreSQL backups and recovery

Production builds add WAL-G v3.0.9 to the existing PostgreSQL 18 Alpine image.
The source archive is SHA-256 checked and compiled without libc dependencies.
This avoids changing the database's text ordering by switching to Debian.
Fixture builds still use the unmodified PostgreSQL image.

Backups are opt-in. After approving deployment, set these in private `app.env`:

```ini
CLASHLENS_BACKUP_ENABLED=true
CLASHLENS_BACKUP_ENDPOINT=https://ACCOUNT_ID.eu.r2.cloudflarestorage.com
CLASHLENS_BACKUP_PREFIX=s3://clashlens-pg-backup/rogue-pg18
CLASHLENS_BACKUP_CREDENTIAL_FILE=/srv/clashlens-secrets/clashlens-backup-r2.env
```

Use a fresh cluster prefix for a new database or a major PostgreSQL upgrade.
Never share it with a scratch database. WAL-G owns `basebackups_005/` and
`wal_005/` below that prefix. Leave bucket lifecycle deletion disabled.

The credential file contains only `AWS_ACCESS_KEY_ID=...` and
`AWS_SECRET_ACCESS_KEY=...`, without quotes. It must belong to the service
account with mode 600. `ops` parses it without executing it and mounts the
resulting JSON as a Podman secret readable only by PostgreSQL's OS user.
The collector, worker and website receive no backup credentials. Keep the
separate read-only recovery key outside runtime units, and keep an additional
protected copy off rogue so losing the host does not also lose recovery access.
Rotate credentials by replacing the file, running the approved `ops up`,
verifying backup and restore, then revoking the old key in Cloudflare.

The timer starts a full backup Sundays at 03:00 UTC. Missed calendar runs are
caught up; restarting the stack does not add another full backup. Run
`./ops backup` once during the approved initial rollout. PostgreSQL continuously uploads
its change log, called WAL, with `archive_timeout=300`. A successful full backup
then prunes backups older than the full backup completed before seven days and
one hour ago. It retains that backup and all newer backups and WAL. Until such
a backup exists, pruning deletes nothing. Extra manual backups cannot shorten
the recovery window. The one-hour margin covers scheduling and backup duration;
this typically retains two or three weekly full backups, rather than exactly two.

```sh
./ops backup                  # upload now, then apply age-based retention
./ops backup-status           # remote backup freshness and pending WAL health
./ops backup-prune            # deletion preview only
./ops backup-prune --apply    # apply the same retention rule after approval
./ops logs backup --since today
```

The existing operation lock prevents concurrent backup, deployment and cleanup.
A scheduled backup waits for an in-progress operation; a manually started backup
and backup pruning still fail immediately on contention. `down` stops the timer
and backup service. A failed upload never runs pruning. Backup commands reject
changes covered by the active release fingerprint before accessing backup
credentials or remote storage.
Release fingerprints use byte-ordered filenames so terminal and scheduled-service
language settings cannot make unchanged code appear different. Rebuild and deploy
after upgrading this check; do not edit a saved fingerprint or bypass the guard.
`backup-status` exits unsuccessfully for a failed service, inactive timer,
missing/unreachable remote backups, a full backup older than eight days, disabled
archiving, or completed WAL files waiting over ten minutes. No WAL activity during
an idle period is not itself failure. `./ops alert-check` alerts on this command
and disk space: PostgreSQL retains unarchived WAL locally during a storage outage
and that queue is not capped by `max_wal_size`. Never delete unarchived WAL to
free space.

### Restore into a separate database

Use the pinned backup image from the release manifest and the read-only key.
Create a temporary Podman secret containing the same JSON settings as the backup
configuration, substituting only the read-only credentials. Do not print it.
Use a new empty named volume, a unique container name, no production pod and no
published port. Run WAL-G as OS user `postgres`, mounting the secret at
`/run/secrets/walg.json` with uid/gid 70 and mode 0400.

1. Run `wal-g --config /run/secrets/walg.json backup-list --detail --json`.
   Choose a full backup whose **finish time precedes the desired recovery time**.
   `LATEST` is unsuitable when recovering an older point.
2. Run `wal-g --config /run/secrets/walg.json backup-fetch /var/lib/postgresql/data/pgdata BACKUP_NAME`
   against the new volume. Create `recovery.signal` in that directory.
3. Start PostgreSQL with that volume and `PGDATA`, the read-only secret,
   `archive_mode=off`, an empty `archive_command`,
   `restore_command=wal-g --config /run/secrets/walg.json wal-fetch %f %p`,
   `recovery_target_time=CHOSEN_UTC_TIME`, and `recovery_target_action=pause`.
4. Confirm both `pg_is_in_recovery()` and `pg_is_wal_replay_paused()` are true,
   and the log says recovery reached the chosen time. Merely accepting queries
   does not prove the target was reached. Compare expected accounts, saved-player
   links, player history, battle links and army summaries. Read every retained raw
   object referenced by the sample and verify its hash. Missing required evidence
   means the restore failed. Do not promote this scratch database into production.
5. Repeat for the seven-day-old boundary, using a full backup from before it.
   Repeat affected checks after the season history and cleanup work in #140
   changes stored data or maintenance.

### Targets, cost and remaining rollout checks

The intended maximum data loss is **5 minutes plus upload delay**, not a hard
five-minute guarantee. A provider outage can exceed it. The initial operational
restore target is **60 minutes**, pending measurement at production size.
Seven-day recovery starts only after seven days of uninterrupted archived history.

The approved pricing model assumed 100 GB per full backup and 30 GB of WAL per
seven days. Keeping two to three full backups plus up to roughly 14 days of WAL
models **260–360 GB**, about **$3.75–$5.25/month** at the #120 R2 rate and free
allowance, rather than its original roughly $3 estimate. This is a model, not
measured production growth. Manual backups add up to another full backup each
until they age out; failed uploads can leave partial objects requiring separately
reviewed cleanup. Real traffic must be measured before accepting the €60 total
monthly envelope. Do not silently let failed pruning or WAL uploads accumulate.

On September 25 Zubair chose to preserve raw responses for the entire promised
seven-day recovery window, including time to restore. The current season-end
plus 56-day expiry code does not yet enforce this protection: a restored
catalogue could reference a response deleted later. #122 owns implementation
and restore proof; #129 must use that protection in scheduled cleanup. Do not
enable production expiry until it is proven. Seven extra days add about 10%
to the older modeled 70-day average raw lifetime, roughly €0.40–€2/month using
#120's €4–€20 range. This is an earlier pricing model, not a measured bill or a
final restore allowance; reprice it for 12,500 live players, weekly checks of
the 22,157-tag known pool and measured growth.

Validation on 2026-09-19 used a separate PostgreSQL cluster with all 34 migrations
and synthetic records, under R2 prefix `validation-20260919`. A full backup took
4.49 seconds, fetching it took 3.46 seconds, and recovery reached the selected
recent point at 22:53:20 UTC, about 17 seconds after fetch began. The recent
target was 22:53:03.020339 UTC; it recovered display name `Recovered recent point`
and 5,080 trophies, excluding later changes to the name and 5,120 trophies.
An earlier restore to 22:52:18 UTC recovered `Before backup` and 5,040 trophies.
Both retained the saved-player link, battle link and army summary. The read-only
R2 key fetched the backup and WAL; direct write/delete attempts were denied.
These tiny-database timings do not establish production recovery time or worst-case
data loss. The earlier target was minutes old, **not seven days old**.

The backup sorting fix #134 was merged and deployed; its scheduled service was
also invoked successfully, as recorded in #122. The September 27 03:00 UTC backup
fired naturally, as recorded in [#140](https://github.com/zzzubair/clashlens/issues/140).
Before closing #122, measure data loss and restore time at the revised size,
verify restored raw references across protected expiry, restore a genuine
seven-day-old point, and verify host reboot. Earlier small restores and service
checks do not prove those remaining gates. Keep real collection disabled until
backups and #124 alerts are proven. This documentation update does not deploy
or close #122.

## Status and logs

```sh
./ops status
./ops logs
./ops logs collector
./ops logs postgres --since today
./ops logs worker -f
./ops queue-status
```

`status` fails when the target or any required system service is stopped, or when
any required container is absent, stopped, or unhealthy. Logs come from the user
journal, which includes both container output and systemd lifecycle failures
without printing configuration files.

## Private Discord alerts

`./ops alert-check` checks the five launch conditions and posts changes to the
private operator channel through an incoming webhook. Create the service-owned
mode-600 file `/srv/clashlens-secrets/clashlens-discord-alert-webhook` separately.
Its default directory follows `CLASHLENS_API_KEY_HOST_DIR`; an optional
`CLASHLENS_DISCORD_ALERT_WEBHOOK_FILE` overrides the full path. Store only the URL
in that file. Never put it in `app.env`, command arguments, logs or documentation.
A missing, unreadable, wrongly owned or non-600 file fails the check loudly.

A subsequently approved production `./ops up` installs `clashlens-alert.service`
and `clashlens-alert.timer`. The enabled `clashlens.target` starts the timer on
reboot, using the same installation path as the backup timer. It checks every
minute with `Persistent=true`, so a missed calendar check runs after reboot.
Both units use `PartOf=clashlens.target`. The service has no dependency on a
healthy collector or API, so failed processes cannot prevent the check starting.
Fixture mode does not install or start Discord alerts. `./ops down` records an
intentional stop and stops the timer; manual checks also stay quiet until a
successful `./ops up`. Checks also stay quiet while `up` starts the stack.
Time deliberately stopped does not count as a fetch gap.

### Alert conditions

The thresholds below define when alerts fire. For investigation and recovery,
use the [operating notes](operating.md#respond-to-alerts).

- No successful official API fetch for **600 seconds**, excluding time within
  **04:55–05:00 UTC**. This uses the collector's persisted last-success age.
  With no success yet, the clock starts at the first check. Missing metrics
  continue that clock and fail the check. Reset collection after 05:00 must
  still progress; an unfinished Reset sweep does not suppress alerts forever.
- Spool bytes above **80% of `CLASHLENS_SPOOL_MAX_BYTES`**, spool objects above
  **80% of `CLASHLENS_SPOOL_MAX_OBJECTS`**, or either filesystem holding the spool
  or PostgreSQL volume above **80% used**. Exactly 80% does not trigger.
- **More than three automatic restarts of any one Clash Lens service in the
  preceding hour**, counted from systemd's structured restart journal events.
  Counter resets and checker restarts do not erase this history. Keep at least
  one hour of user journal history.
- **Any failure of `./ops backup-status`**. The alert check invokes the existing
  command; [backup operations](#postgresql-backups-and-recovery) documents its failure
  conditions and freshness limits.
- **A failed private player-data read**, including when process readiness says
  healthy. The check enters the private API container, checks `/readyz`, and
  signs a `/v1/players/search` read limited to one result. Keys stay inside the
  container and response data is discarded. An empty search result is valid.

Messages give the condition, its first observed UTC time and one next step.
There is one alert and one recovery per condition; unchanged checks stay quiet.
Missing disk measurements or restart history never clear an existing alert.
`alerts.json` and `alerts.lock` live under the existing private ops state
folder, `${XDG_STATE_HOME:-$HOME/.local/state}/clashlens`. State is atomically
replaced with mode 600 and contains five condition records with at most one
pending transition each, normally under 4 KiB. It keeps no growing event history,
keys, URLs, player lists or account data.

Only a Discord **2xx response** confirms delivery. Redirects, timeouts and other
responses fail the command and leave the transition pending for the next run.
A saved alert is retried even if the condition has since recovered, followed by
its recovery. A lost HTTP acknowledgement or a crash after Discord accepts a
message but before state is saved can cause one duplicate on retry. During
extended delivery failure the bounded state preserves pending transitions and
the latest condition, rather than accumulating every intervening change.

Use these commands to discover problems with the alert mechanism itself:

```sh
systemctl --user status clashlens-alert.timer clashlens-alert.service
journalctl --user -u clashlens-alert.service --since today
./ops alert-check
```

The journal reports failed delivery or unavailable measurements without printing
HTTP response bodies or exception details. Slow failed probes may delay the next
check; the service times out after four minutes. This local checker cannot notify
Discord while the host, user service manager or network is unavailable, or after
an out-of-band stop of `clashlens.target`. Confirm recovery after those outages.
A real test alert was delivered and Zubair confirmed channel visibility; see
[the delivery evidence](discord-alert-validation.md#owner-requested-live-delivery-test).
Real recovery delivery, channel privacy and reboot behaviour still require a
separately approved rehearsal.
Job/upload stalls, missed Reset publication and capacity budgets remain deferred
under #140; this command adds none of those policies.

## Failed work

Listing and previewing are the default; a retry needs both one exact item and
`--apply`:

```sh
./ops failed-items --limit 20
./ops failed-items --work-id 123
./ops failed-items --work-id 123 --apply
./ops failed-items --upload-hash SHA256
./ops failed-items --upload-hash SHA256 --apply
```

This command starts an ephemeral copy of the pinned Python image with an
init-only database secret, then removes the secret. The long-running worker
never receives retry authority. Repair configuration or authentication before
restarting the collector and retrying archive configuration failures. Archive
checksum or catalogue contradictions return
`archive_integrity_repair_required` and are never requeued automatically.
Failed profile and battle-log observation processing is replayed only through
the existing audited `deploy/replay-request --observation-id ID --reason REASON`
path. League-history, global, and derived processing failures require
investigation. Transport failures are evidence governed by the normal work
policy and are not manually requeued.

## Support recovery

The agreed support entry point is private tickets in the Clash Lens Discord
community, using an existing ticket bot. Public discussion/feedback and the
private operator-alert channel are separate. Provider, permissions, transcript
retention, identifiers and cost remain setup work in
[#138](https://github.com/zzzubair/clashlens/issues/138). Website support links
must work without a Clash Lens login. A ticket is not ownership proof; tokens
must use the existing protected verification flow, never ticket messages.

The existing restricted host wrapper remains supported. Configure its entry
point as `DEPLOY_SCRIPT=/srv/clashlens/ops`; the internal
`support-recovery-exec` command enters the existing private API container and
is unavailable in fixture mode. The root-owned wrapper still validates the
sudo caller and prompts for the current in-game token without echo.

`deploy/support-transfer` and `deploy/replay-request` continue to connect
through their narrow PostgreSQL service roles and do not use `./ops`.

## Data and launch boundary

`./ops down` removes no retained data. PostgreSQL uses a named volume; the
spool and fixture archive use explicit persistent paths. The 16 GiB spool cap
bounds temporary raw-response storage. Database and remote archive retention
remain governed by the product rules and are not made size-bounded by Quadlet.

Fixture lifecycle evidence does not prove launch readiness. Apply the separate
tracking and public-website gates at the top of this document; real traffic,
spending, deployment and deletion still require their specific approvals.
Leave old GCS/B2 buckets untouched.
