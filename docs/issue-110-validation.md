# Issue 110 completion evidence

This record covers the six-step refactoring plan in
[issue #110](https://github.com/zzzubair/clashlens/issues/110). It does not
authorize going live. The final PR records the tested commit and CI runs.

## Scope and disagreements resolved

- Steps 1–5, including 4b/4c/4d, were already merged through PR #117. The
  completion audit checked their requirements against current code, rather
  than treating the checked boxes as proof.
- Step 4c stored official league history but had no season-page reader. Its
  completion comment called the reader later work; the issue requires that
  history feed the season pages. The completion change adds that reader.
- The profile's previous season ID is not a Legend season boundary. Readers
  use validated Legend I history and 28-day boundaries. Official history does
  not manufacture daily coverage or reinterpret the ambiguous official star
  labels as tracked battle totals.
- The local claim performance test had previously been described as a host
  timing problem. Reproduction on the base commit found an approximately
  101 ms claim; the query scanned rows that a bounded index probe could avoid.
  The fix separates ordinary/dependency probes and corrects their partial
  indexes. The existing 100 ms bound remains unchanged.
- The accepted API rate measurement used direct HTTPS from rogue, whose IP
  is allowlisted for the keys. The collector uses that configuration. The
  private API's token-verification client separately requires its existing
  fixed-egress proxy; deployment must configure it rather than removing
  that client's guard.

## Fedora isolation

The lifecycle test uses an isolated Fedora 44 cloud guest on rogue, not the
host running the agent or unrelated work. The guest has two virtual CPUs,
3 GiB RAM, a 30 GiB disk, SELinux enforcing, Podman 5.8.1, and systemd 259.
The official Fedora image signature and checksum were verified before use.
Its SHA-256 is
`28680fe5b371a5a82ebf43a31926e086a168e59949d03969c5093e7071f90b7f`.

Only fixture services are used: local Clash responses, archive storage,
Google login, and Discord login. No live player traffic or paid service is
part of this validation. Build, start, service recovery, reboot, and stopped
state persistence are separate checks; generated configuration alone is
not reboot evidence.

## Storage interpretation

The existing trial reports database allocation per stored response and
modeled daily growth at 8, 16, and 32 changed responses per player, for
12,500 players. Its before/after byte and response snapshots now cover the
same interval while producers are stopped or paused. Recovery drain counts
are recorded separately from that interval.

The historical #82 baseline is **166–185 GB**, or approximately
154.6–172.3 GiB. These units are not interchangeable. Extrapolating the
initial fixture population over six months is not a measured six-month
capacity result: the fixture front-loads initial collection and then keeps
most responses unchanged. The trial prints that extrapolation with its
limitation, alongside the useful per-response cost.

The current system does not prove that all database storage stays within
that baseline. Ordinary collection observations and archive catalogue
tombstones do not have a general deletion policy. A bounded local spool
does not imply a bounded database. Existing season-finalization and
retirement commands remain operator-invoked and are not scheduled by
`./ops`. This change does not delete retained data to make a projection fit.

Migration 0028 adds one latest-sighting timestamp per content-addressed
upload, preserving the retention deadline even when an upload is delayed
and compact response state moves on. Its eight-byte value adds roughly
0.8–3.2 MB/day at the modeled 100,000–400,000 new hashes/day, before tuple
overhead. That is a modeled cost, not an observed live change rate.

Some legacy compact sightings cannot be reconstructed. The migration
therefore also gives existing uploads a one-time retirement floor based on
the migration's season, preserving any later deadline. That separate
nullable timestamp uses eight bytes on legacy upload rows; new uploads do
not receive this floor. Existing verified objects may consequently remain
longer. The extra archive cost depends on the legacy bytes otherwise due
for retirement; this fixture test does not measure a live archive. No
index on per-poll freshness is added.

Migration 0029 records the settled upload attempt as a UUID and its retry
disposition. The values use up to 17 bytes per upload, approximately
1.7–6.8 MB/day at the same modeled rate, before tuple overhead. They let an
uncertain commit be reconciled without accepting a stale or different
upload attempt. No extra index or retained-response deletion is introduced.

The durable last-success metric adds one eight-byte timestamp to each
existing endpoint state row, approximately 0.3 MB at 12,500 players and
three endpoints, before tuple overhead. It creates no per-fetch row or
freshness index. Later errors cannot erase the last successful fetch time.

## Separate go-live work

Issue #110 explicitly requires a separate go for:

1. Daily backups, continuous WAL archival, and a proven scratch restore.
2. Production Google/Discord credentials and both real login flows.
3. A full real Legend day through 05:00 UTC, with agreed keys/cost limits,
   EOD comparisons, and measured database, swap, disk, and archive growth.
4. Alerts for stalled collection, disk/spool pressure, and repeated service
   restarts.

Live Reset accuracy, official league-history star meanings, the live
storage budget, and launch readiness are not proved by fixture tests. Old
GCS/B2 buckets are outside this work and remain untouched. Reset redesign
and automatic season finalization remain separately scoped work.
