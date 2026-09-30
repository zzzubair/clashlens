# Operating Clash Lens on rogue

Use the production checkout as `zubair`. Preview containers are separate.
On 2026-09-30, the five production containers were healthy and the weekly
backup timer was active. The Discord alert service and timer were **not installed**.
The alert-specific checks below apply after their approved deployment.

## Daily health check

These checks read local status or stored data. They do not request new Clash data.
The ports and spool path below are rogue's production values; if changed during
an approved deployment, use the new values from its configuration record.

```sh
ssh fedora
cd ~/development/ClashLens
./ops status
./ops queue-status
./ops backup-status
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:3000/healthz
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8081/metrics \
  | grep -E '^clashlens_(collector_last_success_age_seconds|spool_bytes|spool_objects) '
df -h /srv/clashlens-data/spool "$(podman volume inspect --format '{{.Mountpoint}}' clashlens-postgres-data)"
systemctl --user list-timers --all 'clashlens-*' --no-pager
```

Healthy looks like this:

- `status` shows stack and pod `active`, and postgres, collector, api, worker
  and website `healthy`. Website `/healthz` returns `{"status":"ok"}`; it does
  not prove player data can be read.
- During active tracking, successful fetches keep advancing. The alert counts
  gaps of 600 seconds after subtracting the Reset pause, 04:55 to 05:00 UTC.
  Spool bytes and object count stay below 80% of their configured caps; both
  data filesystems stay below 80%.
- Queue `failed` is zero, or every existing failure has an investigated cause.
  `oldest_due_seconds` is the age of the oldest overdue job, or `null` if none.
  If the queue grows, repeat the check after a minute: counts and age should
  show work progressing. One snapshot or an empty queue does not prove collection.
- `backup-status` succeeds, reports a completed remote backup no older than
  eight days, and reports no overdue WAL upload. WAL is PostgreSQL's change log.
  Its timer has a next run on Sunday at 03:00 UTC. An idle database need not
  produce new WAL; a historical `failed_count` alone does not prove a current failure.
  A warming-up seven-day window is not full recovery coverage.

After alert deployment, also run:

```sh
podman exec clashlens-python-api python -m clashlens.alerts --probe
systemctl --user status clashlens-alert.timer clashlens-alert.service --no-pager --lines=0
journalctl --user -u clashlens-alert.service --since '10 minutes ago' -n 30 --no-pager
```

The probe silently succeeds only after checking readiness and reading stored
player-search data; an empty result is valid. It prints no keys or player data.
The alert timer should be active with a check every minute and recent successful
runs. The alert and backup services run once per timer firing, so `inactive (dead)`
between successful runs is normal. `failed`, missing units, delivery failures or
unavailable measurements need investigation.

## Respond to alerts

Start with the commands below in the same production checkout. Keep incident
times in UTC and share only secret-free errors. Never paste `app.env`, secret
files, full container inspection output or player lists into Discord.
Repairs that restart services, deploy, delete data or change spending need
Zubair's approval. Use the [existing service lifecycle](deployment.md#existing-service-lifecycle)
for an approved stop or restart; do not keep restarting a broken service.

Once the condition clears, the next scheduled check sends one recovery message.
Confirm both the measurements below and that message in the private operator
channel. No repeated alert does not mean recovery: unchanged incidents stay quiet.
`./ops alert-check` can run the check immediately, but **sends real Discord messages**
and saves alert state. A successful exit means the check and delivery worked,
not that all five conditions are healthy.

### Tracker stopped

**Meaning:** no successful official fetch for 600 seconds, excluding the
04:55 to 05:00 UTC Reset pause. With no success yet, the clock starts at the
first check; unavailable metrics do not stop it.

**First checks:** `./ops logs collector --since '15 minutes ago' --no-pager`,
then the daily status, fetch-age and queue checks. Look for stopped services,
connection or authentication failures, or a full spool.

**Fix or escalate:** repair the reported cause through an approved change.
Check database and worker logs if their failures block collection. Escalate a
continued gap after 05:00 UTC; Reset does not excuse an indefinite outage.

**Recovered:** fetch age drops below 600 seconds and keeps refreshing as
tracking runs, followed by the Discord recovery message.

### Disk or spool over 80%

**Meaning:** spool bytes or object count exceeds 80% of its configured cap,
or the filesystem containing the spool or PostgreSQL data exceeds 80% used.
Exactly 80% does not trigger.

**First checks:** repeat the daily metrics, `df` and `./ops queue-status`;
read `./ops logs collector --since '1 hour ago' --no-pager` and
`./ops logs worker --since '1 hour ago' --no-pager`. If database disk use grows,
also run `./ops backup-status` to check for retained, unuploaded WAL.

**Fix or escalate:** repair blocked uploads or processing. If usage keeps rising,
escalate for an approved collection pause or extra space before the disk fills.
Never delete retained raw responses, spool files or unarchived WAL to silence it.
Use [failed-work inspection and approved retries](deployment.md#failed-work).

**Recovered:** both spool measures and both filesystem measures are at or below
80% and remain stable or fall. Missing measurements cannot clear this alert.

### Service restart loop

**Meaning:** any one `clashlens-*.service` has more than three automatic
restarts in the preceding hour. This also matches preview units such as
`clashlens-preview-api.service`; identify the unit before treating it as a
production failure:

```sh
journalctl --user --user-unit='clashlens-*.service' --since '1 hour ago' \
  --no-pager MESSAGE_ID=5eb03494b6584870a536b337290809b3
./ops logs
```

**Fix or escalate:** inspect the failing service's preceding error using
`./ops logs collector --since '1 hour ago' --no-pager`, replacing `collector`
with the affected production service. For a preview unit, use `journalctl --user
-u clashlens-preview-api.service --since '1 hour ago' --no-pager` with its name.
Repair its reported configuration, resource or dependency failure before an
approved restart. Do not clear journal history.

**Recovered:** the service stays healthy and no service has more than three
automatic restarts in the rolling hour. Recovery can wait for old events to
leave that window even after the cause is fixed.

### Backup failed or stale

**Meaning:** `./ops backup-status` failed. Causes include a failed backup
service, inactive timer, missing or unreachable backups, newest backup older
than eight days, disabled WAL archiving, or completed WAL awaiting upload for
more than ten minutes.

**First checks:** `./ops backup-status`,
`./ops logs backup --since '8 days ago' --no-pager`, and
`./ops logs postgres --since '1 hour ago' --no-pager`.

**Fix or escalate:** repair the reported timer, storage, network or credential
problem with approval. If a new full backup is needed, obtain approval for
`./ops backup`; it also applies the existing retention deletion policy.
A failed scheduled service must also be recovered, not just hidden by a manual
backup. A changed-release refusal needs an approved release repair; never bypass
the check or edit its saved fingerprint. Follow [backup operations](deployment.md#postgresql-backups-and-recovery).

**Recovered:** `backup-status` succeeds with a fresh remote backup, active timer,
no failed backup service and no overdue WAL. A backup listing does not prove restore.

### Data reads failing

**Meaning:** readiness or an actual private player-search read failed, including
when the processes themselves look healthy. This check does not cover every
player page or army analytics query.

**First checks:** repeat the daily private probe, then
`./ops logs api --since '15 minutes ago' --no-pager` and
`./ops logs postgres --since '15 minutes ago' --no-pager`.

**Fix or escalate:** use those errors to investigate database access, request
signing or an incompatible release. Escalate persistent failures for an approved
repair. Keep checks inside the private container; do not publish its port or keys.

**Recovered:** the same private probe successfully reads stored data and the
Discord recovery arrives. Website `/healthz` alone is insufficient.

### When alerts themselves fail

Use the daily timer status and alert journal commands. Delivery errors remain
pending and retry on later runs. Check connectivity and the secret file's owner
and permissions through the [alert configuration guide](deployment.md#private-discord-alerts),
without displaying its contents. Never delete alert state to force a recovery.
The checker cannot notify while rogue, its service manager or network is down.
An intentional `./ops down` suppresses checks until a successful `./ops up`;
stopping the target directly also stops its timer. After an outage, confirm
services, timers, measurements and any pending recovery delivery.

## Restore

Follow [Restore into a separate database](deployment.md#restore-into-a-separate-database)
using the read-only recovery key and an isolated empty volume. The procedure
checks that the chosen time was reached, compares restored records and verifies
the retained raw responses they reference. Do not overwrite production or promote
the scratch database as a troubleshooting step.

Record the chosen recovery time, actual elapsed time, missing data and evidence
checks. Production-size restore timings are being measured separately; the
deployment guide's target is not a measured guarantee. An approved production
recovery needs its own decision, especially after database changes that an older
code version cannot undo.
