# Compact history and retention

## Agreed changes, 2026-09-25

These requirements extend the delivered #126 format; implementation and
remaining gaps are described below. [#139](https://github.com/zzzubair/clashlens/issues/139) owns the new
history views, [#129](https://github.com/zzzubair/clashlens/issues/129) owns
scheduled cleanup, and [#122](https://github.com/zzzubair/clashlens/issues/122)
owns backup-compatible raw retention. See [product-status.md](product-status.md)
for deadlines and the full product map.

- Keep daily EOD, attack gain, defense loss and EOD change from the previous
  Legend day. Day 1 uses 5,000; Day 28 EOD supplies the season-ending trophies.
  EOD 5,050 followed by 4,950 means -100. Missing EOD stays unknown. This is
  distinct from the existing stored battle-result `net_change`.
- Show final Clash Lens rank among tracked players on completed-season pages.
  The current summary reads official-rank evidence; that must not substitute
  for the new public rank. Keep official evidence and existing integrity fields.
- Keep both all-tracked and final-season Top 100 army statistics. Fix Top 100
  membership from final Clash Lens standings and include those players' recorded
  battles across the whole season, not changing daily membership.
- Preserve individual Clan Castle troop usage in both populations. The toggle
  changes regular troops to individual Clan Castle troops; keep BY/AGAINST
  views, unit identities, quantities, outcomes and denominators distinct.
- Measure the added summary bytes and six-month cost. An unaffordable result
  requires a decision; do not silently drop an agreed population or troop view.
- New history views must be ready before the first tracked season ends,
  November 2 at 05:00 UTC if tracking starts October 5. They do not block an
  earlier tracking or website launch. Protect their required detail until ready.
- Allow corrections for seven days after season end. Summaries can be visible
  and updated during that window. Finalization and eligible detail retirement
  must wait for the window, required summaries, verified coverage and existing
  safety checks. Failed or unfinished work blocks cleanup. Keeping raw bytes
  does not make a finalized season reopenable in the current code.
- Keep raw responses available for every restore promised by the ten-day
  backup window, including time to perform the restore. This is separate from
  the seven-day season-correction window. See
  [raw expiry and recovery protection](#implemented-raw-expiry-and-required-recovery-protection)
  for the implemented rule and required restore proof.

No existing counters, coverage evidence, unknown-unit history or kept production
data are authorised for deletion by this documentation change. Historical day
ranges, arbitrary trophy filters, battle drilldown, destruction, combinations
and army-type classification remain outside the new history scope.

## Implemented finalization and detail retirement

Migration 0021 adds the durable completed-season detail-retirement
record: one `season_detail_retirements` row per season storing the
established boundaries, player/army summary digests, and retirement
progress. The record survives daily-log and battle-fact deletion and
fences later writers. This migration creates the record only; no data
is deleted.

```sh
python -m clashlens.cli finalize-season-detail --season-id 1785714000
# Inspect the JSON report, then explicitly opt in:
python -m clashlens.cli finalize-season-detail --season-id 1785714000 --apply
python -m clashlens.cli retire-season-detail --season-id 1785714000 --max-rows 500
# Inspect the JSON report, then explicitly opt in:
python -m clashlens.cli retire-season-detail --season-id 1785714000 --max-rows 500 --apply
```

The default is preview only. Both commands refuse until seven days after
the exact Season end, the end of its 28th Legend day at the 05:00 UTC
Reset; September's Season `1788757200` ends October 5 and can close
from October 12 at 05:00 UTC. The refusal reports `status: blocked`,
`reason: season_close_wait`, `eligible_at` and `applied: false`, and
writes nothing. Apply reruns every check rather than trusting a preview.
Retirement rechecks the stored window and the wait on every run, using
the database clock, so a `finalized` record written early by older code
cannot start deletion. The exact window comes from the confirmed current
and previous Season, and for older Seasons from the same 28-day calendar
counted back from them. A Season whose window cannot be established, or
a stored window that is missing or disagrees with it, blocks; every
blocked result carries `eligible_at` whenever the window is known. Player
and army summaries still build and refresh as soon as the Season ends;
only closing waits. The wait is a clock check only; the close guard below
checks the work.

Finalization verifies the season ended
under the same completed-season gate, every required player summary and
every army lens/category matches its current projection (complete or
explicitly partial summaries are accepted; missing rows, stale digests,
or unexplained failures block), and then asks the close guard
(`season_finalization_guard.py`). Retirement asks it again on every run,
so a `finalized` record, including one older code wrote, is never enough.
Arrival time never excuses work. The guard blocks on any processing job
that is not complete with a success outcome, unless its own dates prove
it cannot touch the Season: a response saved before the Legend day ahead
of the Season, a Legend day outside that day to the closing day, a Reset
outside Season start to Season end, or an army redecode whose battles all
fall on other Seasons' days. A job whose source, dates or battles cannot
be read, an export or an unknown kind of work stays in scope. It also
blocks on any saved response from the Legend day before the Season
onwards whose newest processing outcome is not `processed` (finished-job
cleanup keeps outcomes, so a missing job proves nothing); any replay
request for such a response that is not complete; any publication
generation at a Reset from Season start to Season end, the closing Reset
included, that is not published or superseded; any correction from
Season start onwards that is not finalized with a published or superseded
generation; and any
[Reset settlement check](domain.md#8-evidence-and-confidence-states) that
reads the Season's days and is unfinished or has an unprocessed response.
A missing table, failed query or check over 10 seconds blocks.
Each blocker lists at most five example ids under `blocking_work`; missing
tables and failed checks are listed by name instead. Until
the expanded history above can be checked, it also always reports
`promised_history: ["expanded_history_unavailable"]`, so no Season can
close yet. The applied `finalized` record fences writers atomically before
any deletion. Retirement then deletes all `api_player_daily_logs` for
the season, its `army_analytics_battle_facts`, redundant
`army_analytics_completed_days` markers and their `army_analytics_day_totals`
and `army_analytics_rank_band_totals`,
and battle detail
(decodes, perspectives, evidence, source reports, payload membership,
battles) only where no retained, live, shared, or protected dependency
still needs them; each table is limited to 1–1000 rows per invocation
so runs are bounded, restartable, and idempotent. Summaries, players
and accounts, raw lifecycle, reset baselines, frozen publication
identities/entries, boundary generations/manifests, correction chains,
and the ranked-day versions left by the copy cleanup below are retained, and
restrictive foreign keys are unchanged. Since migration 0051 a battle-log fetch lists its battles in one
row instead of one row per battle; retirement empties a retired battle's place
in that list, and a database trigger stands in for the foreign key a list
cannot carry. Once finalized, targeted corrections, replays, and
rematerializations return an explicit `season_detail_retired` result
before any domain mutation and never rebuild retired detail or replace
summaries from a reduced sample; rolling logs containing old battles
still process their live-season content. Detailed reconstruction ends at
finalization; reopening or restore is out of scope. Repeat bounded
retire runs until the report status is `retired`.

Measure one season (or the whole database) plus a labeled projection:

```sh
python -m clashlens.cli measure-season-storage --season-id 1785714000 --players 12500 --headroom-percent 20
```

The report carries allocated bytes for every application table,
partition parent/child, materialized view, and application sequence in the
active schema (heap plus indexes/TOAST), row counts, compact-summary size
distribution, relation-kind inclusions/exclusions, and the explicitly labeled
migration metadata exclusion. Partition parents are cataloged with zero
allocation so their children are not double-counted. The unmeasured list is:
generated WAL, retained WAL and base backups (ten-day recovery
window), spool occupancy, and remote raw bytes/request tariffs. The
projection covers six calendar months (about 6.5 twenty-eight-day
seasons) against measured usable capacity with headroom. Live detail and
daily bookkeeping are not guessed as zero: until measured, the projection
reports a lower bound, names the missing components, and leaves
`fits_budget` unavailable. It labels itself synthetic extrapolation, never
#60 Step 9 live validation, and treats vacuum-reusable space as reusable,
not as disk shrinkage.

Pre-migration-0035 synthetic fixture illustration (2-player populated season,
empty battle arrays, PostgreSQL 18): 56 daily logs at 114,688 B allocated and 3
army facts retired to zero rows; 2 player summaries at 1,400 B/row and 22
legacy army summary rows at 8,720 B total retained; relation files did not
shrink (vacuum-reusable). The current API does not read those legacy army
rows. See
[Unit, quantity and outcome summaries](#unit-quantity-and-outcome-summaries-issue-126)
for the replacement format. The labeled
12,500-player projection from this fixture is about 114 MB of retained
summaries over 6.5 seasons against measured Fedora capacity — a lower
bound, not acceptance: the fixture carries no measured live detail or
bookkeeping load, no TOAST pressure or at-scale indexes, no
correction/opposite-perspective frequency, and no WAL, backup, spool,
or remote-tariff costs. The storage CLI leaves those major components
unmeasured rather than passing zero values.

Migration 0020 created the base `army_season_summaries` records and the
materialization command below. Migration 0035 replaced its label-based outcome
rows for newly built summaries. The
[issue 126 section](#unit-quantity-and-outcome-summaries-issue-126) owns the
current stored and public contract. Existing detail is retained; neither
migration authorizes cleanup by itself.

```sh
python -m clashlens.cli materialize-army-season-summaries --season-id 1785714000
# Inspect the JSON report, then explicitly opt in:
python -m clashlens.cli materialize-army-season-summaries --season-id 1785714000 --apply
```

The default is preview only. A season is completed under the same gate as
the player summaries (confirmed anchor timing or a completed day-28
publication); anything else, including a live season, is left untouched.
The implemented v3 army reads cover Legend days 1–28 for all tracked players
only: no day ranges, no population filters, no per-battle drilldown. The agreed
Top 100 and Clan Castle additions above still need implementation. Late
corrections refresh already-summarized seasons atomically with the day's
facts; unchanged categories are a no-op.

Migration 0019 adds compact historical player-season summaries: one
`player_season_summaries` record per player per season with typed season
totals and up to 28 compact daily trophy entries. Summaries are projected
from the latest published daily log per day (joined only to that log's
exact ranked-day version) and read back without battle detail. Existing
detail is retained; no cleanup is authorized by this migration.

Each daily entry keeps `eod_change` beside the battle-result `net_change`:
the day's EOD minus the previous day's EOD, with Day 1 measured from 5,000.
A missing, non-adjacent or unknown previous EOD leaves it unknown. Its
`eod_state` and `eod_change_state` are `provisional` whenever known: no Reset
reading is proven settled. This is summary format
`player-season-summary-v2`. An older summary stays listed and readable with
those three fields unknown; the command below rebuilds it from retained detail
where the Season's detail has not been retired. Season closure already refuses
a summary that no longer matches a fresh projection.

```sh
python -m clashlens.cli materialize-season-summaries --season-id 1785714000 --max-players 100
# Inspect the JSON report, then explicitly opt in:
python -m clashlens.cli materialize-season-summaries --season-id 1785714000 --max-players 100 --apply
```

The default is preview only, and the preview report already carries the
next cursor. Page large populations by repeating the command with the
reported `next_after_player_id` until it is null:

```sh
python -m clashlens.cli materialize-season-summaries --season-id 1785714000 --max-players 100 --after-player-id 12345 --apply
```

A season is completed when it matches the confirmed anchor's current
season id with its exact 28 days elapsed, matches the previous season
id, or — for noncanonical legacy ids — has a completed day-28
publication establishing the boundary. Anything else, including a live
season, is left untouched.

Migrations 0016–0018 separate durable game history from repeated collection
bookkeeping. They do not delete existing history or remote objects. Follow the
[`./ops` deployment flow](deployment.md) to stop the running services before
applying migrations and starting an updated release.
Back up and rehearse restore before upgrading a populated database. Applying the
migrations is not a production-cleanup authorization.

## Extra ranked-day copies

Every battle saves a complete new copy of the player's Legend day result and
its daily log; on 2026-10-02 that was about 13 copies per player-day and
1.5-2 GB a day. Since migration 0052, `./ops` runs
`python -m clashlens.ranked_day_compaction` in the worker container from the
`clashlens-ranked-day-compaction.timer`, 5 minutes after the previous run
finished. Once a Legend day has ended and its Reset work is done (the same
check the [late-battle sweep](domain.md#6-ranked-day-and-leaderboard-snapshots)
waits for, and at least 30 minutes have passed), the run deletes that day's
replaced copies with their daily logs and adjustments. It keeps:

- the newest copy of each player-day, and the copy the newest daily log points at;
- every copy a publication generation, publication manifest, frozen
  leaderboard, analytics summary, army fact or queued publication correction
  points at;
- the copy the following day's newest copy was built from;
- for a kept copy that made an earlier result current again, the copy it
  replaced and the copy holding that earlier result. Its hash is built from
  both: recalculation needs the replaced copy to find the result unchanged,
  and later cleanup passes need the earlier result to recognise the hash.

A kept copy that named a deleted copy as the one it replaced names the
nearest older kept copy instead, or none. Each batch covers 25 players of the
day not yet cleaned that has waited longest for a batch, a day never started
first, plus the day before it, in its own transaction, which is cancelled and
rolled back after 5 seconds so it never holds those player-days for long; the
run then stops and the next one retries the batch. Each run stops after 2
minutes, so a backlog of many days is worked through over several runs. A newer
copy saved later, such as a late correction, makes that day cleaned again,
taking turns with the other ended days so it cannot keep them waiting
(migration 0067). Nothing that runs later needs the deleted copies: late
corrections and the next day's recalculation read only the newest copy, and an
earlier result that becomes current again is saved as a new copy. The deleted
copies are gone from the database; the raw responses they were calculated
from stay under the raw-response rules. `./ops ranked-day-compaction` runs one
pass by hand and `./ops logs ranked-day-compaction` shows each run's totals.

## Reused manifest rows

Each correction of a Reset's board freezes new manifests, the frozen inputs
kept as proof of what the board was built from. On 7 October 2026, 35
generations froze 911,900 manifest rows, about 2.15 GB, though about 97% of
each player's rows repeated an earlier generation's. Since migration 0081 a
manifest names the Reset's newest full manifest of its kind as its base and
stores only the players whose row differs from it; a differing row that an
earlier manifest already stores keeps its plain columns and names that manifest
for its identity. An army manifest also stores its Season input lists as the
IDs removed from and added to the base's, unless that would change more than
half the IDs. A manifest is frozen in full again only when there is no earlier
manifest or the membership changes, however many rows differ, so rebuilding any
manifest reads only its own rows, one full manifest, and for a reused identity
the one row storing it in full. `boundary_publication_manifest_entries(id)`
returns any manifest's complete rows exactly as a full manifest stores them,
and the digest still covers those rows. Database triggers refuse a base that is
not a sealed full manifest of the same Reset and kind, and a reused identity
not stored in full. Every freeze logs a `boundary_manifest_frozen` line with
rows and identity bytes stored and reused. Measured on a 13,000-player test
board, a correction manifest stored 0.3-0.4 MB of rows instead of 12 MB.
Replaying the same rule on production's 4-8 October manifests stores about 91%
fewer row bytes. Manifests frozen before migration 0081 stay full and unchanged.

How often the newest Reset's board is rebuilt is unchanged and is held as an
owner decision: each late correction still freezes a new generation, and each
one still copies the generation's member list and board entries in full; only
the manifest rows are reused.

## What remains

- Battle reports are shared across changing rolling logs. Every returned row is
  inspected; unchanged sightings reuse reports. Opposite perspectives and genuine
  corrections remain separate evidence. An A→B→A correction reuses A's content
  but records a new evidence event.
- Shared battle identity stays Legend day/attacker/defender. Timestamps remain
  details, not a new unverified cross-perspective identity rule.
- Profile versions keep the implemented semantic fields (name, trophies, league,
  season, eligibility and clan name), rather than copying the full API body into
  both parsed payloads and profile versions. Full raw profiles remain in the
  archive until expiry. Unused upstream profile fields are not permanent history.
- Daily standings, published analytics, reset anchors and reconciliation evidence
  are not bulk-deleted. Old representations remain readable; migration does not
  rewrite historical JSON or immediately recover its disk space.

## Completed operational history

Run with a separate operator database credential, not a runtime application role:

```sh
python -m clashlens prune-history --retention-hours 48 --max-jobs 1000
# Inspect the JSON report, then explicitly opt in:
python -m clashlens prune-history --retention-hours 48 --max-jobs 1000 --apply
```

Use the normal `CLASHLENS_DATABASE_URL_FILE` secret-file setting. The default is
preview only. Retention accepts 48–672 hours; each table is limited to 1–1000
candidates per invocation. Only the finished-job part runs on a schedule: in
production a timer runs `prune-history --jobs-only --apply` every 30 seconds
after the last batch ends, as the `clashlens_history_retention` role from
migration 0053, which can do nothing else. Its operation and failure checks
are in [finished-job cleanup failed](operating.md#finished-job-cleanup-failed).
The other parts still run only by hand. Deleting a job also deletes its
attempts, events and replay-request record; processing outcomes and profile
effects stay, with their link to the deleted attempt cleared.

The collector cleanup removes eligible completed explicit collection work, preserving
both ends and transitions of unchanged log runs, semantic profile anchors,
latest profile effects and rankings, snapshot/reset references, and unfinished
or failed work. Completed derived processing jobs (except exports and legacy
publication-migration anchors) and unreferenced parsed/ranking payloads are also
eligible. Restrictive domain foreign keys remain a final safety barrier. Lock or
statement timeout aborts the transaction; investigate rather than disabling
constraints. Ordinary regular collections have no explicit work row and
their observations are not selected by this cleanup. Their completed processing
jobs can be pruned separately, but observation metadata and archive catalogue
tombstones remain. Failed work also requires operator investigation; this is
not a time limit on all tables or a bounded database-size guarantee.

Since migration 0051, `known_player_discoveries` records a player once per
source (battle opponent or official ranking) instead of on every fetch that
returns them; rows recorded before it stay.

The same command also prunes redundant discovery provenance attached to retained
roots: `known_player_discoveries` rows and observation-backed
`player_discovery_events` (source `official_global_ranking`), bounded per table by
`--max-discoveries` (1-1000, default 1000). A discovery row is eligible only when
its owning root collection is old, its observation is successfully processed and
completed before the confirmed current-season start, under a completed root
collection of an allowed
work type with no child tree, transport failure, reset-baseline reference,
unfinished or recent processing, or pending replay request. An unknown season
boundary fails closed and live-season discovery history is retained. Source-less
events (submitted tags, player references, account links) are preserved, as are
parent roots and all semantic detail. After guarded cleanup, exhaustive
discovery-event history becomes unavailable; discovery scheduling, replay,
corrections, and semantic history are unchanged.

Normal PostgreSQL vacuum makes deleted space reusable; deletion does not shrink
relation files or imply that retained WAL/backups have expired. Do not run
`VACUUM FULL` on production as part of routine cleanup.

## Implemented raw expiry and required recovery protection

Do **not** configure an upload-age lifecycle on the evidence namespace. A raw
response becomes due **86 days after the end of its latest sighting's UTC
day**: never less than 86 days after the sighting, at most one day more. A body
returned again on a later day moves its deadline later; an earlier sighting
never shortens it. Every sighting on the same day gives the same deadline, so
only a body's first sighting each day rewrites it (migration 0087).
Uploading late does not start another retention clock.

Migration 0046 recalculates existing stored responses from the later of their
latest retained sighting and first verification, and discards the old season
deadline. The latest retained sighting is the newest of that location's
observations, its upload record's latest sighting and the newest compact poll
state for its hash.
A response with no retained sighting counts from its first verification. Records
with neither time have no deadline and are never automatically deleted.

- **Retiring**: the state cleanup gives a due response, which blocks every new
  use of it while its bytes still exist.
- **Recovery hold**: the twelve days a retiring response waits before cleanup
  deletes its bytes.
- **Marked or held response**: a response that is retiring and still inside its
  recovery hold.

A response the old code had already marked `retiring`
starts its recovery hold at its recalculated deadline, or at upgrade time if
that is later.

A due response is not deleted straight away. Cleanup first marks it `retiring`,
which blocks every new use, then deletes its bytes only **twelve days later**: the
ten-day recovery window chosen on October 8 (seven days until then) plus a
two-day allowance to carry out a restore. A restore can target any point from
the last ten days.
Anything still usable at that point was marked after it, so its bytes survive at
least two more days after the restore starts. For longer restores while
production keeps running, follow the
[restore procedure](deployment.md#restore-into-a-separate-database).

A response therefore stays usable for at least 86 days after its latest
sighting. With no unfinished work and cleanup keeping
up, its bytes stay about 98 days, plus the wait for the next cleanup batch. The
measured 21.83 GB/day of new raw responses (October 2) means about 2.14 TB
stored, roughly EUR 34/month at EUR 0.01606/GB-month. This is a projection, not a bill.

Any unfinished upload of the same bytes and unfinished/failed processing or
replay keep a response usable. Marking commits before any
DELETE, and the delete step rechecks that the row is still this archive's held
tombstone. An unknown or failed DELETE leaves the row `retiring`, is counted in
`failed_objects`, does not stop the batch and is retried by the next run.
Recollection uses a new immutable `generation/<token>` location, so a delayed
old DELETE cannot remove new bytes. An upload whose original location is already
`retiring` or `expired` gets a new generation before it writes anything. One
whose location is marked while it uploads is never attached to it: it uploads
again under a new generation. One that finishes on a kept location extends its
deadline when its latest sighting is later. Catalogue tombstones remain;
cleanup does not compact them. Bucket versioning, noncurrent versions, backup retention and
orphan objects need separately verified provider policies; deleting a current
key does not prove all provider storage was reclaimed.

The owner approved production expiry on 2026-10-08 after the real-size restore
rehearsal described in [deployment.md](deployment.md#restore-into-a-separate-database).
That rehearsal restored a 4.4-day-old point and did not read raw references, so
#122/#129 still owe a genuine ten-day-old restore that reads the raw references
it needs. The code stays off by default; production turns it on in `app.env`.
[`deployment.md`](deployment.md#raw-response-cleanup) owns the scheduled job,
its credentials and the dry-run-first enablement steps. The command it runs is:

```sh
python -m clashlens prune-archive --max-objects 1000          # preview
python -m clashlens prune-archive --max-objects 1000 --apply  # mark and delete
```

Each run deletes up to the batch size of held responses whose twelve days have
passed, then marks up to the batch size of due ones. The preview changes nothing
and reports how many objects and bytes that one batch would delete and mark.
It must run on the collector host with the **exact same spool**, because a wrong
spool path defeats cross-process locking.
Migration 0047 adds a lookup from each processing job to its source response,
built in the normal migration transaction while application services are stopped,
so each response's in-use check finds matching jobs through that lookup rather
than searching the whole job history.

The local spool remains bounded temporary storage, not a bucket mirror. Existing
spool cleanup is separate from remote retirement. After raw expiry or operational
pruning, exhaustive replay of every historical poll is intentionally unavailable;
new replay work against retired evidence is rejected. Durable semantic battle,
player and publication history remains available.

## Fedora fixture measurements

Measured application source: `807be87` (September 5, 2026). PostgreSQL 18 ran
in the owned disposable Fedora container with 2 CPUs / 2 GiB; the Python driver
used four lanes. No official API requests were made. The host filesystem had
1,017,969,311,744 usable bytes and about 80.6 GB used. Temporary spool files were
on tmpfs, so their measurements are byte accounting, not NVMe performance proof.

| Probe | Workload | Relation growth | WAL generated | Spool / distinct raw bytes |
| --- | --- | ---: | ---: | ---: |
| Duplicate | 100 balanced responses × 2 cycles | 11,239,424 B | 21,821,224 B | 55,630 B |
| Mixed | 20 live profiles + 100 battle/backfill jobs | 2,023,424 B | 2,479,936 B | 5,915 B |

Both probes reported no hard failures or active queue residue. Retained WAL
was 1 GiB and did not grow during either short sample; this does not mean WAL
or backup storage costs zero. Duplicate processing produced 33 shared battles,
33 evidence rows and **zero per-battle occurrence rows**, despite 66 battle-log
responses. It still wrote 200 observations and processing jobs. The balanced
sample deliberately overrepresents Top-200 relative to production: its 66
ranking responses produced 13,200 operational entry links. Do not scale its
56,197 B/response average to the production endpoint mix.

For the 100-battle mixed sample, the allocated totals for `legend_battles`,
`battle_source_rows`, `battle_evidence`, `battle_perspectives` and
`battle_payload_rows` sum to 581,632 B: **5,816 B/distinct battle** for these
selected tables, including index/TOAST allocation, ignored source rows and
small-table page overhead. At the planning estimate of 100,000 battles/day,
180 days would use about **104.7 GB for these tables alone**. That is not total
local use: the sample does not establish representative armies, correction or
opposite-perspective frequency, profile changes per player/day, retained roots,
publications, vacuum reuse, backups or orphan growth. A full player/day rate and
six-month headroom remain unqualified.

A separate four-cycle cleanup probe aged completed bookkeeping by three days.
Preview changed no rows; apply reduced observations/collector jobs from 400 to
135, processing jobs from 400 to zero, profile effects from 136 to 68, log samples
from 132 to 66 and ranking versions from 132 to one. All 66 source reports,
33 battle evidence rows, 34 profile versions and 200 canonical ranking rows
remained. This tests row retention, not post-vacuum filesystem shrinkage.

The duplicate archive probe verified one PUT plus one GET for a new object and
zero bucket requests for its verified repeats. Real archive GB-month, PUT/GET,
DELETE and egress cost still require representative raw novelty/body sizes and
the selected Scaleway tariff. Neither fixture byte totals nor a creation-age
lifecycle establish that cost.

## All-component storage slice (issue #82 preflight, 2026-09-08)

A closing measurement slice ran:
a 12,500-player x 28-day synthetic rehearsal in disposable PostgreSQL 18
(production materialization, finalization, bounded retirement to `retired`,
ordinary vacuum, canonical-string-identical historical reads), an exact
256-prefix metadata census plus a 384-body shape sample of the read-only
GCS corpus, and verified Scaleway/R2 tariff extraction. No official API
traffic; no production data; aggregate-only archive handling (no tags,
bodies, or archive references retained). Daily-log width comes from one
synthetic 50-entry column-size point scaled by measured body-sample sizes;
raw cost is a six-month average monthly figure with an end-state run-rate.

Measured: 1,399 B/player-season summaries (1,725 B allocated), 896 B/ranked
version, 312 B/empty daily log, 2,851 B/battle across six tables, 2,977
B/bookkeeping observation; finalize `ready` then `finalized` (12,500),
retire to `retired` in 350 bounded rounds with 61 MB vacuum-reclaimed;
corpus 683,391 objects / 24.4 GB over 29 days with bursty arrival (peak day
90.7%) and profile/battle-log p50 bodies of 23/66 KB. Central and
conservative six-month scenarios fit measured Fedora capacity with 20%
headroom; the seven-day recovery window is modeled as base backup plus
generated WAL on R2, and raw retention on Scaleway Standard. These were labeled
assumptions. Issue #120 owns the later provider and pricing decisions, while
[`deployment.md`](deployment.md) owns the current backup procedure (#63 is closed).

Retained on Fedora under `/home/zubair/clashlens-issue82-results/`:
`issue82-gcs-census.json`, `issue82-gcs-bodies.json`,
`issue82-pg-rehearsal.json`, `issue82-pricing.json` (beside the raw tariff
pages), and `issue82-storage-report.json` with the six-month report
`issue82-storage-slice-report.md`, each with a `.sha256` sidecar.

## Capacity and rollout gates

A six-month capacity guarantee requires measured novelty, relation/index/TOAST
and WAL growth, spool occupancy, backups and operating headroom on Fedora. Small
fixture tests demonstrate correctness, not production capacity. A synthetic run
cannot establish the official API key rate or prove behavior through a real
Legend day. Those checks require separate authorization before real traffic.
Scaleway permissions, immutable object creation, a proven backup restore, and
final-host acceptance remain launch checks. Do not enable production cleanup or
close those gates based on unit-test results alone.

## Unit, quantity and outcome summaries (issue 126)

This section records the format delivered by #126/#135. The agreed additions
at the top of this document are tracked separately in #139. Migration 0035
and projection `army-unit-usage-v3` replace the earlier outcome
and composition contract for newly built historical army summaries. They retain
one whole-season usage count per namespace-qualified unit ID and quantity, using
the season's usable battles as the denominator, plus 1★, 2★ and 3★ counts. Rates
and catalogue names are resolved when read. An army with five copies of a unit
records quantity five and one use. Battle-time trophy values and all clan-castle
contributions are excluded.
Player trophy summaries and current/live analytics are unchanged.

The implemented v3 reads do not offer destruction, combinations, day ranges or
population cohorts. Missing or legacy summaries without unit/quantity evidence
return unavailable; there is no website fallback to partial battle detail.
Unknown units have deterministic labels such as `Unknown spell #900`.
Unclassified troop IDs appear in both troop and siege views as
`Unknown troop or siege #900`. These are the same retained uses, not two
separate units. Once the catalogue establishes their
category, they appear only in that category with the current catalogue name.
Other unknown namespaces likewise resolve their names at read time. Separate player trophy-history records remain unchanged.
The API pages unit results in groups of 200.

The migration itself deletes nothing. Rebuilding an eligible nonfinalized season
replaces its old outcome arrays and deletes obsolete combination/clan-castle
summary categories. Already finalized legacy data is not silently rewritten or
reconstructed. A summary exceeding the 512 KiB retained category limit aborts
materialization and prevents retirement. See [issue-126-validation.md](issue-126-validation.md)
for behavioral evidence and storage measurements.
