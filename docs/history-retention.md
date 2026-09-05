# Compact history and retention

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
