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
Production discovery is on: `CLASHLENS_PLAYER_DISCOVERY_ENABLED` defaults to
`true` in `ops`, and `false` turns it off. Each battle-log opponent or Top-200
player who is not tracked and has not had this week's check is saved as due a
check (`players.eligibility_due_at`, migration 0084): one profile request and
one league-history request. Unlike the scheduled weekly check, it does not
reuse saved league history. List imports and the Monday promotion re-check
save players the same way, so a player named by several sources is due once.
On each pass the collector turns due players into checks, oldest due first,
while fewer than 500 wait; one pass at a time adds checks, so passes together
never pass that limit. A job waits up to one second for a player another job
is updating; after that the whole log or ranking is processed again later.
A player stays due until tracked or until this week brings a recognized
profile or a not-found answer, so a full queue only delays them. Each check
moves the player's next try 5 minutes later, doubling with each check to at
most 6 hours: when a check fails, the player is checked again then without
anything naming them again. Saving a player as due does not depend on a
waiting check, so one that later fails is still tried again. A due player
whose check is still waiting, or whose profile fetched this week is still
being processed, waits for the next try instead of getting another check;
once that profile is processed without a recognized league, the next try
fetches the profile again.
Legend I gains about 2,000 players a
week, so this costs about 570 requests a day, plus about 4,000 once for the
roughly 2,000 Legend I players not yet tracked. Each player found eligible is
then tracked like any other, so revisits slow in proportion to the added
players while the keys set the pace.

The promotion list (`promotion_candidates`, migration 0076) holds Legend II and
Legend III players who can be promoted into Legend I at a Monday Reset: one row
per tag with the tier and trophies last seen and when, and no raw response.
Every processed profile showing Legend II or III adds or refreshes its row; one
showing any other recognized tier removes it. An unchanged answer that confirms
the saved profile moves its row's check time forward. An older answer never overwrites
a newer one or changes the row of a player whose saved profile was checked
later. The migration copies inactive players whose saved profile shows Legend
II or III, as of that profile's latest check. The lab's list is added once with
the collector database role, from a CSV read on standard input with the header
`tag,league_tier_id,trophies,checked_at` and each tag written as `#TAG`:

```sh
podman exec -i clashlens-collector python -m clashlens.cli \
  load-promotion-candidates --database-url-file /run/secrets/database-url < candidates.csv
```

It prints how many lines it read and how many rows it added or updated. Tracked
players and players whose saved profile was checked later than the line are
left out, a tag already listed keeps its newer check, and one invalid line
refuses the whole file. It also prints how many lines it read, duplicate tags,
players by tier, players left out as tracked or with a newer saved profile,
rows kept because the list already had a newer check, and the list's Legend II
and Legend III totals afterwards. About 130 bytes a row with its indexes: the lab's
October 2026 list of 250,680 players is about 33 MB, growing only with newly
seen Legend II and III players.

Each Monday, after the Reset sweep (05:03:40-05:10 UTC), settlement (from 05:20)
and the late-battle check (from 05:30), from 06:00 the collector asks for the
profile of every listed Legend II player not checked since 06:00, oldest
check first, then every listed Legend III player the same way (an answer saved
between the Reset and 06:00, such as the weekly check's, can predate the game
applying a promotion, so it does not count), at most
`CLASHLENS_PROMOTION_RECHECK_PER_SECOND` (set in `app.env`) requests a second
on the regular keys (20 by default; 0 turns it off), at most two at once. A
Legend III player promoted to Legend II is refreshed to Legend II, so it is
asked with Legend II the next Monday. Just before each request, after its pacing wait,
it is sent only while that Reset's collection and settlement checks have
finished, no tracked player is more than two minutes late (read at most once a
second), and one key's worth of regular request slots is idle; otherwise the
player stays due and the re-check resumes when collection catches up. A
request admitted just before a key wait or an API outage can still start
late, so at most two promotion requests ever start together. These answers
are not saved, so a player who stayed put costs one request and no storage. A
profile showing Legend I saves the player as due the ordinary discovery check
above, which saves the profile, starts tracking and backfills from the first
battle log. Only a saved answer newer than the re-check's counts, so an
earlier lower-league answer this week, such as one read in the minutes before
the game applied the promotion, neither settles the player nor stands in for
the check. The row is marked checked once the player is tracked or saved as
due; a player another job holds is asked again later. Any other answer
refreshes or removes the list row. A failed request, or an answer that cannot
be read or shows an uncertain tier, leaves the player due; it is asked again
once the rest of its tier has been asked, at most once a minute. Legend III
players are asked only once no Legend II player is due, so a Legend II player
left due is asked again before any Legend III player. Each stretch
of work ends with one `promotion_recheck` line in the collector log counting
asked, promoted, listed, removed, failed and queued players. At the lab's
October 2026 list this is about 59,000 Legend II requests each Monday, plus
two requests for each promoted player, then about 192,000 Legend III requests.
Two in flight at about 120 ms each gives roughly 16 requests a second, so
Legend II takes about an hour and Legend III about three more.

The `population-status` command, run like `load-promotion-candidates`,
prints tracked players split into available (a current profile naming this
Season), waiting to sign up and unavailable (profile not found); for players
first seen as an untracked battle opponent or first given a discovery, lookup
or refresh check, whichever came first, in the last 7 days and now tracked,
the median, 95th-percentile and longest time from then to their first battle
log, so time waiting for queue space counts (first logs before migration 0084 are not recorded); untracked
players, and this week's answers, waiting checks, due retries and players not
yet checked for battle opponents (saved on the player when a battle log first
names them untracked, so pruning sightings does not move them) and other
known players separately; this
week's weekly and discovery checks by outcome, with how many of their players
are now tracked; the due players; untracked players whose saved profile still
shows no recognized league, which drops only as recognized profiles are
saved; and the promotion list by tier, with how many were asked since 06:00 on Monday and known
Legend II and III players missing from it. `--repair` first saves as due
every untracked player that never had a recognized or not-found answer, and
lists every untracked player whose latest recognized profile shows Legend II
or III; it fetches nothing itself and deletes nothing. On 8 October 2026 that
was 2,697 players (504 whose checks failed on 1-4 October and were never
answered, one never queued, and 2,192 whose saved profile predates recognizing
Legend II and III and lower leagues), about 5,400 requests, and about 9,500
known Legend II and III players missing from the list.

[Local development](../README.md#local-development) owns supported fake-player
sizes and trial commands. Add the known pool and weekly check workload to
verification without treating all known tags as live players.
For production key allocation, see
[Clash API keys](operating.md#clash-api-keys). Measure
collection, weekly checks, processing, storage and cost before deciding whether
additional keys or wiring changes are necessary. Weekly scheduling and reuse of
finished checks have not been verified by the manual-import test.

## Weekly eligibility switch

`ops` passes `CLASHLENS_ENABLE_WEEKLY_ELIGIBILITY=true` to the collector, for
the agreed automatic pass from the October 12 Reset, unless `app.env` sets it
to `false`. The collector on its own, without `ops`, keeps it off; the
equivalent direct collector option is `--enable-weekly-eligibility`. Enabling
it also catches up unfinished work for the current week. Applying code or this
document does not authorize deploying.

Migration `0037_weekly_eligibility.sql` makes the shared
`clashlens_enqueue_discovery_profiles` function reuse the current week's check
as soon as the migration is applied, independently of the switch. First-time
tags still enqueue immediately. Pending profile checks across older cycles are
reused, and terminal routine attempts do not restart on every repeat sighting.
Migration `0090_weekly_eligibility_next_players.sql` keeps that selection but
saves on each inactive player the week the weekly search finished with them, so
a search reads from an index only players not yet finished that week, oldest
finished week first and then by ID, and a finished week reads none. The search
it replaced tested every player already checked that week: on 9 October 2026,
with 9,510 of 13,215 checked, it took up to 58.6 seconds.

Weekly and ordinary discovery work make one request per endpoint per run. A
temporary failure (a transport failure, a rate limit or a server error) puts the
same work row back after five seconds, refetching only endpoints without a
successful or not-found answer, up to three more runs while the API answers and
without limit during a provider-outage pause, within 23 hours 55 minutes of
queueing. A rejected key (401 or 403) fails the work at once. A failed or
unrecognized response never becomes proof of eligibility. Each such check,
weekly ones included, leaves the player due, so once it fails the ordinary
discovery checks above try again.

A successful profile fetch completed since Monday's 05:00 UTC Reset prevents
another routine profile request while it awaits processing or once it shows a
recognized league; once processed without one, the next check fetches the
profile again (migration 0084). This applies both before enqueueing and when
admitting already-queued ordinary discovery or weekly work. Unchanged
responses also count when they retain an older observation awaiting
processing. Reuse does
not confirm eligibility; only processing recognized tier evidence does that.
An unchanged answer to discovery work is saved and processed again when the
retained profile was processed without a recognized league tier.

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

The collector reserves a quarter of its regular slots for repeat checks when both
repeat and first-battle checks are due, then fills remaining slots with
first-battle checks before more repeats. Each group selects `players.next_due_at`
oldest first, breaking ties by player ID; either can use spare slots.
Admission moves the player to admission time plus 90 seconds, then fetches
the profile and, only when it can have changed, the battle log (next section).
A player who has finished the Legend day is then checked every 8 minutes
instead ([Finished for the day](#finished-for-the-day)).

At about 1.2 requests per check, 13,263 active players every 90 seconds would
need about 177 request starts/second, more than the 150 that six regular keys
allow at 25 each. So the keys set the pace: about one check per player every
~108 seconds, if saving keeps up. Before this change every check made two
requests, and the same keys allowed one check every ~177 seconds.

A check fetches the profile, saves it, and only then fetches the battle log if
needed, so it holds at most one request at a time. The collector keeps two
seconds of its regular keys' request starts in flight as checks, at least 256
and at most 384: 384 with nine keys at 28 a second, which would otherwise be
504. `CLASHLENS_REGULAR_PARALLELISM` in `app.env` (or `--regular-parallelism`,
1 to 384) overrides it. The collector has a save thread for each slot, and
request threads (6 per key including the interactive key, so 60 with nine
regular keys) share what the save threads leave of 448. The
[uploads process](#spool-archive-and-rate-enforcement) in the same container
counts as 32 more, and 64 of the container's 544 processes and threads stay
spare for database and other threads. Before stopping anything, `./ops up`
refuses settings whose save, request and upload threads plus those 64 exceed
`CLASHLENS_COLLECTOR_PIDS`.
Its database connections stay at 32 whatever the slot
count, so more slots cannot use more of PostgreSQL's 100 connections
(production used 52 on 2026-10-03).

On 2026-10-03 production held 250 checks in flight at 66 checks a second,
about 3.8 s each, with keys at 18 of their 28 requests a second. The Clash API
answered in about 0.15 s, and of the collector's 32 database connections only
about 10 were in a transaction at once. The checks were not saving: about 220
were waiting for a key. Each key waited out its gap between starts and then
started the next gap from when it actually woke, and the collector woke
paced starts late (health checks answered in a median 6 ms, 38 ms at the 90th
percentile). Every late wake added to the gap, so 28 a second became 18. Now a
key that falls behind by under a second keeps its schedule and its next starts
make up the time, while no second, measured from any instant, holds more starts than
the rate. A replay of the pacing with that lateness made 20.5 starts a second
per key before this change and 27.7 after; it is a model, not a production
measurement. When keys, slots, the spool or the database cannot keep up,
players are checked later than 90 seconds, still oldest first within each
group, and the due queue does not empty.
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
  has none for this player since it started (the safety fetch; skipped once the
  player has [finished the day](#finished-for-the-day)); or
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
the row's own content, not its time. A "no opponent, no battle" row (see
[domain.md](domain.md)) is skipped: it is not a battle, owes no fetch and does
not count as malformed. Live logs keep such rows for days; 448 players' logs
had shown one by 2026-10-02.

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
since it started, at most about 720 bytes (660 measured with full 32-row
battle logs, 8 bytes per remembered battle, plus an estimated 56 for the
finished Legend day), about 9.5 MB for 13,263 players. It
grows only with the number of players checked, and is not saved. After a restart every player's first two checks fetch both
responses again: up to about 26,500 extra battle-log requests, three minutes of
all six keys, but never a missed battle.

The 05:00 UTC Reset still fetches both responses, the profile first (below),
and Refresh and interactive first-time collection fetch both together. A newly found player's first regular battle-log check
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
after another, the last two from 05:07:20 UTC (below). A Reset retry fetches only the responses that have no usable
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
publication becomes unavailable instead of waiting forever. Failed evidence
still recalculates the ended day, which then ends incomplete. A Reset HTTP 429 or 5xx response
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
profile/battle work row per member, due at 05:03:40. At all 11 Resets from 29
September to 8 October 2026 the ended day's last battle ended by 05:03:38 and
the new day's first started at 05:07:20 or later, and no profile showed the
automatic defense loss before 05:07:38. So from 05:03:40 the sweep fetches only
profiles, each showing the ended day's end before that loss; from 05:07:20 it
fetches each player's battle log, which only has to come after the profile,
and any league history, after any profile still owed, which is then followed
at once by its battle log. A new-day battle cannot reach a profile before about
05:09:50: a defense shows about 2 minutes after it starts, an attack about 4
minutes after its report. Regular work stays blocked until all Reset
work is terminal; unfinished older Reset work also blocks the next boundary.
So the Reset pairs may use the regular checks' slots (`CLASHLENS_REGULAR_PARALLELISM`,
300 with six keys) instead of the 32 the other durable work shares, and the
keys set their pace: 13,295 profiles take at least 89 seconds at six keys' 150
starts a second. With 32 slots, each fetching a profile and then a battle log,
the 8 October 2026 sweep read 23 profiles a second. On 9 October 2026 the
collector, run locally against the fake API with production-sized answers
taking 0.14 seconds each, read 13,295 profiles in 93 seconds with 300 slots
and in 170 with 32, and their battle logs in 142 and 217 seconds after 05:07:20;
the same machine ran the earlier pairs-at-once sweep at 43 players a second,
1.9 times production's pace.
Regular checks paused by a full spool or a provider-outage pause do not count
as admitted work, so they never delay the sweep; once admission closes they
wait for their next pass.
A Reset outage therefore holds ordinary collection while the provider-outage
pause lasts, plus at most three more failed runs of each Reset work row.

### Finished for the day

A Clasher has at most 8 attacks and 8 defenses a Legend day, so after both
their battles and trophies cannot change until the next Reset. Such a player's
regular checks then fetch only the profile, once every 8 minutes, when all
of these hold:

- the last saved battle log shows exactly 8 valid attacks and 8 valid defenses
  on the current Legend day (each battle on the day of its `battleTimestamp`
  less 5 minutes, as the worker stores it), and no malformed row other than
  a "no opponent, no battle" row; 9 of either never counts;
- this check's profile was usable and no battle-log fetch is owed, so the
  profile has not changed since a log that followed its last change;
- the newest battle in the log is at least 15 minutes old. The attacker's
  profile can show the last attack late: from September 29 to October 3,
  2026, trophies changed more than 15 minutes after the 16th battle on 177 of
  40,743 finished days (0.43%), and more than 10 minutes on 506 (1.2%).

The 15-minute safety fetch of the battle log stops for these players.
Players in the control group (about 5%, see above) still fetch the battle
log on every check. An 8-minute wait that would end at 04:53 or later, two
minutes before regular checks stop at 04:55, is not taken: the player keeps
the 90-second cadence until 04:55, so a check that starts a minute or two late
is still admitted and their Live Leaderboard entry does not go stale before
the Reset. If a later profile does change, the usual battle-log fetches and
90-second cadence resume. Eight minutes keeps the player page (stale after 15 minutes)
and the Live Leaderboard (stale after 10 minutes, with an alert on any stale
entry) fresh even when checks start a minute or two late, as they do when the
keys set the pace. The 05:00 Reset sweep, the 05:20 settlement checks and
Refresh are unchanged. The next check time is a database update of
`next_due_at`, so it survives a collector restart; it never makes a check
earlier, and inactive players are left alone. A failed update keeps the normal
cadence. "Undisputed" means the player's own log alone: the collector's
database role cannot read `legend_battles`, so a disagreement between the two
players' copies of a battle (55 of those 40,743 days) does not change the
cadence. Rechecking this player's profile cannot resolve it.

Measured with the 13,263 players active on October 3 against the battles
recorded from September 29 to October 3, 2026, the share of players finished
at each UTC hour and the resulting revisit time for everyone else (six keys
at 150 requests a second, 1.22 requests a check, 1.12 for a finished player
before this change; never below 90 seconds):

| UTC hour | Finished | 13,263 players | +5,000 players |
| --- | ---: | ---: | ---: |
| 05:00–12:00 | 0–0.1% | 108 s | 149 s |
| 16:00 | 1.5% | 107 s | 147 s |
| 20:00 | 8.6% | 101 s | 140 s |
| 23:00 | 19.6% | 92 s | 128 s |
| 02:00 | 38.5% | 90 s | 106 s |
| 04:00 | 64.1% | 90 s | 90 s |

Over a day 11.6% of players are finished on average. Their checks drop from
about 1.4 million requests a day to 0.28 million, saving about 1.1 million of
the 13 million requests six keys allow. Between 73% and 80% of active players
finished each of those four days, almost all late: the hours after Reset, the
busiest, gain nothing. The +5,000 column assumes new players finish like
current ones.

### Season 0 profiles

On 2026-10-03, 1,357 of 13,263 active players had no accepted profile because
the game reported their Legend I profile with `currentLeagueSeasonId` 0. All
were at exactly 5,000 trophies, none had a Legend battle this Season, and their
logs held only other battle types. Each profile request returned the same
unusable answer about every 154 seconds, about 761,000 requests a day for the
group.

A regular check of a player whose last profile from this collector reported
Legend I with Season ID 0, with the same trophies, attackWins, defenseWins and
Unbreakable count as the profile before it, fetches the profile only when that
profile is at least 15 minutes old. This includes a player whose older,
accepted profile is still their current one; it stays current and the Season 0
profile's trophies are never used. The worker's own profile rules decide what
counts as Season 0, so an empty, malformed or non-Legend profile does not wait.
The battle log keeps every rule above, but sees a profile change only at the
15-minute recheck, so a battle can wait up to about 15 minutes; the game keeps
recent battles in the log, so none is lost. A Season 0 profile with changed
counts, or any profile with a valid Season, from any check, Refresh or Reset,
ends the wait at once: that check fetches the battle log, and every check
fetches the profile as usual for the rest of that Legend day, before the
worker even accepts it. The wait starts again only after the next Reset, at a
Season 0 profile with unchanged counts. A failed profile request is no
evidence, so the next check retries it. Reset, settlement and Refresh requests
are unchanged. The wait lives in collector memory and a restarted collector
cannot tell who played, so it checks every Season 0 player as usual until the
next Reset. A restart after Reset therefore costs roughly 0.4 to 0.7 million
extra profile requests that Legend day. `./ops up` leaves the collector running
when its image and configuration are unchanged, so only deploys that change it,
or use `--restart-collector`, pay this.

In the fake-game check model (`tests/test_battle_log_schedule.py`), a day of
checks 91 seconds apart, after a Reset the collector was running through,
fetches a quiet player's profile 96 times instead of 960 and the battle log 96
times, through its 15-minute safety fetch. A defense that changes the profile
is saved at the 15-minute recheck, about 12 minutes later in the measured case
instead of on the next check, and that player's later checks that Legend day
fetch the profile every time. A battle already in a tracked opponent's saved
log is still fetched on the next check. The 1,357 players measured had no
Legend battle this Season, so their profiles stay quiet: at the measured rate
this saves about 631,000 of the group's 761,000 daily profile requests (83%),
and more for any player with accepted history whose newest profile says
Season 0.

### Settlement check, 20 minutes after Reset

The game can apply the previous day's automatic defense loss minutes after
the Reset, so the Reset pair is only provisional
([boundary settlement](domain.md#8-evidence-and-confidence-states)). In the same transaction that freezes the
sweep, the collector schedules one `reset_settlement` work row per frozen
member, due at 05:20 UTC, and links it to that member's boundary settlement
row. Only the sweep's first capture schedules them, so restarts, finished or
failed checks and members joining later add none, and a member leaving keeps
its check. Each check fetches a new profile, saves it, then fetches the battle
log; it never reuses a recent profile or skips the log. It keeps its first
usable profile: a retry fetches only what has no usable answer yet, plus a
battle log whose request started before that profile arrived. Both responses
are always saved with their real request times, even when unchanged, and the
work row keeps pointing at them, so the worker processes them behind newer
responses instead of skipping them. Settlement checks fetch no league history;
a Season-opening Reset schedules a separate refresh for that (below).

The checks run in the 32 ordinary intent slots behind any unfinished Reset
work, with the same retries as Reset work. They never block regular
admission, the next Reset or the 05:30 late-battle check, which does not wait
for their responses' processing: every attempt's processing job, including
one a retry replaced, has a `process-settlement:` key. No request starts 23 hours 55 minutes after
their Reset: the HTTP client checks the cutoff right before each request,
retry or redirect goes out, after any wait for a key or an outage pause, and
a check past it stops without changing its work row. From 04:55 the
collector's scheduling loop fails unfinished checks as `settlement_expired`
without a request, up to 1,000 rows per transaction, until a pass finds none
left, before the next Reset. Responses already saved are still
processed. Nothing reads the pair yet; every published result is unchanged.

Budget at 13,263 members (October 3, 2026): 26,526 extra requests per Reset.
Six regular keys at 25 starts per second take at least 177 seconds, but the
32 slots are the real limit: production's early Reset pass, the same work,
finished in 7m45s and 10m10s on October 1 and 2, but the October 6 pass ran
from 05:20 to 08:18. Discovery and ranking checks go ahead of the pass and of
the season-end league-history refresh, so a player first seen at 05:30 is
checked within seconds, not after the pass. Each finished check keeps a work
row, about 220 bytes plus three index entries: 3-5 MB a day, about 1.8 GB a
year, never deleted. A saved profile and log average 23 KB and 74 KB of raw
bytes, up to 1.3 GB a day before identical bytes are stored once.

## Spool, archive and rate enforcement

Before each request the collector reserves its possible 4 MiB body and one spool
object. It writes private temporary bytes, hashes and syncs them, then atomically
publishes the hash-named file. Collection pauses when the spool cannot reserve
capacity and resumes when cleanup frees it. The worker's readiness only needs
the spool to be readable, so it keeps processing saved responses while the
spool is full; that processing is what lets cleanup free space. A failed disk
read makes the job wait and retry without spending an attempt. A saved copy
that is gone, after a lost disk or a database restored to before spool cleanup
ran, is read back from the archive, checked against its hash and saved again.
While an upload of the bytes is under way or its last write may yet land, a
missing archived copy makes the job wait. Otherwise, once the archive's
identity marker matches, a missing copy spends one of the job's ordinary
attempts as `spool_missing`, and every attempt looks again for the saved bytes,
an upload and an archived copy. Only when the job's last attempt still finds
none does the response fail for good, as `spool_missing`.

A copy saved back this way is admitted while the files on disk stay within the
spool's folder limits plus 32 files and 32 largest bodies, one per concurrent
worker job (`worker.MAX_CONCURRENCY`). Space the collector has reserved for a
response it is still fetching is not on disk yet, and the worker cannot see that
reservation in the collector's process. So repairs may fill the folder to its
limits plus those 32 files and bodies, and then every reservation the collector
already holds, one file and one largest body each, whichever request made it,
can still be saved on top. Repairs happen only during recovery; a repair past the allowance waits as a
full spool, never as missing proof. The collector's own saves still stop at the
limits.

Archive uploads run in their own process. The collector starts it inside its
container with the collector's own settings, and starts it again if it exits or
sends no report for two minutes; the collector itself keeps running. Uploads
used to run inside the collector, on its threads and database connections.
After the 8 October 2026 Reset they fell from about 1,300 a minute to 145–300 a
minute for two hours while the worker loaded the database, and raw responses
waited up to 76.5 minutes for an archive copy.

The uploads process makes up to 16 uploads at once, with its own threads and
four database connections. An upload makes two database calls: claiming it and
recording it done or failed. Its 60-second claim is renewed every 20 seconds in
the background, and before a step only when less than 40 seconds are left.
Every 30 seconds, claims that ran out return to the queue in batches of up to
1,000, found through an index of claimed rows
([migration 0082](../deploy/migrations/0082_upload_lease_expiry_lookup.sql)).
Without it each pass read the whole upload table: 1.1 GB, 405 ms on average and
up to 6.2 s, on 8 October. See [migration 0042](../deploy/migrations/0042_upload_claim_order.sql)
for the ordered upload lookup. When a pending upload's saved copy is gone, the
process checks the location this upload would write to; bytes already there,
from an upload a restored database forgot, complete it without a second write.
A location the archive catalogue marks retired is never read. After a write
that may still land (one that failed in a way that may pass, or whose claim ran
out), and after a first attempt that found no copy there, a missing copy is
checked again on each of the next 30 attempts before the response counts as
never archived. Attempts that write nothing name
that write in their failure detail (`write attempt N may yet land: ...`), so
failures before a write never shorten its wait. A spool read
failure in the uploads process pauses collection, as one in the collector does.
[Upload waits](operating.md#uploads-waiting) shows how to read its step times.

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
or while the worker holds that player; it saves the response instead. Rows the
worker adds that point at the last saved response never make a check wait:
on 2026-10-03 one worker transaction kept such rows for 14 minutes while it
built a Reset publication, and saving waited behind it for 4 minutes. A saved
response's database update waits at most 3 seconds for a lock the worker holds,
such as its player. It then stays saved on disk and the collector moves on,
retrying the update in the background after any earlier saved response for
the same player and request type, while other players' responses never wait
behind it, until it lands or the collector restarts and replays it. Work such as a
Refresh or a Reset check waits for its own saved responses to land instead of
fetching them again, and work whose responses a restart left waiting is not
picked until they land. Starting a Reset sweep also waits at most 3 seconds,
then tries again on a later pass. Restart replay, the update that checks a Clasher who
finished the Legend day less often, and marking work finished wait the same 3
seconds: replay leaves the rest to the background, the Clasher keeps the normal
check cadence, and finishing is retried. A shared body already sighted earlier the same UTC day
keeps its earlier latest sighting time, which only orders spool cleanup and
sets the archive retention deadline, the same for every sighting that day. A
body already marked for deletion is never recorded this way; it is saved
again. No response waits on the
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

League history is collected initially, at each season-ending Reset and again
20 minutes later in a separate `league_history_refresh` work row per frozen
member on the ordinary lane, because the ended Season's official results
appear minutes after the Reset. The
[Season final ranks missing](operating.md#season-final-ranks-missing) runbook
owns the manual recovery command. It is stored in full and parsed separately
from profiles and battle logs. Raw response deadlines and recovery protection
belong in
[history-retention.md](history-retention.md#implemented-raw-expiry-and-required-recovery-protection).
The [deployment runbook](deployment.md#raw-response-cleanup) owns cleanup
credentials, scheduling and enablement.

Collection allows six concurrent requests per key. Through the
[Paris relay](deployment.md#paris-fixed-address-relay) the collector refuses
to start with settings that could hold more than 64 connections at once:
(regular keys + 1) × requests per key. Request-start limits and
shared permission rules belong in [Clash API keys](operating.md#clash-api-keys).
The interactive key is never borrowed for regular work.

## Validation and live-run boundary

`./dev trial` measures per-player gaps, coverage, failures, queue age, database
growth, spool recovery, and memory/swap behavior. Profiles must have a median
gap below 300 seconds and a worst gap below 600 seconds; battle logs, which are
skipped until they can have changed, need only every player revisited with a
worst gap within the 15-minute safety fetch plus one check (1,020 seconds).
Fixture players have at most 2 Legend battles, so none
[finishes the day](#finished-for-the-day) and skips the safety fetch.
It uses loopback fixtures; a real Legend-day run still needs separate
authorization.
