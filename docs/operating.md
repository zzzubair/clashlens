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
  | grep -E '^clashlens_(collector_(last_success|oldest_due|oldest_pending_processing)_age_seconds|spool_bytes|spool_objects) '
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
- `clashlens-history-retention.timer` is active with a next run under a
  minute away. It deletes finished processing jobs 48 hours after they finish;
  see [finished-job cleanup failed](#finished-job-cleanup-failed).

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
The alert, backup, raw-response cleanup, ranked-day copy cleanup and finished-job cleanup services run once per timer firing, so `inactive (dead)`
between successful runs is normal. `failed`, missing units, delivery failures or
unavailable measurements need investigation.

## Freshness measurements

The collector's `/metrics` exports these gauges with the
`clashlens_collector_` prefix:

- `check_age_p50_seconds`, `check_age_p95_seconds`, `check_age_max_seconds`:
  successful check ages across active tracked players, excluding players hidden
  by the Live Leaderboard's
  [profile not-found rule](domain.md#live-leaderboard-ordering). Each player's age is
  the time since the profile's `last_success_at`, so it shows how often players
  are checked. Unchanged successful responses advance it; failures do not.
  Battle-log ages do not count: regular checks fetch the battle log only when it
  can have changed
  ([details](collector-polling.md#battle-log-only-when-it-can-have-changed)).
- `check_age_sample_players` and `check_age_missing_players`: players with a
  successful profile check and active players without one, after the same
  not-found exclusion. No samples means the three age gauges are absent, not
  zero. Existing active-player and scheduling gauges still include these
  tracked players so their retry work remains visible.
- `metrics_sample_timestamp_seconds`: database sample time as Unix seconds.
  Database gauges refresh on the first scrape and then at most once every
  30 seconds, with concurrent scrapes sharing the sample. A failed refresh
  returns HTTP 503 rather than silently serving an expired sample.
- Existing `oldest_due_age_seconds`, `last_success_age_seconds` and
  `oldest_pending_processing_age_seconds` remain available. An overdue check
  measures scheduling delay; it is different from time since successful checks.

The private API's existing signed `/operatorz` response adds a `live_leaderboard`
object. Its fields are `age_p50_seconds`, `age_p95_seconds`, `age_max_seconds`,
`older_than_10_minutes`, `entries`, `age_missing_entries`, and
`sample_timestamp_seconds`. These numbers cover every entry, regardless of
page size, using the same membership and confirmation rule as the Live
Leaderboard page. Inactive players, unaccepted or missing profiles, and players
whose profile was last reported not found are excluded. Age starts at the later
of the processed profile time and its last unchanged confirmation. Exactly
600 seconds is fresh; more than 600 seconds contributes to the count.
Empty populations have zero counts and null age values. The API samples on
the first authorized request and at most once every 30 seconds thereafter;
failed refreshes fail the request. Check the sample timestamp when using either
endpoint for deployment checks. Neither readiness endpoint runs these queries.

Both sets use nearest-rank percentiles: p50 is the age at or below which 50%
of the measured population falls, and p95 covers 95%. Future timestamps produce
zero age. These are raw elapsed seconds, including Reset; alert suppression
and the existing 10-minute alert thresholds are unchanged. Samples live only
in process memory and add no stored history or per-player metric labels.
Leaderboard measurements stay in the API because its database role already
has the required reads; the collector receives no additional permissions.

## Clash API keys

Clash Lens keeps to a budget of **8** Clash API keys. The Clash API allows up
to 10 keys per address, and every key is tied to the Paris relay's address.

| Slot | File in `CLASHLENS_API_KEY_HOST_DIR` | Used for |
| --- | --- | --- |
| `normal-1` to `normal-4` | `clashlens-normal-1` to `clashlens-normal-4` | Regular collection |
| `extra-1`, `extra-2` | `clashlens-extra-1`, `clashlens-extra-2` | Regular collection |
| `interactive-1` | `clashlens-interactive-1` | Refresh, first-time lookups and player verification |
| 8th key | not created | Free; add it to regular collection when needed |

Each configured slot needs a private mode-600 file containing only its key.

`CLASHLENS_REGULAR_API_KEY_NAMES` in `app.env` lists the regular slots. Its
default is the six names above. `./ops` accepts 4 to 7 names, so regular keys
plus `interactive-1` never exceed the budget. To use an 8th key, create it in
the developer portal for the relay address, save it as a mode-600 file named
`clashlens-<name>`, add `<name>` to that list, and deploy through the approved
release procedure. No code change is needed.

`CLASHLENS_REQUESTS_PER_SECOND_PER_KEY` caps how many requests each key may
start per second across all callers, the interactive key included. It accepts
whole numbers from 1 to 29 and defaults to 25. `./ops`, the collector command
and its request pacing refuse 30 or more. Six regular keys at the default allow
at most 150 requests per second. A regular check fetches the profile and, only
when trophies, win counts or defenses won changed recently, the player attacked
in the last 10 minutes, a tracked opponent's log shows a battle this player's
log lacks, it has no fresh battle log, or it is in the 5% control group, the
battle log: about 1.2 requests per check
([details](collector-polling.md#battle-log-only-when-it-can-have-changed)).
Checking 13,263 players every 90 seconds would need about 177, so in practice
the keys pace checks to about one every ~108 seconds per player. This is
arithmetic, not measured throughput or a provider-limit guarantee. To read the
real figure, divide the growth of
`clashlens_collector_requests_total{endpoint="battle_log",pool="regular"}` by
that of `{endpoint="profile",pool="regular"}` over a few minutes outside Reset,
summing all outcomes, and add one. For about two check rounds (~4 minutes)
after a collector restart, every check fetches both responses.

The collector stores the interactive key's configured total in the shared
database. Interactive collection, player verification and operator Discord
recovery obtain permission from the same rolling one-second window.
At the default, collection can use
24 starts and verification one, for 25 combined. When the total is at least two,
one start is reserved for verification; at a total of one, either caller can
use the single start. API restarts preserve the collector's configured limit.
Verification sends one request per permission and refuses redirects; collection
obtains a new permission for each redirected request.

Check each key from the collector's measurements:

```sh
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8081/metrics \
  | grep '^clashlens_collector_key_'
```

Each key has four lines, labelled by pool and slot name; key values never appear.
`key_healthy` is 0 after the API refused that key (HTTP 401 or 403); it stays 0
until the collector restarts. `key_paused` is 1 while the key waits after a rate
refusal. `key_rate_limit_per_second` is the configured whole-key cap.
`key_requests_started_total` counts collection requests, including redirected
requests, since the collector started. It excludes player verification and
operator recovery. Read it twice, 60 seconds apart, and divide the difference
by 60 for the collection rate.

## Respond to alerts

Start with the commands below in the same production checkout. Keep incident
times in UTC and share only secret-free errors. Never paste `app.env`, secret
files, full container inspection output or player lists into Discord.
Repairs that restart services, deploy, delete data or change spending need
Zubair's approval. Use the [existing service lifecycle](deployment.md#existing-service-lifecycle)
for an approved stop or restart; do not keep restarting a broken service.
An approved restart of one service is
`systemctl --user restart clashlens-worker.service`, with `worker` replaced by
`api`, `website` or `collector`; only that container restarts. This holds only
after the next `./ops up` re-renders the service files and reloads systemd;
before that, a one-service restart restarts the whole stack. To check it, run
`podman ps --format '{{.Names}} {{.StartedAt}}'`, restart the one service, run
it again, and confirm the PostgreSQL and collector start times are unchanged
(when the restarted service is the collector, only PostgreSQL's). Restarting
`clashlens-postgres.service` also restarts the worker, API and collector, which
use the database. `./ops down` and `./ops up` stop and start the whole stack.

Use the [alert conditions and delivery rules](deployment.md#alert-conditions)
to interpret messages. Confirm both the measurements below and the recovery
message in the private operator channel. `./ops alert-check` can run the check
immediately, but **sends real Discord messages** and saves alert state.
A successful exit means the check and delivery worked, not that all eleven
conditions are healthy. The website-unreachable alert comes from the
[outside check](deployment.md#outside-availability-check) on the Paris relay.

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

### Collection or processing behind

Use the [overdue-check and Live Leaderboard conditions](deployment.md#alert-conditions).
Check collection and leaderboard freshness separately; a processing backlog
alone does not prove the leaderboard is stale.

**First checks:** `./ops queue-status` and the collector's
`oldest_due_age_seconds` and `oldest_pending_processing_age_seconds`, then
`./ops logs collector --since '15 minutes ago' --no-pager` for timeouts and
`./ops logs worker --since '15 minutes ago' --no-pager` for processing errors.
A growing overdue check with many timeouts points at the official API; a
growing processing wait with a healthy collector points at the worker or
PostgreSQL capacity. `oldest_pending_processing_age_seconds` starts at the
saved response's `collector_observations.created_at` and includes pending,
retrying, dependency-waiting and leased jobs, even when the next attempt is
scheduled in the future. Rescheduling a retry does not reset its age; finished
jobs are excluded. Jobs without a saved response use their own creation time,
so delayed derived work also contributes to the processing wait.
With the [worker's queue ordering](architecture.md#structured-data-and-evidence),
this age can stay high while the Live Leaderboard is already current. Worker
`job_result` lines with outcome `superseded` identify jobs skipped under those
rules.
A response fetched before the Reset sweep finished that is still waiting also
holds back that day's [late-battle check](domain.md#6-ranked-day-and-leaderboard-snapshots);
the worker logs a `late_battle_sweep` line with status `complete` once every
player's correction for a Reset has succeeded, `retrying` after a
`player_failed` line, or `failed` if the check itself errored; after either
of those it tries again 10 minutes later.

A waiting upload with `oldest_pending_upload_age_seconds` over an hour points
at the archive: look for upload errors in `./ops logs collector`.

**Fix or escalate:** repair the reported cause through an approved change.
Escalate a wait that keeps growing; restarting services does not shrink it.

**Recovered:** the measurements satisfy the linked alert conditions and each
condition's Discord recovery message arrives.

### Armies page empty

The Armies page reads only finished Legend days. Each day passes through
these steps, all run by the worker without a timer:

1. At Reset the collector saves each tracked player's profile and battle log,
   called the Reset pair. Once both are processed, the pair's latest row in
   `reset_baseline_evidence` becomes `complete` and queues that player's
   end-of-day `reconcile_ranked_day` job; unusable evidence makes it `failed`.
2. The end-of-day job replaces the player's `Live` day in
   `ranked_day_versions` with a finished one. After the Reset publication
   target time, the worker queues `build_snapshot` or `build_army_analytics`
   when that output's inputs are ready for every player in the Reset's
   `boundary_publication_generations` row. A failed pair is recorded as
   unavailable rather than blocking the other players' publication.
3. `build_army_analytics` writes the day's `army_analytics_battle_facts` and
   its `army_analytics_completed_days` marker in one transaction, in batches
   of 500 players.

**First checks**, read-only:

```sql
SELECT boundary_at, state, count(*) FROM reset_baseline_evidence
GROUP BY 1, 2 ORDER BY 1, 2;
SELECT ranked_day_start, state, count(*) FROM ranked_day_versions
GROUP BY 1, 2 ORDER BY 1, 2;
SELECT ranked_day_start FROM army_analytics_completed_days ORDER BY 1;
```

Ended days still `Live`, with Reset pairs `partial`, means step 1 is stuck.
That happened for every Reset before the October 2026 fix: profiles and battle logs are
processed under different parser versions, and each Reset check looked for
both results under one of them.

**Backfill:** run outside 04:45–05:15 UTC and repeat until `evaluated_count`
and `enqueued_count` are both zero, then wait for the queued jobs and run
once more. Zero counts mean nothing is left to queue, not that every repair
finished: a non-empty `failed_blockers` lists failed repairs still holding an
ended day `Live`.

```sh
podman exec clashlens-python-worker \
  python -m clashlens.cli republish-current-season --max-jobs 100
```

Each run re-checks at most `--max-jobs` current-season Reset pairs left
`partial` although both results were processed, one short transaction per
player. It reports how many pairs it checked (`evaluated_count`), the
end-of-day jobs it queued, and how often each reason a pair failed was seen
(`failure_reasons`). A run that checks pairs but queues nothing stops there;
read its `failure_reasons` before running again. The season's opening Reset
is re-checked too, as day 1's starting evidence, but queues no leaderboard,
army or day rebuild for the previous season. A repaired pair rebuilds both
current-season days it touches once they have ended, so a day already
finished without its starting pair is corrected. One job rebuilds those days
and every later saved day in that Season, oldest first in one transaction,
so automatic defense loss and shield duration use the corrected earlier day.
The worker then publishes the days on its own.
A repair job of a complete pair that failed by running out of lease time or
retries while its ended day is still `Live` is queued once more, with the
same inputs, under the key `reconcile:reset-recovery:<failed job id>`, and
counted in `evaluated_count`. The failed job is kept as it was. Days another
rebuild is already working on are skipped. Any other such failure, or a
retry that fails too, is reported in `failed_blockers` (job, player, day,
failure reason) and needs investigating before it is retried by hand.
Only days whose Reset evidence is still in the database can be rebuilt;
older days need the archived raw responses replayed, which this does not do.
Once no partial pairs remain to check, the same command first queues up to
`--max-jobs` rebuilds of current-season days still marked inferred shielded
although the next Reset's trophies differ (two days on 2026-10-03). Each
rebuilds that day and every later saved day, and the day becomes uncertain.
When none remain, it queues up to `--max-jobs` published current-season
player-days that lack the current reconciliation rule version.

Before all of that, each run compares both reports again for every battle
0057 changed, then queues up to `--max-jobs` rebuilds of players whose own
battle report migration 0057 moved to the Legend day before, as listed in
`battle_day_repairs`. Each rebuilds the earlier of the report's two days that
the player has published, then every later saved day in that Season, and the
later of the two if it is in the next Season. A player is done once the
latest published result of each moved report's new day lists the report now
shown for that battle side (the moved one, or a later corrected report that
replaced it) and that of its old day no longer lists the moved one; finished
jobs being deleted after 48 hours does not queue them again. A player with a
rebuild already queued or running that recalculates one of those days waits
for a later run. Each player has one rebuild job at a time, under the key
`reconcile:battle-day:<player id>`. If the latest one failed it is not
retried: it is reported in `failed_blockers` (job, player, day, failure
reason) until it is investigated, and deleting the failed job lets the next
run queue it again. After deploying 0057, run the command
until it queues nothing and the queued jobs have finished.

**Cost:** on 2026-10-02 this re-checks 25,599 pairs for the 2026-10-01 and
2026-10-02 Resets. Each queues one job, about 25,600 jobs in total. Each job
recalculates up to 28 player-days. With only the two ended days saved, that
is about 38,400 day calculations if the pairs split evenly between the two
Resets, about 12% of a day's normal reconciliation work (about 332,000 jobs
a day in early October 2026). Later saved days add calculations; the Season
limit bounds this batch at 716,772 day calculations. One army day is about
183,000 facts. Measured with synthetic
facts built from production armies: 1,787 bytes per fact with its indexes,
so about 330 MB per day and 9.2 GB per 28-day season until the season is
retired. Building a day in 500-player batches adds about 76 MB to the worker,
against about 2 GB for a whole day at once. Reading two days took 0.03 s
for Top 100 and 0.93 s for all tracked players.

**Recovered:** `army_analytics_completed_days` lists the backfilled days.
Check the Armies page against the
[current-season coverage and population rules](domain.md#population-filters-and-lenses).

### Raw-response cleanup failed

Cleanup deletes old raw responses on its own timer; see
[raw-response cleanup](deployment.md#raw-response-cleanup).

**First checks:** `./ops logs archive-retention --since '1 hour ago' --no-pager`.
Each run prints one JSON report; `failed_objects` counts objects whose marking
or deletion failed, and the journal names each one's location and error type.

**Fix or escalate:** repair the reported storage, network, credential or
database problem with approval. A failed marking leaves the response usable;
a failed deletion leaves it marked. The next run retries both. Never delete
them by hand or mark rows in the database to hide the failure. To pause deletion,
set `CLASHLENS_ARCHIVE_RETENTION=preview` and run
`./ops up` with approval.

**Recovered:** the next batch succeeds and its report has `failed_objects=0`.

### Finished-job cleanup failed

Every production `up` installs `clashlens-history-retention.timer`. Five
minutes after `up`, and then 30 seconds after each batch ends, it deletes up
to 1,000 processing jobs that finished more than 48 hours ago, with their
attempts. Pending, leased, waiting, failed and cancelled jobs are never
deleted, and the results the jobs produced stay. About 650,000 jobs finish a
day; the timer can delete roughly 2 million. It connects as
`clashlens_history_retention`, a database role that can only run this
deletion, with a password `up` replaces each time. It does not take the shared
operation lock, so it never delays deployment or backups; `up` and `down`
stop it first. No Discord alert reports a failed batch.

**First checks:** `systemctl --user status clashlens-history-retention.service --no-pager`
and `./ops logs history-retention --since '1 hour ago' --no-pager`. Each batch
prints one JSON report with `eligible_python_processing_jobs` and
`deleted_python_processing_jobs`.

**Fix or escalate:** a lock or statement timeout fails only that batch; the
next one retries. If every batch fails, run `./ops up` with approval to reset
the role's password, then investigate the database error. Never delete jobs
by hand. `./ops history-prune` runs one batch now.

**Recovered:** the service's latest run succeeded, and while older finished
jobs remain, batches report `deleted_python_processing_jobs` above zero.

### Work failed permanently

**First checks:** `./ops failed-items --limit 20` lists failed processing jobs
and uploads with their failure category.

**Fix or escalate:** retry a failed upload with `--upload-hash` and `--apply`
once its cause is fixed. Failed processing jobs have no retry command; see
[failed work](deployment.md#failed-work) and escalate.

**Recovered:** 24 hours after the newest permanent failure. The alert means a
new permanent failure in the last 24 hours; its recovery means no new one for
24 hours, not that anything was repaired. A manual retry of a failed item
clears the alert early; a repeat failure raises a fresh alert.

### Reset publication missing

**First checks:** `./ops logs worker --since '2 hours ago' --no-pager`, then:

```sh
podman exec --user postgres clashlens-postgres psql -X -d clashlens -c \
  "SELECT boundary_at, generation, snapshot_state, army_state FROM boundary_publication_generations ORDER BY 1, 2"
```

**Fix or escalate:** escalate; repairing a publication needs an approved change.

**Recovered:** every Reset since the first one has published its frozen
leaderboard and army results.

### Website unreachable from outside

**First checks:** `ssh fedora`, then `./ops status` and
`curl --max-time 10 http://127.0.0.1:3000/healthz`. If rogue does not answer
SSH, it is powered off or offline.

**Recovered:** the relay's check answers again and posts its recovery.

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
