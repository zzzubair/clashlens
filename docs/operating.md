# Operating Clash Lens on rogue

Use the production checkout as `zubair` for the
[current friends-testing setup](product-status.md#launch-order). The separate
[September 25 preview deployment](../website/README.md#september-25-preview-deployment)
is historical.

On 2026-09-30, the five production containers were healthy and the weekly
backup timer was active. The Discord alert service and timer were **not installed**.
The alert-specific checks below apply after their approved deployment.

## Reading timing measurements

After deploying the timing measurements, read the collector's existing endpoint:

```sh
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8081/metrics \
  | grep '^clashlens_collector_scheduling_delay_total'
```

These are counts since this collector started, split by `ordinary`, `reset`,
and `interactive` lanes. Each admitted check attempt adds one count after local
storage is reserved, before fetching its first response. Time is measured from
the work's due time. Checks paused before admission add nothing; a later retry
that starts adds another sample. The ranges are disjoint: `lt_1` is below 1 s,
`1_to_5` is at least 1 s and below 5 s, then `5_to_30`, `30_to_120`, and
`ge_120` for at least 120 s. Early starts count in `lt_1`. This measures check
admission delay, excluding later waits for an API key or a response.

The existing `worker_health` log records and configured operating snapshot file
include seven additional `stages`: `python_process_observation`,
`python_replay_observation`, `python_build_snapshot`, `python_build_analytics`,
`python_build_army_analytics`, `python_redecode_army`, and
`python_reconcile_ranked_day`. Each sample covers one claimed job attempt,
from after claiming until it returns or raises an error, including retries
under that claim. An attempt claimed again later adds a new sample. Existing
parse/domain stages are nested inside response jobs; do not add their elapsed
times to the job totals.

For each new stage, `elapsed_seconds` and `thread_cpu_seconds` are accumulated
elapsed and thread computation seconds. Divide `thread_cpu_seconds` by
`elapsed_seconds` to see its computation share, for example 2 / 10 = 20%.
The remainder includes database/network waits and time waiting to run, so it is
not a direct measure of database time. Other threads' and PostgreSQL's computation
are excluded. Older stages are not measured this way and show
`thread_cpu_seconds` as `null`, not zero. Compare changes between
two snapshots from the same process for a recent interval, and avoid dividing
by zero. Counts reset on restart; active jobs appear only when they finish.
The change adds 15 collector counters and seven worker stage summaries, plus
fixed timing fields on existing stages. The number of measurements stays fixed;
there are no per-player labels, new queries, files per job, or polling loops.

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
  [alert conditions](alerts.md).
- Queue `failed` is zero, or every existing failure has an investigated cause.
  `oldest_due_seconds` is the age of the oldest overdue job, or `null` if none.
  `overdue` counts jobs past their due time; `scheduled_later` counts jobs
  not due yet, such as recalculations queued a day ahead, which are not a backlog.
  `kinds` splits the overdue jobs into `responses`, `results` (daily results),
  `builds` and `other`, each with its own `overdue` count and `oldest_due_seconds`.
  If the queue grows, repeat the check after a minute: counts and age should
  show work progressing. One snapshot or an empty queue does not prove collection.
- `backup-status` succeeds and its timer has a next run matching the
  [backup schedule](deployment.md#postgresql-backups-and-recovery).
  A historical `failed_count` alone does not prove a current failure.
  A warming-up ten-day window is not full recovery coverage.
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
[private-read condition](alerts.md) explains what it checks.
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
Leaderboard page. Inactive players, unaccepted or missing profiles, players
whose profile was last reported not found, and players still
[waiting for their Season reset](domain.md#live-leaderboard-ordering) are
excluded. Age starts at the later of the processed profile time and its last
unchanged confirmation. Exactly 600 seconds is fresh; more than 600 seconds
contributes to the count.
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

The Clash API allows **10** keys per developer account, and every Clash Lens
key is tied to the Paris relay's address. Clash Lens uses up to 9 regular
collection keys plus one interactive key. Any account key not in the regular
list or used as `interactive-1` may be used for player discovery.

| Slot | File in `CLASHLENS_API_KEY_HOST_DIR` | Used for |
| --- | --- | --- |
| `normal-1` to `normal-4` | `clashlens-normal-1` to `clashlens-normal-4` | Regular collection |
| `extra-1`, `extra-2` | `clashlens-extra-1`, `clashlens-extra-2` | Regular collection |
| `interactive-1` | `clashlens-interactive-1` | Refresh, first-time lookups and player verification |

Each configured slot needs a private mode-600 file containing only its key.

`CLASHLENS_REGULAR_API_KEY_NAMES` in `app.env` lists the regular slots. Its
default is the six regular names above. `./ops` and the collector accept 4 to 9
names, so regular keys plus `interactive-1` never exceed the account's 10. To add
one, create it in the developer portal for the relay address, save it as a
mode-600 file named `clashlens-<name>`, add `<name>` to that list, and deploy
through the approved release procedure. No code change is needed.

`CLASHLENS_REQUESTS_PER_SECOND_PER_KEY` caps how many requests each key may
start in any one second across all callers, the interactive key included. It accepts
whole numbers from 1 to 29 and defaults to 25. `./ops`, the collector command
and its request pacing refuse 30 or more. Six regular keys at the default allow
at most 150 requests per second. A regular check fetches the profile (only every
15 minutes for an unchanged
[Season 0 profile](collector-polling.md#season-0-profiles)) and, only
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
use the database. `./ops down` stops the whole stack. `./ops up` restarts it,
but leaves the collector, PostgreSQL, pod and network running when none of them
changed ([rule](deployment.md#when-up-restarts-the-collector));
`./ops up --restart-collector` restarts them anyway. Any Python change makes a
plain `./ops up` restart them; for a worker-only or API-only change, run `./ops
up --keep-collector` by hand to keep them running (choosing this automatically
is a follow-up).

Use the [alert conditions and delivery rules](alerts.md)
to interpret messages. Confirm both the measurements below and the recovery
message in the private operator channel. `./ops alert-check` can run the check
immediately, but **sends real Discord messages** and saves alert state.
A successful exit means the check and delivery worked, not that all
conditions are healthy. The website-unreachable alert comes from the
[outside check](deployment.md#outside-availability-check) on the Paris relay.

The Live Leaderboard alert and every recovery wait on purpose, as set out in
the [alert conditions](alerts.md). Expect a recovery up to
15 minutes after the fix. A few percent of players a little past ten minutes,
such as after a deploy, is normal near the official API request limit and does
not alert.

### Tracker stopped

Use the [fetch-gap condition](alerts.md), which accounts
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
[capacity condition](alerts.md).

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
[restart condition](alerts.md):

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

Use the [backup alert condition](alerts.md) for immediate
failures, the grace period for unavailable checks, journal diagnostics and incident
times. Use the
[backup failure conditions](deployment.md#postgresql-backups-and-recovery)
to interpret `backup-status` errors. WAL is PostgreSQL's change log.

**First checks:** `./ops backup-status`,
`journalctl --user -u clashlens-alert.service --since '30 minutes ago' --no-pager`,
`./ops logs backup --since '2 days ago' --no-pager`, and
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

Use the [private-read condition](alerts.md). This check
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

Use the [overdue-check and Live Leaderboard conditions](alerts.md).
Check collection and leaderboard freshness separately; a processing backlog
alone does not prove the leaderboard is stale.

**First checks:** `./ops queue-status` and the collector's
`oldest_due_age_seconds`, `oldest_pending_processing_age_seconds` and, for
daily result calculations, `oldest_job_reconcile_ranked_day_age_seconds`, then
`./ops logs collector --since '15 minutes ago' --no-pager` for timeouts and
`./ops logs worker --since '15 minutes ago' --no-pager` for processing errors.
A growing overdue check with many timeouts points at the official API; a
growing processing wait with a healthy collector points at the worker or
PostgreSQL capacity. `oldest_pending_processing_age_seconds` starts at the
saved response's `collector_observations.created_at` and includes pending,
retrying, dependency-waiting and leased jobs, even when the next attempt is
scheduled in the future. Rescheduling a retry does not reset its age; finished
jobs are excluded. A pending job not yet due, such as a day-end recalculation,
is left out until its due time and then counts from it. Jobs without a saved response use their own creation time,
so delayed derived work also contributes to the processing wait.
Leaderboard, analytics and export builds (`build_*` jobs) are left out of it.
Each job type's own oldest age, builds included, is
`oldest_job_<work_type>_age_seconds`, present only while that type has
unfinished jobs.
With the [worker's queue ordering](architecture.md#structured-data-and-evidence),
this age can stay high while the Live Leaderboard is already current. Worker
`job_result` lines with outcome `superseded` identify jobs skipped under those
rules.
A response fetched before the Reset sweep finished that is still waiting, other
than a settlement check's, also holds back that day's [late-battle check](domain.md#6-ranked-day-and-leaderboard-snapshots);
the worker logs a `late_battle_sweep` line with status `complete` once every
player's correction for a Reset has succeeded, `retrying` after a
`player_failed` line, or `failed` if the check itself errored; after either
of those it tries again 10 minutes later.

A waiting upload with `oldest_pending_upload_age_seconds` over 15 minutes
points at the archive or the uploads process: see
[uploads waiting](#uploads-waiting). Until it is archived, a raw response
exists only on the server's disk.

**Fix or escalate:** repair the reported cause through an approved change.
Escalate a wait that keeps growing; restarting services does not shrink it.

**Recovered:** the measurements satisfy the linked alert conditions and each
condition's Discord recovery message arrives.

### Uploads waiting

Archive uploads run in their own process inside the collector container; see
[spool and archive](collector-polling.md#spool-archive-and-rate-enforcement).

**First checks:** read the collector's uploads lines, twice a few minutes apart:

```sh
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8081/metrics \
  | grep -E '^clashlens_(uploader_|collector_(oldest_pending_upload_age_seconds|pending_uploads|archive_health))'
./ops logs collector --since '15 minutes ago' --no-pager | grep -E 'uploader_(health|restart)'
```

`clashlens_uploader_running` is 1 while the process runs, and
`clashlens_uploader_restarts_total` counts restarts since the collector started;
each restart logs an `uploader_restart` line with the exit code.
`clashlens_uploader_report_age_seconds` stays under about 10 seconds while it
reports. `clashlens_uploader_uploads_total` counts finished uploads by outcome:
`uploaded`, `upload_lease_lost`, a failure category such as
`archive_unavailable`, or `archived_copy_found` when a lost saved copy was
already in the archive. For each step, `clashlens_uploader_step_seconds_sum`
divided by `clashlens_uploader_step_seconds_count`, using growth between the
two reads, is its average time: `database_wait` waiting for one of the four
database connections, `claim`, `spool_read`, `archive_write`, `complete`,
`renew`, `release`, and `total` from claim to completion.
`clashlens_uploader_step_p95_upper_ms` bounds the slowest 5%. The
`uploader_health` log line repeats all of these each minute. Counts reset
when the process restarts.

A long `database_wait` or `claim` points at the database, a long
`archive_write` at the archive or the home connection, and a high
`clashlens_collector_api_requests_in_flight` beside it at shared bandwidth.

**Fix or escalate:** repair the reported cause through an approved change.
Never delete saved responses or upload rows to shrink the wait.

**Recovered:** `oldest_pending_upload_age_seconds` is back under two minutes and
the uploads alert's Discord recovery message arrives.

### Armies page empty

The Armies page reads only finished Legend days. Each day passes through
these steps, all run by the worker:

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
   Builds for a Reset older than the newest one wait out 04:30-07:00 UTC; see
   [past-Reset rebuild pacing](domain.md#6-ranked-day-and-leaderboard-snapshots).
3. `build_army_analytics` writes the day's `army_analytics_battle_facts`,
   its `army_analytics_completed_days` marker and its per-lens
   `army_analytics_day_totals` in one transaction, reading 500 frozen
   manifest rows at a time. It locks the Reset's generation row only to
   publish, so `build_snapshot` and `build_analytics` do not wait for it.
   Facts keep each army in `battle_army_decodes`; read them through
   `army_analytics_battle_facts_with_armies`.
4. Within 10 seconds of a day's facts and frozen leaderboard both being saved,
   the worker's maintenance timer counts `army_analytics_rank_band_totals`:
   each Legend day's totals for the 17 rank bands covering ranks 1-10,000 of
   that Season's newest leaderboard. Top N and rank-band views add these up.
   Streak views, trophy ranges, ranges ending before the newest day, and any
   day rebuilt since its totals were counted read facts instead. The worker
   log shows each count as an `army_rank_band_totals` event.

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
(`failure_reasons`). Pairs that failed only because their battle log held
"no opponent, no battle" rows, then counted as gaps, are re-checked too,
as are pairs whose delayed settlement check's log was saved with only such
gaps, oldest Reset first: the saved gap flag and outcome of the Reset's
battle log, and of its delayed settlement check's log, are first re-derived
from their saved rows, and any other reason the pair failed, such as a
rejected profile, still holds. A run that checks pairs but queues nothing stops there;
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
When none remain, it queues up to `--max-jobs` rebuilds of players whose
latest published result for an ended day is still `Live`, or shows no net
although it holds 8 attacks and 8 defenses with neither side disputed, in the
current or previous Season, oldest day first, including days with no ending
Reset check.
Each rebuilds the player's oldest such day not yet requested and every later
saved day of its Season; a day may end `Partial` with no end-of-day total
when the evidence cannot prove one. The key
`reconcile:ended-live:<ranked-day version id>` names the result replaced, so
each such result is queued once and an earlier request never holds back the
player's later days. Players with a rebuild of that day or Season already
queued or running wait for a later run. A request that failed while its
result is still the latest is not queued again but listed, at most
`--max-jobs` of them, in `failed_blockers`. Until October 2026 a failed
ending Reset check never recalculated its day, which left 4,705 ended days
`Live` on 2026-10-03. A day saved `Live` also queues one recalculation of
that day, `reconcile:day-end:<player>:<day>:<rule>`, due two hours after its
Reset at the lowest priority; it does nothing once the day is finished. Before
October 2026 a player switched off during a day, such as the 2,037 moved out
of Legend I when the 2026-10-05 Season started, got no Reset reading, so their
day stayed `Live`; finish those with this command.
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
183,000 facts. Since migration 0058 facts no longer copy their armies: a
copy of the 2026-10-02 day's 177,800 facts took 68.6 MB instead of
319.3 MB, so about 1.9 GB per 28-day season until the season is retired.
Building a day in 500-player batches adds about 76 MB to the worker,
against about 2 GB for a whole day at once. Reading two days took 0.03 s
for Top 100 and 0.93 s for all tracked players.

**Recovered:** `army_analytics_completed_days` lists the backfilled days.
Check the Armies page against the
[current-season coverage and population rules](domain.md#population-filters-and-lenses).

**Repair campaign:** one saved list, per Season, of every result four past
fixes change, held back from republishing until a single coordinated rebuild
finishes. `--campaign preview --season <Season ID>` lists, without
writing anything, every report, decode, player day and Reset publication of
that Season that the 2-star/55% payout, five-minute day move, catalogue v2
decodes and accepted Reset settlements change, plus each affected player's
first saved day of the next Season, to recalculate, and which are excluded
(raw response gone, Season finalized, correction window closed). Later
next-Season days are left to the repair itself. `--campaign register` saves
that list as a dormant campaign that holds nothing and queues nothing;
registering again replaces it with what is still outstanding. `--campaign
activate` refuses until every repair stage is installed; an active campaign
holds its listed Reset publications until each is done.
Every campaign write for a Season is refused from its end plus seven days
(September 2026: from 2026-10-12 05:00 UTC). On 2026-10-03 a September
preview on commit cd6d0f4, before later changes narrowed which days and Resets
a campaign lists, read 590 reports, 102,381 battles needing decodes in 4,653
batches, 2,032 player days and 10 Resets, about 1.7 MB of rows, in 31 seconds.
Re-measure with `--campaign preview` before activation.

**First-tracked players' earlier days:** a player first tracked during a
Season gets Day 1, and any earlier day their own battles reach, from their
first saved battle log. New players get this when that log is saved; for
players tracked before that, `--first-logs preview --season <Season ID>`
counts, without writing anything, the players and days it would recalculate:
each player whose first saved battle log was saved on Day 1, from Day 1, and
each player first tracked later whose battles reach an earlier day, from
that day. `--first-logs queue --season <Season ID>` queues up to `--max-jobs`
of them (default 100), at backfill priority: a worker thread runs them only
when no higher-priority work that thread can claim is due; run it again until
`left_to_queue` is 0. Each player
is queued once for their earliest saved battle log. A Day 1 waits for the
player's accepted Legend I profile naming the Season, which its Season-rule
start of 5,000 needs; those players count in `waiting_for_profile` and are
queued when that profile is saved. A
player first tracked later with no Legend battles on Day 1 gets no Day 1:
they may not have joined the Season until later, and Clash Lens must not
invent a Day 1 for them.

**Day 1's automatic defense loss:** Day 1 now averages its own defenses only,
leaving out the previous Season's last day, and charges a player with at
least as many attacks as defenses for `attacks - defenses` missing defenses;
a Reset reading taken before the loss is read less it
([automatic defense adjustment](domain.md#automatic-defense-adjustment)). A
Day 1 saved before that keeps its old result until recalculated.
`--day-1 preview --season <Season ID>` counts, without writing anything, the
players whose saved Day 1 has 1 to 7 defenses; `--day-1 queue --season
<Season ID>` queues up to `--max-jobs` of them, each recalculating Day 1 and
every later saved day of the Season, at backfill priority: a worker thread
runs them only when no higher-priority work that thread can claim is due, so a
thread that does not process saved responses can still run them while
responses wait. Run it again until `left_to_queue` is 0;
each player is queued once, and players queued by the earlier run that only
averaged Day 1's own defenses are queued once more. On 2026-10-06 the October
2026 Season (`1791176400`) had about 3,100 such players.

**"No opponent, no battle" rows in the automatic defense loss:** the game
counts each such row as a used attack or defense slot when it charges the
automatic defense loss
([automatic defense adjustment](domain.md#automatic-defense-adjustment)). A
day saved before that keeps its old result, and so does the day after, which
pools it. `--zero-result-slots preview --season <Season ID>` counts, without
writing anything, the players with an ended saved day of that Season whose
battle logs hold such a row; `--zero-result-slots queue --season <Season ID>`
queues up to `--max-jobs` of them, each recalculating the player's oldest
such day and every later saved day of the Season, at backfill priority. Run
it again until `left_to_queue` is 0; each player and day is queued once. It
reads the Season's saved battle rows, those above the lowest row a battle of
two days before the Season used, so run it outside 04:00–07:00 UTC.

**Mid-Season sign-up days:** a Reset read before the player signed up for
the Season (a Legend I profile at 5,000 naming Season 0) now starts that day
at 5,000 by the Season rule. Days calculated before that have no start; on
2026-10-08 the October 2026 Season had 48 such ended days on 6 October.
`--sign-up preview|queue --season <Season ID>` counts or queues, at backfill
priority, each such player's day before the sign-up day and every later
saved day; run it outside 04:00–07:00 UTC until `left_to_queue` is 0.

**Full logs that share only other battles:** two full 50-row battle logs
overlap when they share any saved row, not only a Legend battle. Days
calculated before that report `battle_log_overlap_gap` falsely; on
2026-10-07 the October 2026 Season had 911 such ended days on 5 and 6
October, 25 of them otherwise ready to finish.
`--overlap-gap preview --season <Season ID>` counts, without writing
anything, the players whose latest result for an ended day of that Season
reports the gap; `--overlap-gap queue --season <Season ID>` queues up to
`--max-jobs` of them, each recalculating the player's oldest such day and
every later saved day of the Season, at backfill priority. Run it again until
`left_to_queue` is 0. A day still reporting the gap after its recalculation
has a real one. Each day is queued once within about 48 hours of its request
finishing, while finished-job cleanup keeps the request; a later run queues
those days again, which only recalculates them at backfill priority. A
recalculation that failed is kept and not queued again: `failed` counts them
and `failed_blockers` lists at most `--max-jobs` (job, player, day, failure
reason) for investigating.

**Days ending in a trophy mismatch:** a day with no defenses can now take the
automatic loss its next Reset reading, or a later one, shows, and a later
reading, or the ended day's last battles, can settle a Reset reading taken
before the game finished crediting the day. Days calculated before that report
`trophy_equation_mismatch`, or show a day with no defenses uncharged; on
2026-10-08 the October 2026 Season had 123 mismatched ended days on 5 and 6
October, about 27 of them fixable by a later reading or the zero-defense charge
and 23 by the last battles.
`--mismatch preview|queue --season <Season ID>` works as `--overlap-gap` does
for both kinds of day, at backfill priority; run it outside 04:00–07:00 UTC. A
day still reporting the mismatch afterwards has a real one.

**Boards that rank a missing player or miss late battles:** a Reset's Daily
board leaves out a player whose profile check returned 404 (player not found)
after their reading and before the Reset, and adds each player's battles
stamped after their reading; see
[frozen snapshots](domain.md#6-ranked-day-and-leaderboard-snapshots). Boards
frozen before those rules still rank such players and miss those battles: on
2026-10-07 the October 2026 Season's Day 1 board ranked 24 missing players
and Day 2 34, with two of them first and second on Day 2 above ZOOS Yatta,
and Day 2 missed late battles for 290 players. After deploying the rules, run:

```sh
podman exec clashlens-python-worker \
  python -m clashlens.cli republish-current-season --boards preview --season 1791176400
podman exec clashlens-python-worker \
  python -m clashlens.cli republish-current-season --boards queue --season 1791176400
```

`preview` writes nothing and lists each of that Season's Reset boards whose
frozen input still ranks such a player, with how many went missing
(`profile_not_found`), or whose saved entries miss the battles after their
readings, with how many (`late_battles`). `queue` adds one correction for
each, rebuilding its leaderboard and army records; `correction` reads `queued`, or
`already_queued` when one was waiting. The worker starts each correction as
any other: the newest Reset at once, an older one after the 04:30–07:00 UTC
quiet window and 6 hours after its last rebuild. The corrected board then
replaces the published one, which stays saved as superseded. Run `preview`
again later: a board is listed until its rebuild starts, so an empty `boards`
means only that nothing is left to queue. The old board is still served until
the rebuild publishes, and a failed rebuild leaves it served. The run is
finished only when each listed Reset's newest `generation` shows
`snapshot_state` and `army_state` as `published`, using the query under
[Reset publication missing](#reset-publication-missing). A board frozen after
the deploy needs nothing. Only Resets inside the given Season are read, so the
Season before is never touched.

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
once its cause is fixed. Failed processing jobs have no retry command: replay a
failed profile or battle-log job with `deploy/replay-request`, naming the
failed job's own parser, such as `--parser-version supercell-battle-parser-v3`
([failed work](deployment.md#failed-work)). If its saved response truly cannot
be processed, accept the job with the owner's agreement:
`./ops failed-items --accept-job-id ID --reason 'why it cannot be processed'`
previews, and the same with `--apply` records it. The job, its attempts and its
saved response stay; the record keeps who accepted it, when and why, and
`./ops failed-items` shows it as `accepted`. Otherwise escalate.

**Recovered:** 24 hours and 15 minutes after the newest permanent failure. The
alert means a new permanent failure in the last 24 hours; its recovery means no new one for
24 hours, not that anything was repaired. A manual retry of a failed item
clears the alert early; a repeat failure raises a fresh alert.

The separate **failed work waiting for a person** alert stays open while any
failed job or upload is left, however old, and says how many there are. It
recovers 15 minutes after the last one is retried, replayed or accepted, or, for a daily
result calculation, after its replacement from the current-Season republish
finishes. The failed job itself stays failed as a record. A replacement that
found the same result can count as failed again after the 48-hour finished-job
cleanup ([details](alerts.md)).

### Reset publication missing

This alert fires when the website's public Daily leaderboard page does not
show the latest Reset's frozen leaderboard by 05:30 UTC, including when that
page cannot be read then, and when an earlier Reset is still unpublished. The **Reset
behind its 05:30 target** early warning comes first and names the stage:
collection not ended at 05:10, Reset work projected past 05:25 at 05:15, or
inputs not frozen at 05:25.

**First checks:** `./ops queue-status` for Reset work left, `./ops logs worker
--since '2 hours ago' --no-pager`, `./ops logs website` if the alert says the
website check could not read the board, then:

```sh
podman exec --user postgres clashlens-postgres psql -X -d clashlens -c \
  "SELECT boundary_at, generation, snapshot_state, army_state FROM boundary_publication_generations ORDER BY 1, 2"
podman exec --user postgres clashlens-postgres psql -X -d clashlens -c \
  "SELECT * FROM reset_acceptance_records ORDER BY boundary_at DESC LIMIT 2"
```

The second shows, for the latest Resets, when collection ended, when the Reset
readings were processed, when the board's inputs froze, when it was saved as
published and when the website first showed it, with the board's input states
and how many of the Reset's boundaries were settled.

**Fix or escalate:** escalate; repairing a publication needs an approved change.

**Recovered:** the latest board is readable and every Reset since the first
one has published its frozen leaderboard and army results.

### Deploy failed

**First checks:** `./ops status`, then `./ops logs` for the step that failed.
`./ops up` printed it when it stopped. The alert schedule keeps running while
the stack is stopped, so the other alerts a stopped stack raises follow.

**Fix or escalate:** fix the cause and run `./ops up` again, or escalate. To go
back to the earlier code, follow the
[rollback](deployment.md#existing-service-lifecycle) steps; never reverse a
migration.

**Recovered:** 15 minutes after an `./ops up` succeeds.

### Untracked Legend I battlers

**First checks:** `./ops logs worker --since '2 hours ago' --no-pager` for
processing failures, then `./ops queue-status` and
`./ops failed-items --limit 20`: opponent discovery saves each newly seen
player as due a profile check, so a stalled collector or worker leaves them
untracked. `podman exec clashlens-collector python -m clashlens.cli
population-status --database-url-file /run/secrets/database-url` counts due
players, this week's answers and players never answered
([collector polling](collector-polling.md)).

**Fix or escalate:** escalate; a due player is checked again 5 minutes after
its check started, doubling to 6 hours, until answered. `population-status
--repair` saves every never-answered untracked player as due again.

**Recovered:** at most 10 players from the current or previous Legend day's
battles have stayed untracked for over an hour, not counting players whose
saved profile showed a lower tier after their latest such battle, such as
Monday demotions.

### Season final ranks missing

Ended-Season pages show "Not published yet" until Clash of Clans league history
holds that Season's row, which appears minutes after the Season-opening Reset.
The Reset fetches league history again 20 minutes later; when that came too
early, or before this existed, schedule one more request per tracked player:

```sh
podman exec clashlens-collector \
  python -m clashlens.cli refresh-league-history --database-url-file /run/secrets/database-url
```

It reports how many it `scheduled` for the latest ended Season; a second run
schedules 0 while the first run's requests are still waiting, and a fresh set
once they finished. The collector sends them on the ordinary lane within the
normal key budget, about 13,000 requests at October 2026 membership.

### Website unreachable from outside

**First checks:** `ssh fedora`, then `./ops status` and
`curl --max-time 10 http://127.0.0.1:3000/healthz`. If rogue does not answer
SSH, it is powered off or offline.

**Recovered:** the relay's check answers again and posts its recovery.

### When alerts themselves fail

The monitoring warning means a disk, restart-history, Live Leaderboard,
Reset publication or untracked battler check has been unreadable for ten minutes. Run
`journalctl --user -u clashlens-alert.service --since '30 minutes ago' --no-pager`
to see which diagnostic repeats. Its recovery only means all five checks can
be read again; a disk, Live Leaderboard or publication problem they then
report keeps its own alert open.

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
