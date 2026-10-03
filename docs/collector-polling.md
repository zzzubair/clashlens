# Continuous player polling

The Python collector runs a continuous, fair loop through tracked Legend I
players. Ninety seconds is the minimum revisit interval, not a batch deadline.

## Agreed discovery and population changes, 2026-09-25

The [launch map](product-status.md) targets complete collection by October 5 for
12,500 live players within a total known pool of 22,157 supplied tags. Add any
new list before tracking starts and import another on October 5. Known-player
eligibility is checked once per week at the Monday transition, automatically
from the October 12 Reset; reuse fresh profiles from live collection.
Repeated inputs and daily clan scans reuse that week's result for known players.
First-time tags need an initial existence/eligibility check.
Normalize and deduplicate within/across lists and all other tag sources. Retain
every confirmed real player regardless of Town Hall or league; regularly collect
only eligible Legend I players. New public tag lookups need no Start tracking
button. Clan discovery adds first-seen and daily member-list checks, but does
not block starting from supplied lists.

Launch lists will use [verified manual operator imports](manual-list-import-validation.md)
through existing database/eligibility functions. No reusable import feature is
required. The legacy `bootstrap-population` command caps input at 20,000,
rejects duplicate lines and refuses a later new import; it remains unchanged.
Automatic discovery requirements remain in [#125](https://github.com/zzzubair/clashlens/issues/125).
Production still rejects the discovery-enabled flag.
[Local development](../README.md#local-development) owns supported fake-player
sizes and trial commands. Add the known pool and weekly check workload to
verification without treating all known tags as live players.
For production key allocation, see
[Clash API keys](operating.md#clash-api-keys). Measure
collection, weekly checks, processing, storage and cost before deciding whether
additional keys or wiring changes are necessary. Weekly scheduling and reuse of
finished checks have not been verified by the manual-import test.

## Weekly eligibility switch

`CLASHLENS_ENABLE_WEEKLY_ELIGIBILITY=false` is the default in `app.env` and the
collector. `ops` passes this setting into the collector. The equivalent direct
collector option is `--enable-weekly-eligibility`. Keep it off for the October 5
manual recheck; enable it afterward for the October 12 automatic pass. Enabling
it also catches up unfinished work for the current week. Applying code or this
document does not authorize enabling it or deploying.

Migration `0037_weekly_eligibility.sql` makes the shared
`clashlens_enqueue_discovery_profiles` function reuse the current week's check
as soon as the migration is applied, independently of the switch. First-time
tags still enqueue immediately. Pending profile checks across older cycles are
reused, and terminal routine attempts do not restart on every repeat sighting.
Weekly work retries transport/server failures at most three times per endpoint.
A failed or unrecognized response never becomes proof of eligibility.

Successful profile fetches completed since Monday's 05:00 UTC Reset prevent
another routine profile request, even while processing is pending or after a
later fetch fails. This reuse applies both before enqueueing and when admitting
already-queued ordinary discovery or weekly work. Unchanged responses also
count when they retain an older observation awaiting processing. Reuse does
not confirm eligibility; only processing recognized tier evidence does that.

A fetch completed before Monday Reset cannot satisfy the new week's check
merely because its processing finishes after Reset. Recognized post-Reset
profiles count even when their relevant content is unchanged, or their
separate season-anchor evidence conflicts. Actively tracked players continue
through normal collection without a second weekly request.
The existing profile processing activates a newly promoted player and makes
that player due for battle collection using the same identity and history.

The weekly scheduler queues at most 30 players at a time, with starts spaced by
two seconds and one check in flight. It uses the existing regular keys and their
configured request/concurrency limits. It pauses during the 04:55 admission
cutoff, unfinished Reset work, or when any live player is more than two minutes
past its normal 90-second due time. A backlog does not trigger a catch-up
burst. Turning the switch off also leaves queued weekly work paused.

Weekly checks reuse previously successful league-history collection; a player
without it gets that endpoint alongside the profile. Ordinary discovery and
weekly work both resume unfinished required league history without refetching
an already-recorded successful or not-found profile. A reused successful profile
also leaves required league history pending, even after processing activates
the player. The target pool has 9,657 inactive players, so a successful pass
needs 9,657 additional profile requests, plus any
missing initial league histories. At the configured admission ceiling that is
at least 5 hours 22 minutes, with collection delays extending the pass. The
10-minute live-refresh requirement remains the capacity acceptance limit.

This week's completed eligibility work cannot be pruned until the next Monday.
Existing cleanup rules retain cancelled/failed work and referenced evidence.
Consequently the 9,657 weekly receipts can add 502,164 rows over 52 weeks before
operator cleanup; no new deletion policy is introduced here.

### Isolated request measurement, September 30

The opt-in test in `python/tests/test_weekly_eligibility_load.py` used the existing
`development/fixtures.py` official-API fixture and a throwaway PostgreSQL 18
database on Rogue. It seeded 22,157 synthetic identities, including 12,500 active
players and 9,657 inactive players with previously collected league history.
The weekly admission clock was accelerated, and each 30-player batch was
collected concurrently through the real collector, local spool and four regular
key limiters. This differs deliberately from the production scheduler's one
check in flight every two seconds.

- The complete pass made **9,657 profile requests**, one per inactive player,
  across 322 batches. No active player received weekly work. The fixture counted
  zero repeated profiles and zero battle-log or league-history requests.
- Regular keys received 2,415, 2,414, 2,414 and 2,414 requests. No interactive key
  was used. The accelerated collection took 324.218 seconds; this is not the
  production pass duration or a full-stack throughput claim.
- `collector_work`, including its indexes, occupied **4,816,896 bytes** after the
  pass, or 4.59 MiB. A linear 52-week projection is about 239 MiB for these work
  records alone. This excludes observations, processing jobs, raw storage and
  future changes in the known population.

Run this measurement from `python/` with `CLASHLENS_RUN_WEEKLY_LOAD=1` and an
isolated `CLASHLENS_TEST_DATABASE_URL`, using the locked Python environment:
`uv run --locked --python 3.12 pytest -q -s tests/test_weekly_eligibility_load.py`.
The existing container check runner's `/tmp` temporary-memory mount is required
on Rogue; its ordinary container filesystem cannot prove free-file capacity and
the collector correctly refuses it.

The focused tests in `python/tests/test_weekly_eligibility*.py` include the
Monday boundary, cross-week pending work, concurrent repeat sightings, ordinary
and unchanged post-Reset evidence, success followed by failure, and ordinary
discovery and weekly restarts with unfinished league history before and after
eligible-profile processing. They also include promotion, delayed pre-Reset
processing, the disabled switch, bounded retries, queue pacing, regular-key
limits and pausing when live collection is late. The database cases require
PostgreSQL execution; skipped cases do not validate these behaviors. The
request measurement above predates the response-reuse and restart corrections.
The measurement does **not** poll the 12,500 active players or run the processing
and archive services. The combined stack's 10-minute live-refresh limit, Reset
timing and complete storage cost still require the population timing trial.

## Queue behavior

The collector reserves 64 of its 256 regular slots for repeat checks when both
repeat and first-battle checks are due, then fills remaining slots with
first-battle checks before more repeats. Each group selects `players.next_due_at`
oldest first, breaking ties by player ID; either can use spare slots.
Admission moves the player to admission time plus 90 seconds, then fetches
the profile and, only when it can have changed, the battle log (next section).

At about 1.2 requests per check, 13,263 active players every 90 seconds would
need about 177 request starts/second, more than the 150 that six regular keys
allow at 25 each. So the keys set the pace: about one check per player every
~108 seconds, if saving keeps up. Before this change every check made two
requests, and the same keys allowed one check every ~177 seconds.

A check fetches the profile, saves it, and only then fetches the battle log if
needed, so a check that needs both saves twice in a row. On 2026-10-02, with
160 checks in flight, production made 76 checks a second: about 2.1 s per
check, almost all of it saving to the spool and database (the Clash API
answered in about 0.13 s), while the keys ran at 19 of their 25 requests a
second. The collector now keeps up to 256 checks in flight with 256 save
threads. Since a check makes its requests one after the other, that is at most
256 requests at once, fewer than the 320 that 160 paired checks held before
this change, and at up to 2 s per check it covers the ~125 checks a second the
keys allow. This is arithmetic from that
measurement; the 256-check figure has not run in production yet. When keys,
slots, the spool or the database cannot keep up, players are checked later
than 90 seconds, still oldest first within each group, and the due queue does
not empty.
Per-key limits stay in force and regular work never uses the interactive key.

### Battle log only when it can have changed

A battle shows on the defender's profile at once: trophies lost or, for a
0-trophy defense such as a 0-star 30% attack, one more defense won in the
`Unbreakable` achievement. The attacker's profile is less reliable. On
2026-10-02, of 12,035 attacks between 05:15 and 06:30 UTC, only 29% were on the
attacker's profile by the check whose battle log first showed them, and half
reached the profile more than 3½ minutes after the log: the profile seems to
catch up only when the attacker stops playing. `attackWins` and
`defenseWins` stayed 0 for about two in three profiles. So a regular check
fetches the profile first, saves it, and then fetches the battle log only when:

- the profile's trophies, `attackWins`, `defenseWins` or `Unbreakable` value
  differ from the last valid profile, or did so on the previous check (the
  follow-up fetch);
- this player's own log showed a new attack less than 10 minutes ago (the
  after-attack fetch). Clashers usually attack several times in a row, half the
  time within 4 minutes of the last, so the next attack shows within about one
  check even while the attacker's profile lags;
- a newly saved battle log of another player shows a battle against this
  player that this player's logs do not have yet (the opponent fetch);
- this check's own profile response is unusable: the request failed, returned
  an error, or had trophies, `attackWins` or `defenseWins` missing, negative or
  not whole numbers. A profile saved by another request at the same time, such
  as a Refresh, does not stand in for it;
- the last successful battle log is at least 15 minutes old, or the collector
  has none for this player since it started (the safety fetch); or
- the player is in the control group (below).

The follow-up fetch exists because the Clash API caches each endpoint for up to
60 seconds, so a profile can show a battle before the battle log does. It only
counts when it starts at least 60 seconds after the fetch that the profile
change triggered, so a quick re-check inside that cache cannot satisfy it. The
opponent fetch likewise only counts when it starts at least 60 seconds after
the other player's log showed the battle, and the after-attack fetch ends with
a fetch that starts at least 10 minutes after the attack. Only a saved
successful battle log whose request started after the change was seen counts.
Valid Legend rows pass the worker's row checks (valid side,
stars, destruction and opponent tag) and have an explicit `battleTimestamp`;
`battleTime`, the battle's length, never stands in for it. A newly seen malformed
row leaves a retry owed, but the same malformed row in a later log does not
prevent that retry from completing. A failed log leaves
the fetch owed for the next check and does not reset the 15-minute safety
clock. A malformed row does the same once, the first time any saved log shows
it, in case a corrected copy follows: whether a regular check, Refresh or Reset
saved that log, one more fetch is owed that starts at least 60 seconds after
that log's request started, so neither a Refresh nor a quick re-check inside
the API cache can use it up. The collector recognises a row it has seen by
the row's own content, not its time: live logs keep rows with no opponent for
days; 448 players' logs had shown one by 2026-10-02. Valid rows in such a log
still count as seen battles and still mark tracked opponents.

The two players' logs time the same battle differently: on 2026-10-02 the
attacker's `battleTimestamp` was 108–211 seconds after the defender's. So the
opponent fetch treats a battle as already seen when this player's log has a
battle against that opponent within 5 minutes of it on the other side (the
opponent's defense matches only this player's attack, and the reverse), or any
battle more than 5 minutes after it: that log was fetched after this battle
ended, and rows are appended in order, so it holds this battle, valid or as a
malformed row with its own retry. It does not compare Legend days: the two
timestamps of one battle can fall on either side of the Reset. Comparing times
alone made each defender look behind every time the attacker's log was fetched
again: replaying the production window, that was 0.34 of the 0.49 extra
requests per check.

A battle that moves no trophies still counts. When player A attacks player B
for 0 stars and 49%, A gains trophies and B loses none, but B's `Unbreakable`
count goes up, so B's next check fetches B's log. If A attacked in the 10
minutes before, A's log shows it too, and the opponent fetch then covers B as
well. A valid battle marks its opponent once, on the first saved log of this
player that shows it, including the first log after a restart and a row that
was malformed in an earlier copy, however old; later logs showing it again do
not. The collector remembers only the battles in each player's last log (about
32 Legend rows): a battle that has left the log can no longer be corrected.
Only players the collector has already checked since starting get an opponent
fetch. Anything still left, such as a tracked player's first 0-star attack
under 10% on an untracked player, waits for the attacker's profile or the
safety fetch: at most about 15–17 minutes plus any queue delay. Leaderboard
trophies come from the profile, which every check still fetches, so they are
unaffected. However late a log is fetched, the worker stores each battle under
the Legend day of its own `battleTimestamp` less 5 minutes, and a battle
reported by both players is stored once. The timestamp alone is not enough: an
attacker's report is stamped when the attack ends, so one stamped in the first
5 minutes after the Reset finished an attack of the day before
([domain rules](domain.md#1-time-and-season-contract)). A daily result already
published for one of the previous 7 Legend days is recalculated by the
[once-per-Reset late-battle check](domain.md#6-ranked-day-and-leaderboard-snapshots),
not when the late battle arrives.

The collector keeps this state in memory: one entry per player it has checked
since it started, at most about 660 bytes (measured with full 32-row battle
logs, 8 bytes per remembered battle), about 8.8 MB for 13,263 players. It
grows only with the number of players checked, and is not saved. After a restart every player's first two checks fetch both
responses again: up to about 26,500 extra battle-log requests, three minutes of
all six keys, but never a missed battle.

The 05:00 UTC Reset, Refresh and interactive first-time collection still fetch
both responses together. A newly found player's first regular battle-log check
can reuse its saved profile within the five-second profile cache window; if that
window expires or the battle-log request fails, it fetches the profile again.

**Control group.** About 5% of players (13 of every 256) fetch both responses on
every check, so production can measure how much later battle details appear for
everyone else. A player is in the group when the first byte of the SHA-256 of
their tag is below 13. In SQL:
`get_byte(sha256(convert_to(normalized_tag, 'UTF8')), 0) < 13`.
Compare battle-to-first-battle-log time, zero-trophy battles and requests per
check between the two groups.

**Production, 2026-10-02.** The first version of these rules ran for 30
minutes and was rolled back. One 0-star 30% defense, battle 6964051, was saved
14 minutes late: the attacker's one log fetch after it came 11 seconds after
the battle (inside the API cache), and both logs waited for the safety fetch.
Retained data shows neither player's trophies changed until 07:14, and both
players' `attackWins` and `defenseWins` were 0 before and after the battle
(archived bodies 842532, 863696, 847729 and 867663); these counts only ever
rise, so they stayed 0. The defender's `Unbreakable` rose from 1767 to 1768
between 06:33 and 07:26; exactly when is not retained. Checks made 1.50
requests, not 1.2, and came every 166 seconds (median), not ~106. The test
`test_zero_trophy_defense_of_battle_6964051_arrives_within_a_check` replays
that battle's real check and battle times, and assumes `Unbreakable` rose as
soon as the battle ended.

**Replay, 2026-10-02.** A scratch replay fed every real battle from production,
both players' timestamps, and when each attacker's profile really showed each
attack, through the real rules, with each player checked at a fixed spacing
and a 60-second API cache. Replaying the rolled-back version over the
production window gave 1.49 requests per check outside the control group
(production 1.47) and attacker-side save times of 139 / 272 / 529 s median /
95% / max (production 140 / 290 / 887 s). With checks every 105 seconds over
05:50–08:20 UTC on 2026-10-01, the busiest hours after Reset (14,148 attacker
and 14,203 defender battle copies):

| | Rolled-back version | Now |
| --- | ---: | ---: |
| Requests per check, all players | 1.47 | 1.22 |
| Requests per check, outside the control group | 1.44 | 1.18 |
| Attacker's copy saved after the battle, median / 95% / max | 109 / 204 / 733 s | 96 / 184 / 263 s |
| Defender's copy saved after the battle, median / 95% / max | 87 / 145 / 672 s | 90 / 146 / 171 s |
| 44 zero-trophy battles, first copy saved, median / 90% / max | 78 / 147 / 672 s | 75 / 115 / 146 s |

At 1.22 requests per check six keys allow 150 ÷ 1.22 ≈ 123 checks a second,
one check per player every 13,263 ÷ 123 ≈ 108 seconds. Without the
after-attack fetch, requests drop to 1.20 per check but the slowest attacker
copy takes 364 seconds. The replay is not the collector: it does not model
saving time, key limits, Refresh or Reset, and it assumes the defender's
profile shows a battle as soon as it ends.

Ordinary transport failures wait for the next pass. Interactive, Reset and
ranking work gets bounded retries. When every failure of a Reset, Refresh or
first-time collection was a timeout, dropped connection, HTTP 429 or HTTP 5xx,
the work waits five seconds and runs again instead of failing. Runs that fail
during a provider-outage pause (below) are not counted. Once the API is
answering again, the work gets three more failed runs, then fails and settles
as missing, so a few failing players cannot hold ordinary collection. The
count is kept in collector memory, so a restart allows three more. Reset
work fetches the profile, then the battle log, then any league history, one
after another. A Reset retry fetches only the responses that have no usable
answer yet, so a profile saved before the player's first battle is kept; a
battle log saved before the profile is fetched again, even after a restart.
Reset work stops retrying at 04:55 UTC, five minutes before its Legend day
ends; Refresh and first-time collection stop 23 hours 55 minutes after the
work was created. HTTP 401 or 403 still fails it at once. Raw responses that
will be kept are published to the local spool before their compact database
handoff; restart recovery finishes either half without creating another
observation or processing job.

Ten timeouts, dropped connections or HTTP 5xx answers in a row, with no other
answer between them, start a provider-outage pause for every key. Requests
wait instead of starting. After 5 seconds one request goes out as a recovery
probe. Each failed probe doubles the wait, up to 60 seconds; any other answer
ends the pause and the waiting requests start under the normal key limits.
A request that cannot start within its 20-second request timeout, while
waiting for a key, a connection or the pause, fails as retryable. Regular
checks do not start during a pause; they wait as paused work. HTTP 429 and
401/403 keep their per-key handling and never start the pause, so an outage
neither pauses nor disables a key. Shutdown releases waiting requests
as retryable failures.

Reset work that fails with no response has no processing job. The worker
checks every 10 seconds, and at start, for failed Reset work of any age without
final evidence, up to 100 rows per check. It skips rows whose saved response
is still waiting to be processed; that response's own job re-checks the row
when it finishes, so stuck reads cannot fill the batch. It records the
missing or failed responses as `failed` evidence, and that player's Reset
publication becomes unavailable instead of waiting forever. A Reset HTTP 429 or 5xx response
counts as failed only after its work fails; while the work is retrying it
stays `partial`. A retried profile proves the Reset only if it was collected
before the player's first battle of the new Legend day, so a later profile is
never used as the exact Reset value. A profile also proves the Reset only
when its battle log was collected at the same time or later, so the log shows
every battle before the profile. A profile or battle log collected after 04:55
UTC the next day is rejected as late. These responses stay saved as evidence
either way.

Refresh and initial collection use the separate interactive key. Refreshes
coalesce while active, have a 30-second cooldown, and never change the regular
due time. Player-token verification and interactive collection use the
[shared key allowance](operating.md#clash-api-keys).

At 04:55 UTC regular admission stops. At 05:00, after admitted work drains, the
collector freezes active membership into one Reset sweep and creates one paired
profile/battle work row per member. Regular work stays blocked until all Reset
work is terminal; unfinished older Reset work also blocks the next boundary.
Regular checks paused by a full spool or a provider-outage pause do not count
as admitted work, so they never delay the sweep; once admission closes they
wait for their next pass.
A Reset outage therefore holds ordinary collection while the provider-outage
pause lasts, plus at most three more failed runs of each Reset work row.

## Spool, archive and rate enforcement

Before each request the collector reserves its possible 4 MiB body and one spool
object. It writes private temporary bytes, hashes and syncs them, then atomically
publishes the hash-named file. Collection pauses when the spool cannot reserve
capacity and resumes when cleanup frees it. The worker's readiness only needs
the spool to be readable, so it keeps processing saved responses while the
spool is full; that processing is what lets cleanup free space. A failed disk
read makes the job wait and retry without spending an attempt.

The background uploader creates immutable archive objects with up to 32 uploads
at once. Those uploads share a limit of four database calls at once for claiming,
archive configuration checks, renewing upload ownership, and recording completion
or failure. The limit reduces competition with player collection for database
connections. Archive writes run outside it.
See [migration 0042](../deploy/migrations/0042_upload_claim_order.sql) for the
ordered upload lookup and earlier automatic cleanup of obsolete database row
versions.

A local spool file is deletable only after its processing and upload both
succeed. Identical bytes share one spool/archive object. The fields listed in
`response_fields.py` decide whether an ordinary response changed. When those
fields match the retained response, changes to ignored fields need no new
observation, processing job, archive upload or spool file (about 97% of
responses in October 2026). The collector remembers, in memory, the used-field
fingerprint it last committed for each player and endpoint. An ordinary response
with no work row whose fingerprint matches is a known-unchanged sighting: the
collector records it in the database without saving it, so its bytes never
reach the disk. That check holds no lock, so no other response waits behind
it, and it runs only while no other response sharing its lock is being saved.
Every other response (the first per player and endpoint since the collector
started, a changed one, a reset or work-bound one) is saved to the spool before
its own database work. So is a known-unchanged one when the database does not
accept it as unchanged, or when that one-attempt check fails, times out or is
cancelled. Many players can share one body, such as the same not-found profile,
so the check does not wait while another response updates that body's records,
or while the worker holds that player or their last saved response; it saves
the response instead. A shared body already sighted within the last 10 minutes
keeps its earlier latest sighting time, which only orders spool cleanup and
starts the archive retention clock, so its deletion can come up to 10 minutes
early. A body already marked for deletion is never recorded this way; it is
saved again. No response waits on the
database while holding the shared lock: the lock covers only the spool write,
so a later response is saved before it waits for an earlier one's database
commit. Saved responses for the same lock
still commit in the order they were saved. Restart recovery first finishes any
saved response the database already shows as committed, then replays the rest
in the order they were received, so a later response never hides an earlier
change and none is counted twice. A recorded unchanged sighting leaves the
saved response's commit record alone. A changed or unknown response is therefore never lost to a
database wait. The trade-off: a hard crash before a known-unchanged
sighting commits loses that sighting, meaning its seen time, its poll count, and
the later sighting time and archive retirement deadline it would have given the
kept response. No raw response,
observation, job, battle or archive object is lost, and the next poll records
the sighting again about 90 seconds later. The poll count (`request_count`) is
also approximate: if an unchanged sighting commits but the confirmation is lost,
the collector saves and records that response again, counting one poll twice.
This is an accepted trade-off. The profile's official season rank
(`legendStatistics.currentSeason.rank`) is ignored: it moves whenever other
players battle and nothing reads it, so a rank-only change counts as unchanged.
A changed response is still stored in full, including the rank. Fingerprints
saved before this rule include the rank, so after deploying it each player who
carries a rank counts as changed once on their next check (about 4,300 extra
jobs); other players keep their saved fingerprint. Profile freshness follows the
[player page confirmation rule](domain.md#player-page-freshness).
Reset always stores paired boundary observations, including unchanged responses.

Uploaded-copy cleanup holds one publication barrier and rechecks the bounded
batch in one database transaction. Pending durable handoffs protect their raw
bytes. The spool capacity lock covers file removal and capacity accounting;
database work and directory flushes run outside it. Each removed file's directory
is flushed to disk before the database records local deletion.
The publication barrier prevents the same body from being republished meanwhile.
Missing files still let cleanup finish a deletion interrupted by a crash.

League history is collected initially and after each season-ending Reset. It
is stored in full and parsed separately from profiles and battle logs. Raw
response deadlines and recovery protection belong in
[history-retention.md](history-retention.md#implemented-raw-expiry-and-required-recovery-protection).
The [deployment runbook](deployment.md#raw-response-cleanup) owns cleanup
credentials, scheduling and enablement.

Collection allows six concurrent requests per key. Request-start limits and
shared permission rules belong in [Clash API keys](operating.md#clash-api-keys).
The interactive key is never borrowed for regular work.

## Validation and live-run boundary

`./dev trial` measures per-player gaps, coverage, failures, queue age, database
growth, spool recovery, and memory/swap behavior. Profiles must have a median
gap below 300 seconds and a worst gap below 600 seconds; battle logs, which are
skipped until they can have changed, need only every player revisited with a
worst gap within the 15-minute safety fetch plus one check (1,020 seconds).
It uses loopback fixtures; a real Legend-day run still needs separate
authorization.
