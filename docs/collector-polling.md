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

Run this measurement explicitly with `CLASHLENS_RUN_WEEKLY_LOAD=1` and an
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

The collector selects `players.next_due_at` oldest first, breaking ties by player
ID. Admission moves the player to admission time plus 90 seconds, then fetches
the profile and, only when it can have changed, the battle log (next section).

At about 1.2 requests per check, 13,263 active players every 90 seconds would
need about 177 request starts/second, more than the 150 that six regular keys
allow at 25 each. So the keys set the pace: about one check per player every
~106 seconds. Before this change every check made two requests, and the same
keys allowed one check every ~177 seconds. On 2026-10-01, with 56 check slots,
production reached only ~125 requests/s: each check took ~0.9 s, of which the
Clash API answered in ~0.12 s and the rest was saving to the spool and database,
including disk flushes. The collector now keeps up to 160 checks in flight with
256 save threads. When keys, slots, the spool or the database cannot keep up,
players are checked later than 90 seconds, still oldest first.
Per-key limits stay in force and regular work never uses the interactive key.
The 160-slot figure comes from a local timing run with simulated disk delay, not
from production.

### Battle log only when it can have changed

A battle almost always changes the Clasher's profile: trophies, or the
`attackWins` and `defenseWins` counts. So a regular check fetches the profile
first, saves it, and then fetches the battle log only when:

- the profile's trophies, `attackWins` or `defenseWins` differ from the last
  valid profile, or did so on the previous check (the follow-up fetch);
- a newly saved battle log of another player shows a new battle against this
  player, and this player's last battle log does not reach that battle's time
  yet (the opponent fetch);
- this check got no valid trophies: the profile request failed, returned an
  error, or had missing or malformed trophies;
- the last successful battle log is at least 15 minutes old, or the collector
  has none for this player since it started (the safety fetch); or
- the player is in the control group (below).

The follow-up fetch exists because the Clash API caches each endpoint for up to
60 seconds, so a profile can show a battle before the battle log does. It only
counts when it starts at least 60 seconds after the fetch that the profile
change triggered, so a quick re-check inside that cache cannot satisfy it. Only
a saved successful battle log whose request started after the change was seen
counts; a failed fetch leaves the fetch owed for the next check.

A battle that moves no trophies still counts. When player A attacks player B
for 0 stars and 49%, A gains trophies and B loses none, but B's `defenseWins`
goes up, so B's next check fetches B's log. The opponent fetch also covers B
when only A's side changed or B's profile has not caught up yet. A "new battle"
is one later than every battle in that player's previous saved log; the first
log the collector sees for a player after starting marks no opponents. Only
players the collector has already checked since starting get an opponent
fetch. Anything still left, such as a tracked player's 0-star attack under 10%
on an untracked player, waits for the safety fetch: about 15–17 minutes plus
any queue delay. Leaderboard trophies come from the profile, which every
check still fetches, so they are unaffected. However late a log is fetched, the
worker files each battle by its own `battleTimestamp`, so it lands in its real
Legend day and order, and a battle reported by both players is stored once.

The collector keeps this state in memory: one entry of about 520 bytes per
player it has checked since it started (measured), about 6.8 MB for 13,263
players. It grows only with the number of players checked, and is not saved.
After a restart every player's first two checks fetch both responses again: up
to about 26,500 extra battle-log requests, three minutes of all six keys, but
never a missed battle.

The 05:00 UTC Reset, Refresh and first-time collection still fetch both
responses together, as does the very first battle log of a newly found player.

**Control group.** About 5% of players (13 of every 256) fetch both responses on
every check, so production can measure how much later battle details appear for
everyone else. A player is in the group when the first byte of the SHA-256 of
their tag is below 13. In SQL:
`get_byte(sha256(convert_to(normalized_tag, 'UTF8')), 0) < 13`.
Compare battle-to-first-battle-log time, zero-trophy battles and requests per
check between the two groups.

**Local measurement, 2026-10-02.** The real collector (run loop, local disk
spool, throwaway PostgreSQL 18.6, 160 check slots, 256 save threads) ran
against the repository's fake Clash API in a separate process, with 13,264
players and six keys at 25 requests/s. The fake API changed trophies and added
a Legend battle for random players at 2.6 a second, about production's rate.
Figures cover the last 10 minutes of a 25-minute run:

| | Before (two requests every check) | After |
| --- | ---: | ---: |
| Requests per check | 2.00 | 1.24 |
| Checks per second | 73.6 | 119 |
| Time between checks of a player, median / 95th percentile / max | 181 / 181 / 182 s | 104 / 143 / 148 s |
| Battles whose log was fetched on the same check | every check fetched both | 1,513 of 1,525 |

The 12 misses were 6 players whose two fake battles between checks cancelled
out exactly; the safety fetch catches those. In that window the battle log was
fetched by the control group on 5.2% of checks, after a profile change on 4.0%,
and by the safety fetch on 14.4%. The safety share is high because every
player's first full checks happened together at startup; spread evenly it is
about 104 ÷ 900 ≈ 12%, so steady state is about 1.2 requests per check. With
20 keys, so that keys were not the limit, both versions saved about 200
responses a second on this machine (12 threads, NVMe): the old code made 99 checks
a second and the new code 187 (at 1.10 requests per check, before any safety
fetch). That is about 170 checks a second at 1.2. These runs had no worker,
uploads or production load, and the fake API answers instantly.

Ordinary transport failures wait for the next pass. Interactive, Reset and
ranking work gets bounded retries. Raw responses are published to the local
spool before their compact database handoff; restart recovery finishes either
half without creating another observation or processing job.

Refresh and initial collection use the separate interactive key. Refreshes
coalesce while active, have a 30-second cooldown, and never change the regular
due time. Player-token verification and interactive collection use the
[shared key allowance](operating.md#clash-api-keys).

At 04:55 UTC regular admission stops. At 05:00, after admitted work drains, the
collector freezes active membership into one Reset sweep and creates one paired
profile/battle work row per member. Regular work stays blocked until all Reset
work is terminal; unfinished older Reset work also blocks the next boundary.

## Spool, archive and rate enforcement

Before each request the collector reserves its possible 4 MiB body and one spool
object. It writes private temporary bytes, hashes and syncs them, then atomically
publishes the hash-named file. Collection pauses when the spool cannot reserve
capacity and resumes when cleanup frees it.

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
observation, processing job, or archive upload. The profile's official season rank
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
responses currently become eligible for retirement 56 days after their season
ends; a body seen in a later season keeps that season's later deadline. The
agreed replacement must also preserve bytes needed by seven-day backup recovery,
including restore time; see [history-retention.md](history-retention.md).
Retirement requires separate operator
credentials and is never part of starting or stopping the stack.

Collection allows six concurrent requests per key. Request-start limits and
shared permission rules belong in [Clash API keys](operating.md#clash-api-keys).
The interactive key is never borrowed for regular work.

## Validation and live-run boundary

`./dev trial` measures per-player gaps, coverage, failures, queue age, database
growth, spool recovery, and memory/swap behavior. It uses loopback fixtures; a
real Legend-day run still needs separate authorization.
