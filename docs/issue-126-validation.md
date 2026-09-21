# Unit, quantity and outcome history

## Contract

[Compact history and retention](history-retention.md#unit-quantity-and-outcome-summaries-issue-126)
owns the current stored, public, deletion and availability rules. This file
records the behavioral and storage evidence for that contract. This change did
not run a production migration, deployment, deletion or archive-retention
operation.

## Behavioral checks

`test_army_history.py` proves quantity summation, one using battle per unit,
whole-season denominators, 1★/2★/3★ counts, namespace overlap, later troop/siege
classification, and exclusion of known and unknown clan-castle contributions.

`test_id_history_postgres.py` traces raw ingestion through both player pages
and analytics. It covers reports in either order, a missing side, disagreeing
outcomes, repeated polls, correction and worker restart. The lifecycle test
rolls back and retries summaries and retirement, removes every raw fixture body,
then reads all six historical categories through the signed API before and after
naming the IDs. Unknown labels are non-null and namespace-specific; unclassified
troop IDs are explicitly ambiguous in both troop and siege views until the
catalogue classifies them. Naming changes labels and classification without
changing retained quantities, uses, denominators or star counts. Three players' 84
daily entries and all trophy totals remain identical. Separate tests cover plain
renames, unavailable legacy summaries, and a five-of-ten whole-season usage rate.
Its PostgreSQL-backed 15,000-battle fixture retains 75,000 unit uses across 500
unit/quantity rows. The stored category must remain below 524,288 bytes, and
each signed 200-row API response must remain below 1,048,576 bytes.

The defender trace deliberately lacks its prior-day automatic-defense basis;
it remains partial and is excluded from completed-day analytics. The offense
trace uses the existing completed-day fixture seam on real reconciled evidence.
This is not a claim that incomplete defense evidence becomes complete.

The website removes the historical-detail fallback and exposes only retained
history controls and columns. Its current-season form preserves `current` when
the API returns a resolved numeric season ID. Browser checks cover missing
summaries and later-named IDs read from a restored retired fixture.

## Storage measurement

The repeatable fixture contains 224 offense attacks, 28 with an unknown army,
five home-troop quantities, four known clan-castle arrangements
and an empty defense lens. It compares the
old 22 label-bearing summary rows against 10 unit-namespace rows with the same
indexes, excluding the new column from the old table.

On PostgreSQL 18, the unit/quantity v3 fixture retained 5,267 row bytes per
season in 10 rows, compared with 15,264 bytes in 22 legacy rows: a reduction of
9,997 bytes (65.5%) per season. Allocated table/index/TOAST space was 49,152
versus 81,920 bytes. These are shared season summaries, not per-player costs.
The later label correction leaves stored bytes unchanged because labels are
resolved only at read time. The earlier trophy-bearing figures are superseded. The fixture is
intentionally repeatable, not representative of production unit or quantity
diversity. Growth depends on distinct unit/quantity groups. The separate
15,000-battle fixture proves 75,000 uses remain within the enforced 512 KiB
per-category limit and that each 200-row API page remains below 1 MiB.
WAL, backups, raw archive bytes, at-scale indexes and realistic diversity remain
unmeasured; no production capacity claim is made.

## Validation

Validation used only the task-owned copy `/tmp/clashlens-126-history.ljZXzqnE`
on Rogue and uniquely named rootless fixture containers. No production data or
services were changed.

The unit/quantity v3 pipeline head `6ca5b058` passed full `./dev check`:
790 Python tests with three existing skips, four fixture tests, 312 website unit
tests, and 17 browser tests. Lint, type checks, builds and the browser asset
boundary check passed. The optional restored-history browser case is skipped
in the generic stack and is checked separately against retained fixture data.

After the unknown-label correction, 23 targeted Python/PostgreSQL tests passed.
The restored-history browser suite passed all three cases with unknown IDs,
covering all six categories, corrected two-star outcomes, usage, partial coverage,
missing summaries and accessibility. All three cases also passed after naming,
including correct troop/siege filtering and unchanged numerical outcomes.

A fresh v3 PostgreSQL physical base backup was restored into a separate
container. All 12 named historical army responses and both player summaries
matched exactly, with zero battle or army-fact rows. Changing source totals by
100 after backup left the restored total at two. With the label correction,
reads against that physical restore again verified all 12 named army responses
and both player summaries, plus readable unknown labels and partial coverage
for both lenses across all six categories. No raw responses were available.
This checks the retained representation and its consumers, not remote WAL-G/R2
recovery or seven-day recovery qualification.
