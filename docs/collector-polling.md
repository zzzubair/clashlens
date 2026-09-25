# Continuous player polling

The Python collector runs a continuous, fair loop through tracked Legend I
players. Five minutes is the minimum revisit interval, not a batch deadline.

## Agreed discovery and population changes, 2026-09-25

The [launch map](product-status.md) targets October 5 collection for 12,500 live
players within a total known pool of 22,157 supplied tags. Add the September 30
list and import another on October 6. Known-player eligibility is checked once
per week at the Monday transition; reuse fresh profiles from live collection.
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
Production still rejects the discovery-enabled flag. The current trial's
12,500-player size matches the active target; add the known pool and weekly
check workload to verification without treating all known tags as live players.
Production loads four regular keys and one separate interactive key. Measure
collection, weekly checks, processing, storage and cost before deciding whether
additional keys or wiring changes are necessary. Weekly scheduling and reuse of
finished checks have not been verified by the manual-import test.

The runtime behavior described below is the implementation at `01f746f`.

## Queue behavior

The collector selects `players.next_due_at` oldest first, breaking ties by player
ID. Admission moves the player to admission time plus five minutes, then fetches
the profile and battle log concurrently.

Ordinary transport failures wait for the next pass. Interactive, Reset and
ranking work gets bounded retries. Raw responses are published to the local
spool before their compact database handoff; restart recovery finishes either
half without creating another observation or processing job.

Refresh and initial collection use the separate interactive key. Refreshes
coalesce while active, have a 30-second cooldown, and never change the regular
due time. Player-token verification shares the same 30-start rolling limit: 29
starts are reserved for collection and one for verification.

At 04:55 UTC regular admission stops. At 05:00, after admitted work drains, the
collector freezes active membership into one Reset sweep and creates one paired
profile/battle work row per member. Regular work stays blocked until all Reset
work is terminal; unfinished older Reset work also blocks the next boundary.

## Spool, archive and rate enforcement

Before each request the collector reserves its possible 4 MiB body and one spool
object. It writes private temporary bytes, hashes and syncs them, then atomically
publishes the hash-named file. Collection pauses when the spool cannot reserve
capacity and resumes when cleanup frees it.

One background uploader creates immutable archive objects. A local spool file is
deletable only after its processing and upload both succeed. Identical bytes
share one spool/archive object. The fields listed in `response_fields.py` decide
whether an ordinary response changed; changes to ignored fields update freshness
without a new observation, processing job, or archive upload. A changed response
is stored in full. Reset always stores paired boundary observations, including
unchanged responses.

League history is collected initially and after each season-ending Reset. It
is stored in full and parsed separately from profiles and battle logs. Raw
responses currently become eligible for retirement 56 days after their season
ends; a body seen in a later season keeps that season's later deadline. The
agreed replacement must also preserve bytes needed by seven-day backup recovery,
including restore time; see [history-retention.md](history-retention.md).
Retirement requires separate operator
credentials and is never part of starting or stopping the stack.

Each regular key limits starts to 30/second with six concurrent requests. The
interactive key uses the shared database permit immediately before HTTP. It is
never borrowed for regular work.

## Validation and live-run boundary

`./dev trial` measures per-player gaps, coverage, failures, queue age, database
growth, spool recovery, and memory/swap behavior. It uses loopback fixtures; a
real Legend-day run still needs separate authorization.
