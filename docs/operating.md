# Operating Clash Lens on rogue

Use the production checkout as `zubair` for the
[current friends-testing setup](product-status.md#launch-order). The separate
[September 25 preview deployment](../website/README.md#september-25-preview-deployment)
is historical.

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
- During active tracking, successful fetches keep advancing. Compare fetch age,
  spool bytes, object count and filesystem usage with the
  [alert conditions](deployment.md#alert-conditions).
- Queue `failed` is zero, or every existing failure has an investigated cause.
  `oldest_due_seconds` is the age of the oldest overdue job, or `null` if none.
  If the queue grows, repeat the check after a minute: counts and age should
  show work progressing. One snapshot or an empty queue does not prove collection.
- `backup-status` succeeds and its timer has a next run matching the
  [backup schedule](deployment.md#postgresql-backups-and-recovery).
  A historical `failed_count` alone does not prove a current failure.
  A warming-up seven-day window is not full recovery coverage.

After alert deployment, also run:

```sh
podman exec clashlens-python-api python -m clashlens.alerts --probe
systemctl --user status clashlens-alert.timer clashlens-alert.service --no-pager --lines=0
journalctl --user -u clashlens-alert.service --since '10 minutes ago' -n 30 --no-pager
```

The private probe should exit successfully; its
[private-read condition](deployment.md#alert-conditions) explains what it checks.
The alert timer should be active with recent successful runs matching the
[configured schedule](deployment.md#private-discord-alerts).
The alert and backup services run once per timer firing, so `inactive (dead)`
between successful runs is normal. `failed`, missing units, delivery failures or
unavailable measurements need investigation.

## Respond to alerts

Start with the commands below in the same production checkout. Keep incident
times in UTC and share only secret-free errors. Never paste `app.env`, secret
files, full container inspection output or player lists into Discord.
Repairs that restart services, deploy, delete data or change spending need
Zubair's approval. Use the [existing service lifecycle](deployment.md#existing-service-lifecycle)
for an approved stop or restart; do not keep restarting a broken service.

Use the [alert conditions and delivery rules](deployment.md#alert-conditions)
to interpret messages. Confirm both the measurements below and the recovery
message in the private operator channel. `./ops alert-check` can run the check
immediately, but **sends real Discord messages** and saves alert state.
A successful exit means the check and delivery worked, not that all five
conditions are healthy.

### Tracker stopped

Use the [fetch-gap condition](deployment.md#alert-conditions), which accounts
for the Reset pause and a tracker that has never fetched successfully.

**First checks:** `./ops logs collector --since '15 minutes ago' --no-pager`,
then the daily status, fetch-age and queue checks. Look for stopped services,
connection or authentication failures, or a full spool.

**Fix or escalate:** repair the reported cause through an approved change.
Check database and worker logs if their failures block collection. Escalate a
continued gap after 05:00 UTC; Reset does not excuse an indefinite outage.

**Recovered:** successful fetches resume and keep advancing, followed by the
Discord recovery message. Interpret fetch age using the linked Reset rule.

### Disk or spool over 80%

Compare all four measurements with the
[capacity condition](deployment.md#alert-conditions).

**First checks:** repeat the daily metrics, `df` and `./ops queue-status`;
read `./ops logs collector --since '1 hour ago' --no-pager` and
`./ops logs worker --since '1 hour ago' --no-pager`. If database disk use grows,
also run `./ops backup-status` to check for retained, unuploaded WAL.

**Fix or escalate:** repair blocked uploads or processing. If usage keeps rising,
escalate for an approved collection pause or extra space before the disk fills.
Never delete retained raw responses, spool files or unarchived WAL to silence it.
Use [failed-work inspection and approved retries](deployment.md#failed-work).

**Recovered:** all four measurements are within the linked limits and remain
stable or fall, followed by the Discord recovery message.

### Service restart loop

Find the restarting unit and check it against the
[restart condition](deployment.md#alert-conditions):

```sh
journalctl --user --since '1 hour ago' --no-pager \
  MESSAGE_ID=5eb03494b6584870a536b337290809b3
./ops logs
```

**Fix or escalate:** inspect the failing service's preceding error using
`./ops logs collector --since '1 hour ago' --no-pager`, replacing `collector`
with the affected production service.
Repair its reported configuration, resource or dependency failure before an
approved restart. Do not clear journal history.

**Recovered:** the service stays healthy and restart counts return within the
linked limit, followed by the Discord recovery message. Recovery can wait for
old events to leave that window even after the cause is fixed.

### Backup failed or stale

Use the [backup alert condition](deployment.md#alert-conditions) for immediate
failures, the grace period for unavailable checks, journal diagnostics and incident
times. Use the
[backup failure conditions](deployment.md#postgresql-backups-and-recovery)
to interpret `backup-status` errors. WAL is PostgreSQL's change log.

**First checks:** `./ops backup-status`,
`journalctl --user -u clashlens-alert.service --since '30 minutes ago' --no-pager`,
`./ops logs backup --since '8 days ago' --no-pager`, and
`./ops logs postgres --since '1 hour ago' --no-pager`.

**Fix or escalate:** repair the reported timer, storage, network or credential
problem with approval. If a new full backup is needed, obtain approval for
`./ops backup`; it also applies the existing retention deletion policy.
A failed scheduled service must also be recovered, not just hidden by a manual
backup. A changed-release refusal needs an approved release repair; never bypass
the check or edit its saved fingerprint. Follow [backup operations](deployment.md#postgresql-backups-and-recovery).

**Recovered:** `backup-status` succeeds and the Discord recovery arrives.
A backup listing does not prove restore.

### Data reads failing

Use the [private-read condition](deployment.md#alert-conditions). This check
does not cover every player page or army analytics query.

**First checks:** repeat the daily private probe, then
`./ops logs api --since '15 minutes ago' --no-pager` and
`./ops logs postgres --since '15 minutes ago' --no-pager`.

**Fix or escalate:** use those errors to investigate database access, request
signing or an incompatible release. Escalate persistent failures for an approved
repair. Keep checks inside the private container; do not publish its port or keys.

**Recovered:** the same private probe successfully reads stored data and the
Discord recovery arrives. Website `/healthz` alone is insufficient.

### When alerts themselves fail

Use the daily timer status and alert journal commands. Check connectivity and
the secret file's owner and permissions through the
[alert configuration guide](deployment.md#private-discord-alerts), without
displaying its contents. Follow its delivery and outage rules; never delete
alert state to force a recovery. After an outage, confirm services, timers,
measurements and any pending recovery delivery.

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
