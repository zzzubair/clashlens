# Compact history and retention

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

The default is preview only. Finalization verifies the season ended
under the same completed-season gate, every required player summary and
every army lens/category matches its current projection (complete or
explicitly partial summaries are accepted; missing rows, stale digests,
or unexplained failures block), and season-scoped processing, replay,
publication-generation, and correction work is terminal. Unknown fails
closed. The applied `finalized` record fences writers atomically before
any deletion. Retirement then deletes all `api_player_daily_logs` for
the season, its `army_analytics_battle_facts` and redundant
`army_analytics_completed_days` markers, and battle detail
(decodes, perspectives, evidence, source reports, payload membership,
battles) only where no retained, live, shared, or protected dependency
still needs them; each table is limited to 1–1000 rows per invocation
so runs are bounded, restartable, and idempotent. Summaries, players
and accounts, raw lifecycle, reset baselines, frozen publication
identities/entries, boundary generations/manifests, correction chains,
and ranked-day versions are retained, and restrictive foreign keys are
unchanged. Once finalized, targeted corrections, replays, and
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
partition, and materialized view in the active schema (heap plus
indexes/TOAST), row counts, compact-summary size distribution, and the
explicitly labeled migration metadata exclusion. The unmeasured list is:
generated WAL, retained WAL and base backups (seven-day recovery
window), spool occupancy, and remote raw bytes/request tariffs. The
projection covers six calendar months (about 6.5 twenty-eight-day
seasons) against measured usable capacity with headroom. Live detail and
daily bookkeeping are not guessed as zero: until measured, the projection
reports a lower bound, names the missing components, and leaves
`fits_budget` unavailable. It labels itself synthetic extrapolation, never
#60 Step 9 live validation, and treats vacuum-reusable space as reusable,
not as disk shrinkage.

Synthetic fixture illustration (2-player populated season, empty battle
arrays, PostgreSQL 18): 56 daily logs at 114,688 B allocated and 3 army
facts retired to zero rows; 2 player summaries at 1,400 B/row and 22
army summary rows at 8,720 B total retained with byte-identical API
reads; relation files did not shrink (vacuum-reusable). The labeled
12,500-player projection from this fixture is about 114 MB of retained
summaries over 6.5 seasons against measured Fedora capacity — a lower
bound, not acceptance: the fixture carries no measured live detail or
bookkeeping load, no TOAST pressure or at-scale indexes, no
correction/opposite-perspective frequency, and no WAL, backup, spool,
or remote-tariff costs. The storage CLI leaves those major components
unmeasured rather than passing zero values.

Migration 0020 adds shared whole-season army summaries: one
`army_season_summaries` record per (season, lens, category) with
whole-season usage counts and rates, 0/1/2/3-star attack counts, the
three-star rate derived from the stored attack sample, and the underlying
denominators plus excluded/undecodable counts and honest coverage.
Summaries are projected from the current versioned battle facts and read
back without battle detail; offense and defense stay separate. Existing
detail is retained; no cleanup is authorized by this migration.

```sh
python -m clashlens.cli materialize-army-season-summaries --season-id 1785714000
# Inspect the JSON report, then explicitly opt in:
python -m clashlens.cli materialize-army-season-summaries --season-id 1785714000 --apply
```

The default is preview only. A season is completed under the same gate as
the player summaries (confirmed anchor timing or a completed day-28
publication); anything else, including a live season, is left untouched.
Historical army reads cover Legend days 1–28 with the whole-season sample
only: no day ranges, no population filters, no per-battle drilldown. Late
corrections refresh already-summarized seasons atomically with the day's
facts; unchanged categories are a no-op.

Migration 0019 adds compact historical player-season summaries: one
`player_season_summaries` record per player per season with typed season
totals and up to 28 compact daily trophy entries. Summaries are projected
from the latest published daily log per day (joined only to that log's
exact ranked-day version) and read back without battle detail. Existing
detail is retained; no cleanup is authorized by this migration.

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
bookkeeping. They do not delete existing history or remote objects. Deploy with
`./deploy.sh up` so old collectors and workers are drained before migration;
start the updated Python services afterwards using the normal deployment flow.
Back up and rehearse restore before upgrading a populated database. Applying the
migrations is not a production-cleanup authorization.

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
candidates per invocation. Schedule bounded runs only after validating their
reports and queue impact. No automatic schedule is installed.

The collector cleanup removes redundant completed root collections, preserving
both ends and transitions of unchanged log runs, semantic profile anchors,
latest profile effects and rankings, snapshot/reset references, and unfinished
or failed work. Completed derived processing jobs (except exports and legacy
publication-migration anchors) and unreferenced parsed/ranking payloads are also
eligible. Restrictive domain foreign keys remain a final safety barrier. Lock or
statement timeout aborts the transaction; investigate rather than disabling
constraints. Retries/child collection trees and failed work can still accumulate
and require operator investigation; this is not a universal TTL on all tables.

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

## Raw archive: six months since last seen

Do **not** configure an upload-age lifecycle on the evidence namespace. A response
can remain useful for years while its bytes stay unchanged. Catalogue sightings
use a one-hour upper bound, so deletion can be delayed by an hour, never advanced
before six calendar months. Existing catalogue entries conservatively start their
retention clock at migration time.

Run on the collector host, mounting the **exact same spool and lock directory**
and using its archive instance/bucket/marker configuration. Supply separate
operator database and object credentials with DELETE permission; do not add
DELETE permission to normal collection credentials.

```sh
python -m clashlens prune-archive --max-objects 100
# Only after reviewing the preview and verifying the shared spool:
python -m clashlens prune-archive --max-objects 100 --apply
```

This command uses the normal archive and spool arguments/environment settings.
It validates archive identity through the normal reader initialization. A wrong
spool path defeats cross-process locking: provisioning the shared mount is an
operator prerequisite, not something the command can prove remotely.

Pending verification and unfinished/failed processing or replay protect an
object. Retirement commits a tombstone before remote DELETE. Unknown DELETE
outcomes stay `retiring` and are retried. Recollection uses a new immutable
`generation/<token>` location, so a delayed old DELETE cannot remove new bytes.
Catalogue tombstones remain; this command does not compact them. Bucket versioning,
noncurrent versions, backup retention and orphan objects need separately verified
provider policies; deleting a current key does not prove all provider storage was
reclaimed.

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

Reproduce the storage probes from `python/` with the disposable database URL:

```sh
python ../scripts/performance_runner.py duplicate-heavy \
  --duplicate-observations 100 --duplicate-cycles 2 --lanes 4 --output /retained/duplicates.json
python ../scripts/performance_runner.py mixed-backfill \
  --live-jobs 20 --backfill-jobs 100 --lanes 4 --output /retained/mixed.json
```

Artifacts remain on Fedora under `/home/zubair/clashlens-issue82-tools/`:
`duplicates-807be87.json` (artifact digest
`753ce30eca14b36d5624a4ba8c873c0ae26ae758d90de284bebc6e1d070062f4`) and
`mixed-807be87.json` (artifact digest
`f91611779b7d542086fcb3b41a46afc0a7712037729904b77cfb6643170e23da`).
The reproducible cleanup probe is retained there as `prune-probe.py`.

## Capacity and rollout gates

A six-month capacity guarantee requires measured novelty, relation/index/TOAST
and WAL growth, spool occupancy, backups and operating headroom on Fedora. Small
fixture tests demonstrate correctness, not production capacity. Issue #60 Step 9
still requires 12,500 real players and 288 production-cadence cycles. No synthetic
run replaces that gate. Scaleway permissions, immutable creation, restore and
final-host acceptance remain launch checks in #31. Do not enable production
cleanup or close those gates based on unit-test results alone.
