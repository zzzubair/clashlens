# Alert conditions

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
- **More than three automatic restarts of any one production Clash Lens service
  in the preceding hour**, counted from systemd's structured restart journal
  events. Preview units (`clashlens-preview-*`) are not counted.
  Counter resets and checker restarts do not erase this history. Keep at least
  one hour of user journal history.
- **Any completed `./ops backup-status` run with a non-zero exit alerts
  immediately**. The alert check invokes the command with a 25-second limit;
  [backup operations](deployment.md#postgresql-backups-and-recovery) documents its failure
  conditions and freshness limits. Only timeouts and errors running the command
  get a 15-minute grace period, because a service restart can briefly stall Podman.
  Every failed check makes `alert-check` exit unsuccessfully and logs a fixed
  diagnostic to the alert service journal, including during the grace period.
  Command output and exception details stay private. A completed successful
  run clears the grace clock and any active alert. Delayed alerts and their
  recoveries report when the timeouts or errors first began, or the last
  intentional resume if later.
- **A failed private player-data read**, including when process readiness says
  healthy. The check enters the private API container, checks `/readyz`, and
  signs a `/v1/players/search` read limited to one result. Keys stay inside the
  container and response data is discarded. An empty search result is valid.
- **A player check at least 600 seconds overdue**, from the collector's
  `oldest_due_age_seconds`. This catches collection that slows down without
  stopping, such as a slow official API.
  It is neither raised nor cleared while Reset work is unfinished, measured by
  `clashlens_collector_reset_total > clashlens_collector_reset_terminal`, or while
  those metrics or the overdue age are missing. Checks resume as soon as Reset
  work finishes, with no fixed clock window.
- **A widely or badly stale Live Leaderboard for five minutes**: more than
  **5%** of entries last updated over ten minutes ago, or any one entry over
  **20 minutes** ago, on every check for **300 seconds** (six checks in a
  row; an unavailable check restarts the count but keeps an open alert open).
  Exactly 5% or exactly 20 minutes does not count. From **04:55 to 05:15
  UTC**, while ordinary checks pause and the Reset sweep refreshes every player
  (normally by 05:10), only an entry over 20 minutes counts. From 05:15 both
  limits apply again, even if Reset collection has not finished: until 8 Oct
  2026 unfinished or unknown Reset work paused this check, so a slow Reset hid
  stale live pages. Staleness uses the
  [Live Leaderboard membership and freshness rules](domain.md#live-leaderboard-ordering). The check
  enters the private API container and runs the Live Leaderboard's own query,
  printing only the stale count, the entry count and the oldest entry's age in
  seconds, with the same not-found exclusion.
  [Migration 0043](../deploy/migrations/0043_api_profile_not_found_read.sql) adds
  the durable not-found time to the existing response state, fills it from
  retained responses, and grants the private API read access. It adds no index
  or per-response rows.
  If the query fails, the check fails and preserves
  the existing alert state. The displayed time follows the
  [player page confirmation rule](domain.md#player-page-freshness), including
  across restarts. Migration 0040
  backfills existing confirmations from accepted profiles and successful saved
  responses. It copies a content identifier only when the latest saved response
  is a successful profile already applied to the shown profile; otherwise the
  identifier stays unknown until the next profile is applied.
  It retains one time and one content identifier per player, about
  1 MiB for 13,000 players, with no growing check history. A valid empty
  leaderboard reports `0 0 0`, has no freshness breach, and permits an existing
  freshness alert to recover. The thresholds come from Oct 3, 2026, when
  production used about 130 of its 150 official API requests per second. The
  old any-stale-entry rule alerted at 18:30, 20:04, 20:10 and 21:10 UTC, the
  first three recovering within 3 to 10 minutes. Read-only samples of the
  11,870-entry board every 30 to 60 seconds found none over ten minutes old
  from 20:41 to 20:56 UTC, with the oldest under eight minutes. After the
  21:20 UTC deploy restarted the stack, 100 to 380 entries (0.9% to 3.2%) were
  over ten minutes old in every sample from 21:25 to 21:36, and the oldest
  peaked at 15 minutes. That is normal near the request limit, so 5% and
  20 minutes leave room above it. A collector that stops fetching but still
  reports its measurements passes 20 minutes about 8 minutes after it stops,
  so it alerts about 13 minutes after. One that stops reporting them leaves
  Reset progress unknown, so the fetch-gap alert reports it instead.
  Use the [collection and processing measurements](operating.md#collection-or-processing-behind)
  to distinguish delayed collection from delayed processing.
- **A new permanent failure of a processing job or raw-response upload in
  the last 24 hours**, from the collector's
  `newest_failed_processing_age_seconds` and `newest_failed_upload_age_seconds`.
  Failed jobs stay failed, so this reports new failures. Its recovery means no
  new permanent failure for 24 hours, not that anything was repaired: the failed
  work stays failed until someone fixes it. Failures older than that, such as
  those present at deployment, do not alert. A missing age counts as unknown
  unless the matching `failed_processing` or `failed_uploads` count is zero.
  Seeing a failed upload's bytes again does not restart its 24 hours.
  A manual retry of a failed item clears the alert early; a repeat failure
  raises a fresh alert.
- **Failed work waiting for a person**: any processing job or raw-response
  upload that failed permanently, however long ago, from the collector's
  `failed_processing` and `failed_uploads` counts. The alert says how many of
  each and how long ago the oldest failed, and recovers only when none are
  left, through `./ops failed-items` (retry, replay, or accepting a failed
  processing job that cannot be repaired). A failed job keeps its
  failed state and history, but stops counting once a replay has processed the
  same saved response, shown by that response's successful processing result
  (`observation_processing_outcomes`), which stays after the finished replay
  job is cleaned up. A failed daily result calculation likewise stops counting
  once its replacement from the current-Season republish (which carries
  `recovers_job_id`) has finished, or, after that job is cleaned up, once the
  day has a result saved since the failure. A replacement that found the same
  result saves no new one, so after the 48-hour cleanup its repair can no
  longer be seen and the failure counts again. This also applies to the
  24-hour alert above. A Reset
  record's processed time below counts a finished replay job, and stays
  unknown once that job is cleaned up before the check saw it. On 8 Oct 2026 nine
  jobs from 1–2 Oct were still failed while the 24-hour alert above had long
  recovered.
- **Saved work waiting too long**, as two separate alerts:
  - ordinary processing work waiting too long: daily result calculations
    waiting at least **15 minutes**, from the collector's
    `oldest_job_reconcile_ranked_day_age_seconds`, or any ordinary work
    (saved responses, daily calculations and army re-decoding) waiting at
    least **30 minutes**, from `oldest_pending_processing_age_seconds`. Both
    share one alert, so daily work reaching 30 minutes sends no second
    message, and it recovers only when both are below their limits. A missing
    daily age on an otherwise complete measurement means no daily work is
    waiting. There is no Reset exemption, and after an intentional resume
    work that is already old alerts straight away. Leaderboard, analytics and
    export builds are left out because on Oct 3–4 they routinely ran 24–63
    minutes, which would have kept this alert open and hidden a real backlog.
    The alert names the oldest kind of waiting work and its age, from the
    collector's per-type `oldest_job_<work_type>_age_seconds`;
  - a raw response waiting at least **15 minutes** to be uploaded to the
    archive, from `oldest_pending_upload_age_seconds`; the early warning below
    starts at 5. On 8 Oct 2026 responses saved after the Reset waited up to
    76.5 minutes, and one not yet archived is lost with the server's disk. An upload's wait
    starts when it is first saved, or when retired bytes come back for a
    fresh upload. Retries, including an operator retry of a failed upload,
    keep the original wait.

  Since the Oct 1 worker fixes, the longest ordinary processing wait was 18
  minutes and the longest upload wait under two minutes; Oct 1's stalls of up
  to 3.7 hours would have alerted. Daily calculations normally finish within
  11–13 minutes outside Reset, so the 15-minute limit has little margin and
  measures old work, not proof that the worker stopped. On Oct 4 it would have
  warned at 10:33, 11:07 and 11:46 UTC, during three near-stalls that the
  30-minute limit missed. Expect the 30-minute alert on Reset
  mornings whose processing takes longer than that: on Oct 3 ordinary work
  passed 30 minutes at 05:43 and 06:38, and on Oct 4 it would have warned at
  03:15 instead of 03:46.
- **An early warning before a health-check kill**: the collector or worker
  failed **2 health checks in a row**, from `podman inspect`'s failing
  streak. Podman kills a container at 6, about three minutes, and its own
  status stays `healthy` until then. The alert check reads this before its
  slower checks and again after each one, and sends it as soon as it appears.
  It is its own alert so an open backlog warning never hides it. The same
  warning names PostgreSQL when it has been starting for **5 minutes**, from
  `podman inspect`'s health status and start time: after a crash it replays
  its change log first, and `./ops logs postgres` shows how far it has got.
- **An early warning when work falls behind**, one alert naming every reason
  that holds:
  - the oldest overdue job has waited **10 minutes**, or **45 minutes** between
    05:00 and 07:00 UTC, from `oldest_pending_processing_age_seconds`. The
    normal Reset on 6 Oct 2026 left work overdue for up to 35 minutes;
  - fewer than **100 responses a minute** saved between 05:00 and 06:00 UTC,
    from the collector's `responses_saved_last_minute`, counted from saved
    rows (up to 1,000) in the minute before its sample. Normal Reset hours
    save 420–2,700 a minute;
  - a raw response waiting **5 minutes** to be uploaded to the archive;
  - work of any kind (saved API responses, daily result calculations, board
    and army builds and the rest) waiting for **2 minutes** while none of
    that kind finished in the last 2 minutes, from the collector's
    `waiting_job_<work_type>_age_seconds` and `completed_job_<work_type>_2m`.
    This holds however busy the worker's threads look: threads that keep
    claiming and failing, or keep finding nothing they may claim, finish
    nothing. Every unfinished job that is due counts, including one that is
    running, waiting to be retried or waiting for the archive, from when it
    was saved, so claims, retries and an operator's retry never restart its
    wait. A single job that runs longer than 2 minutes, such as an army
    build, also warns. A past Reset's build the worker holds back between
    04:30 and 07:00 does not count.

  For either warning, a container that cannot be inspected, missing
  measurements, or a sampled minute that starts before 05:00 leave it
  unknown, so an open warning stays open. On 7 Oct 2026 Podman killed the
  collector 13 times and the worker 4 times in 34 minutes with no alert.
- **The latest Reset's frozen leaderboard not readable at 05:30 UTC**, on
  every day including Mondays and Season ends. The check opens the website's
  public Daily leaderboard page at its public address, `CLASHLENS_PUBLIC_ORIGIN`,
  the one visitors use; with none set, the board can never count as readable.
  The page sends a visitor
  on to the board's own address, by Season and day, only once the website has
  read the newest frozen board from the API and accepted it; the check
  follows that address, then enters the private API container to find which
  Reset that board belongs to. So a board saved as published but not shown
  by the website does not count, and neither does a page or check that
  cannot be read at 05:30. Once that Reset's record shows the board was
  readable, a later failed read does not reopen this alert. It also alerts when an earlier Reset has not
  published both its frozen leaderboard and its army results an hour after
  the internal target, five minutes after Reset or ten on Mondays, or has no
  publication record at all 70 minutes after it. It recovers only when the
  latest board is readable and every earlier Reset is published; a fresh Live
  Leaderboard does not clear it. On 8 Oct 2026 the first board published at
  06:26, when the one-hour grace this replaced could alert only from 06:05.
- **An early warning that the Reset is behind its 05:30 board target**, one
  alert naming every stage that holds, until that Reset's board is readable:
  - at **05:12** the Reset sweep has not started, or has not ended for every
    captured member (8 Oct 2026: all 13,251 by 05:09:42, before the sweep
    waited for 05:03:40 to read profiles and 05:07:20 to read battle logs;
    a Season's opening Reset also reads 13,000 league histories after
    05:07:20, at least 89 more seconds at six keys);
  - from **05:15** to 05:25, while the board's inputs are not frozen, the
    Reset-priority work left (`reset_work_remaining`: Reset readings,
    ended-day results and board builds created since the Reset) would not
    finish by 05:25 at the pace it fell over the last ten minutes;
  - at **05:25** the board's inputs are not frozen, leaving under five minutes
    to publish.

  The check enters the private worker container, whose database role writes
  each Reset's record (below), and prints only the Reset, its member count,
  how many members' Reset collection ended, and when the inputs froze and the
  website first showed the board.

  Each Reset's record is one row of `reset_acceptance_records`: members
  captured; when their Reset readings were all collected and all processed,
  counting every response a Reset item saved even if a later request of that
  item failed or a newer response replaced it (the collector keeps replaced
  ones in `collector_work.replaced_observation_ids`, a few ids on a retried
  item); when the first frozen board's inputs froze, when it was saved
  as published and when the website's public page first showed it, to the
  second of that read; that board's input states (Complete, Partial,
  Inconsistent and the rest); and how many of the Reset's boundaries were
  settled, provisional or unresolved when the website first showed it. Each
  value is kept as first seen. The processed time comes only from finished
  jobs: if the finished-job cleanup removed one of a response saved since the
  Reset before the check saw it finish, the time stays empty as unknown, never
  the collection time. A record that has not yet seen its readings processed
  or its board shown keeps being updated every minute for as long as its Reset
  sweep is kept, since a reading can wait days for the archive, and a kept
  sweep the check never recorded while it was down gets its record when the
  check returns. One row a day, under 1 KB: about 0.4 MB a year.
- **More than 10 untracked recent Legend I battlers**: players in a saved
  Legend I battle of the current or previous Legend day who are not tracked
  although their first such battle was saved over an hour ago. Players with a
  saved profile showing a lower tier, observed after the time their latest
  such battle was fought, such as Monday demotions, are left out, because they
  are correctly no longer tracked. Opponent discovery checks a newly seen player within seconds, so a count above 10
  means discovery is stalled or skipping players. On production on Oct 6, 2026
  one of 11,756 Season battlers was untracked. The check enters the private
  worker container, whose database role reads battles, and prints only the
  count; it reads the battles of two Legend days (about 44,000 rows) through
  their existing day lookup, and each battle's saved time through its existing
  battle lookup.
- **Battles may be in the wrong day**: a saved result for the current or
  previous Legend day with a 9th attack or defense, or a battle report
  stamped 05:03:40 to 05:07:00 UTC after either of their Resets. A report
  belongs to the day of its stamp less five minutes because, at all 11
  Resets from 29 September to 8 October 2026, every ended-day battle ended by
  05:03:38 and no new-day battle started before 05:07:20; the Reset sweep
  reads its profiles inside that gap. Either sign means the gap moved. The
  check never moves a battle. It enters the private worker container, prints
  only the count, and reads the battles of three Legend days through their
  existing day lookup, like the untracked battler check, and the two days'
  saved results through their existing day-end lookup.

Missing collector measurements or a failed publication check never clear these
alerts, and each recovers only when its own measurement does.

- **A disk, restart-history, Live Leaderboard, Reset publication, Reset
  progress, untracked battler or battle-day check unreadable for 10 minutes**
  (600 seconds). These seven checks otherwise
  only log a diagnostic and stay unknown, which can hide their own problem
  indefinitely. Each keeps its own first-failure time, so a check that
  becomes readable and later fails again starts a new ten minutes, and one
  check's failure never inherits another's. Several unreadable checks send
  one alert. A readable check that reports a problem is not unreadable; its
  own alert covers it. The
  fetch-gap, backup and private-read alerts already cover their own failed
  checks and are left out, but an unreachable collector also makes spool
  usage unknown, so it can raise this alert alongside the fetch-gap alert.
  It recovers after the usual 15 clear minutes with none of the six
  unreadable, so a different check failing during that time keeps it open.
  Time deliberately stopped does not count towards the ten minutes. This
  alert only works while `alert-check` itself runs, saves its state and
  reaches Discord; it cannot report a dead timer, a crashed checker or a
  broken webhook.
- **A deploy that failed and left Clash Lens stopped**: `./ops up` failed
  after it had stopped services, so it stopped them all, then started the
  alert timer again so this alert is sent and its delivery retried every
  minute. Until 8 Oct 2026 a failed `up` left
  everything stopped with alerts switched off. It stays open, with the other
  alerts a stopped stack raises when `./ops alert-check` runs, until an
  `./ops up` succeeds, and recovers 15 minutes after that.

Messages give the condition, its first observed UTC time and one next step.
There is one alert and one recovery per condition; unchanged checks stay quiet.
A recovery is sent only after **15 minutes** (900 seconds) of checks that all
show the condition clear, and it reports when the condition first cleared.
The 15 minutes count only from when Discord accepted the alert, so an alert
delivered late on retry is never followed straight away by its recovery.
If the problem returns sooner, the open incident continues with no new
message, so several short incidents become one alert and one recovery.
An unavailable measurement or an intentional stop restarts the 15 minutes,
and an intentional stop also restarts the Live Leaderboard's five minutes.
A problem that never alerted never sends a recovery.
Missing disk measurements or restart history never clear an existing alert.
`alerts.json` and `alerts.lock` live under the existing private ops state
folder, `${XDG_STATE_HOME:-$HOME/.local/state}/clashlens`. State is atomically
replaced with mode 600 and contains one record per condition with at most one
pending transition, one first-clear time and one last alert delivery time
each, plus when backup check timeouts or errors and Live Leaderboard staleness
began, when each of at most seven currently unreadable check parts (the Reset
publication check has two: the website page and the private API) first failed, and
at most ten minutes of Reset work left, one sample a minute.
It keeps no growing event history, keys, URLs, player lists or account data.

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
Discord while the host, user service manager or network is unavailable, when
its disk is full, or after an out-of-band stop of `clashlens.target`. The
[outside check](deployment.md#outside-availability-check) covers an unreachable website.
A real test alert was delivered and Zubair confirmed channel visibility; see
[the delivery evidence](discord-alert-validation.md#owner-requested-live-delivery-test).
Real recovery delivery, channel privacy and reboot behaviour still require a
separately approved rehearsal.
Capacity budgets remain deferred under #140.
