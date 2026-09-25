# Manual player-list import verification

Verified September 25, 2026 against `3836c1d`. Zubair chose operator-run manual
imports for launch instead of extending the old bootstrap command or building
a reusable import feature. This verifies the manual database operation and
existing eligibility processing; it does not authorize or prove a live import.

## Procedure verified

Use the existing database connection tools and functions. No new script,
command, dependency, migration or application change was added.

1. Decode the supplied text, accepting its optional invisible encoding header.
   Validate every line before writing. Reject malformed tags, invalid encoding
   and blank lines; normalize tags and remove duplicates. Record the source
   fingerprints, row count and unique count without logging tag contents.
2. Run one database transaction for the whole list. Take the transaction lock
   `pg_advisory_xact_lock(hashtextextended('manual-list-import', 0))` so manual
   imports serialize. Load normalized tags into a temporary table with a unique
   `tag` column; drop that temporary table on commit.
3. Insert missing `players` rows as `active=false, eligibility_state='unknown'`
   using `ON CONFLICT (normalized_tag) DO NOTHING`. These are unconfirmed
   candidates, not proof of real players. Preserve existing identities, profile
   history and regular collection times; never force a tag to eligible.
4. Select input players who are not already active and eligible and have no
   unfinished profile check. An unfinished check is a `collector_work` row with
   kind `discovery_profile`, `initial_collection` or `live_refresh` and status
   `pending` or `waiting_retry`. This check must span all collection cycles.
5. Pass those player IDs, at most 500 per call, to the existing
   `clashlens_enqueue_discovery_profiles(bigint[])` function. Commit the whole
   operation once every batch succeeds. If interrupted, roll back and repeat
   the same list. If the commit succeeded but its acknowledgement was lost,
   repeating the operation preserves players and reuses unfinished work.
6. Read back aggregate player and work counts. Existing collection checks the
   profiles and league history; confirmed eligible players then become due for
   battle collection. Real noneligible players retain their history. Unknown
   eligibility or failed responses must not be treated as confirmed eligibility.

The database helper alone is insufficient for a manual retry: its duplicate
work key includes a five-minute cycle. Step 4 prevents a later retry from
adding a second unfinished check. A later submission may legitimately recheck
an inactive player whose previous check finished. Keep the normal collector
startup and Reset handling; the import must not bypass them.

The legacy `bootstrap-population` command still rejects more than 20,000 tags,
duplicate input and later new runs. Those restrictions are unchanged. Do not
use that command for the supplied population, edit its receipt tables or force
players active to get around its restrictions.

## Observed results

The test used the existing Python environment and all repository migrations
in disposable schemas on a separate PostgreSQL 18.6 container on Rogue. It
was limited to one CPU and 512 MiB memory, with a 384 MiB temporary data mount.
No production connection or official Clash API was used. All players and
responses in the database test were synthetic; supplied source files were
checked separately in [product-status.md](product-status.md#supplied-lists-checked-september-25).

| Scenario | Observed result |
| --- | --- |
| Initial load: 50,830 rows, 22,157 unique tags, optional encoding header | 22,155 new identities plus two seeded identities; 22,156 profile checks. The seeded eligible player remained active. Database operation took 2.882 seconds. |
| Repeat after moving unfinished checks to an earlier five-minute cycle | Zero new identities and zero new checks; player and work rows unchanged. |
| Invalid tag, blank line, invalid encoding | Rejected before any database writes. |
| Interrupt overlapping later list after its first 500 checks | All new identities and work rolled back; existing rows unchanged. |
| Retry that later list: 1,001 existing tags plus 600 new tags | Exactly 600 new identities and 600 checks; seeded history unchanged. |
| Two simultaneous imports, 300 new tags each with 150 shared | Exactly 450 new identities and checks; one unfinished discovery check per player. Final test population: 23,207. |
| History preservation | Existing player state and saved observations, profile versions/effects and battle rows remained identical across the import operations. |
| Eligibility processing with production's discovery flag disabled | Eligible profiles activated; recognized noneligible profiles stayed inactive. Missing tier, not-found response and temporary failure did not activate players. |
| Departure, uncertain evidence and reentry | Unknown tier preserved confirmed eligibility. Recognized departure stopped collection; reentry reused the identity and retained all four profile observations. |
| Collector handoff after normal startup Reset handling | Five candidate cases produced exactly one eligible player due for its first battle; initial discovery work required league history. |

The first final-collection assertion omitted startup Reset handling and the
collector correctly returned no regular work. The focused repeat called the
existing `begin_reset` on the empty test population, verified Reset readiness,
then processed the five cases and passed. No safeguard or product code changed.

The existing targeted tests also passed: **21 passed in 49.31 seconds**. These
covered profile parsing, discovery processing, eligibility cancellation,
discovery enabled/disabled behavior, initial league-history collection,
regular player claiming and profile/battle evidence processing. No tests were
edited. The temporary database container and connection were removed afterward.

## What this does not establish

- Actual existence or current eligibility of the 22,157 supplied tags; none
  have been imported into the live application by this verification.
- Live collection throughput, API-key capacity, raw storage or six-month cost.
  The database insertion timing is not a full-population collection trial.
- Recovery, delivered alerts or production readiness. Apply the launch gates
  in [#119](https://github.com/zzzubair/clashlens/issues/119) before real traffic.
- Automatic public tag lookup, clan discovery or scheduled Monday eligibility
  rechecks. Those remain separate requirements; manual lists do not replace them.
