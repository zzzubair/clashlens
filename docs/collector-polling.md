# Continuous player polling

The Python collector runs a continuous, fair loop through tracked Legend I
players. Five minutes is the minimum revisit interval, not a batch deadline.

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
share one spool/archive object; ordinary repeats update freshness without a new
observation or processing job.

Each regular key limits starts to 30/second with six concurrent requests. The
interactive key uses the shared database permit immediately before HTTP. It is
never borrowed for regular work.

## Validation and live-run boundary

`./dev trial` measures per-player gaps, coverage, failures, queue age, database
growth, spool recovery, and memory/swap behavior. It uses loopback fixtures; a
real Legend-day run still needs separate authorization.
