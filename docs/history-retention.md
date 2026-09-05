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

## Capacity and rollout gates

A six-month capacity guarantee requires measured novelty, relation/index/TOAST
and WAL growth, spool occupancy, backups and operating headroom on Fedora. Small
fixture tests demonstrate correctness, not production capacity. Issue #60 Step 9
still requires 12,500 real players and 288 production-cadence cycles. No synthetic
run replaces that gate. Scaleway permissions, immutable creation, restore and
final-host acceptance remain launch checks in #31. Do not enable production
cleanup or close those gates based on unit-test results alone.
