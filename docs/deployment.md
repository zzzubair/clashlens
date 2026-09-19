# Fedora operation

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

The private API also requires `CLASHLENS_OFFICIAL_API_PROXY_URL` for its
one-player token verification calls. Configure the fixed-egress proxy at an
HTTP(S) origin reachable from inside the pod. The collector does not use this
setting; its regular collection calls connect directly from the Fedora host.

The archive credentials have separate duties. The collector credential creates
immutable raw responses and may read back the marker or one exact object to
prove a write; the worker credential can only read. Neither runtime credential
may list, overwrite, delete, or broadly browse archive objects.
The database also has separate collector, worker, and API roles. The admin
database URL exists only as a short-lived Podman secret during fixture
bootstrap or while an operator explicitly handles a failed item.

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

The timer starts a full backup Sundays at 03:00 UTC, and 15 minutes after the
timer starts. Missed calendar runs are caught up. PostgreSQL continuously uploads
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
`down` stops the timer and backup service. A failed upload never runs pruning.
`backup-status` exits unsuccessfully for a failed service, inactive timer,
missing/unreachable remote backups, a full backup older than eight days, disabled
archiving, or completed WAL files waiting over ten minutes. No WAL activity during
an idle period is not itself failure. Step 5 of #119 should alert on this command
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
   Repeat affected checks after steps 7 and 10 of #119 change stored data or
   maintenance.

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

The existing raw expiry rule conflicts with complete point-in-time recovery near
expiry: yesterday's restored catalogue can reference a response deleted today.
A seven-day physical-deletion delay plus restore-time allowance would address
that, but would change the agreed 56-day rule. Seven extra days add about 10% to
the modeled 70-day average raw lifetime, roughly €0.40–€2/month using #120's
€4–€20 raw-storage range. No expiry rule changes here. Zubair must resolve this
before production expiry is enabled; step 3 cannot close while it is unresolved.

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

Before closing #122: approve and perform deployment, prove scheduled uploads,
measure worst-case data loss and restore time at the priced size, verify restored
raw references, resolve expiry, restore a genuine seven-day-old point, and check
service restart and host reboot. Keep real collection disabled until backups and
step-5 alerts are proven. This PR does not deploy or close #122.

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

This setup proves service lifecycle against fixtures. Before going live,
separately authorize and prove backups/restoration, production OAuth, one real
Legend day with agreed keys and costs, and alerts. Do not infer launch readiness
from the fixture or reboot checks, and do not touch old GCS/B2 buckets.
