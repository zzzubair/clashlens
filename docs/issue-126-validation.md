# Unit, quantity and outcome history

## Contract and deletion effects

Historical army summaries retain usage. Destruction and unit-to-unit
relationships belong to current battle analytics. Current battle evidence keeps
unit IDs, quantities and source information. Historical army summaries keep one
ending usage count per namespace-qualified unit ID and quantity, with the
whole-season usable-battle count as its denominator. They also keep 1★, 2★ and
3★ counts for that unit and quantity. Five copies in one army contribute one
using battle with quantity five. Battle-time trophy values are not retained.

Clan-castle contributions, including spells and siege machines, are excluded
from historical usage. No compositions, hero assignments, unit co-occurrences
or destruction are retained in new army summaries. Current analytics and
player trophy history are unchanged. The new projection stores all troop IDs
in one namespace, classifies troop versus siege at read time, and withholds
unclassified troop IDs until the maintained catalogue identifies them. Other
unknown namespaces remain explicit. Names are never stored in new summaries.

Migration 0035 adds a nullable `unit_usage` column and deletes no data. New
summaries leave the old `result_rows` empty. Rebuilding a nonfinalized season
replaces old per-category outcome rows and removes that season/lens's obsolete
combination and clan-castle categories. Already finalized legacy summaries remain
untouched, but their API reads are unavailable because their lost IDs,
quantities and outcomes cannot be reconstructed. They are not presented as the new
history format. Rebuild eligible seasons before finalizing them. No production
migration, deployment, deletion, or archive-retention change was performed.

Existing bounded detail retirement still deletes daily logs, battle facts and
unreferenced battle evidence only after verifying all required summaries. Player
daily trophy entries and season totals are not altered. Retained unit JSON has a
512 KiB per-category limit; exceeding it fails the summary transaction and blocks
finalization rather than truncating counts. API reads use 200-row pages so a
readable retained category cannot exceed the fixed response limit through JSON
expansion. Page identity changes when numerical results change, including later
classification; a plain rename changes only display text.

## Behavioral checks

`test_army_history.py` proves quantity summation, one using battle per unit,
whole-season denominators, 1★/2★/3★ counts, namespace overlap, later troop/siege
classification, and exclusion of known and unknown clan-castle contributions.
A 15,000-battle fixture retains 75,000 unit uses across 500 unit/quantity rows;
the retained category must remain below 524,288 bytes and its 200-row API page
below 1,048,576 bytes.

`test_id_history_postgres.py` traces raw ingestion through both player pages
and analytics. It covers reports in either order, a missing side, disagreeing
outcomes, repeated polls, correction and worker restart. The lifecycle test
rolls back and retries summaries and retirement, removes every raw fixture body,
then names IDs and reads all six historical categories through the signed API.
It checks quantities and star counts after cleanup. Three players' 84
daily entries and all trophy totals remain identical. Separate tests cover plain
renames, unavailable legacy summaries, and a five-of-ten whole-season usage rate.

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

The PostgreSQL test reports row and allocated bytes for the current shape instead
of fixing a measurement in this document. The earlier trophy-bearing figures
are superseded by the unit/quantity v3 representation. The fixture is
intentionally repeatable, not representative of production unit or quantity
diversity. Growth depends on distinct unit/quantity groups. The separate
15,000-battle fixture proves 75,000 uses remain within the enforced 512 KiB
per-category limit and that each 200-row API page remains below 1 MiB.
WAL, backups, raw archive bytes, at-scale indexes and realistic diversity remain
unmeasured; no production capacity claim is made.

## Validation

The original v1 validation used only the task-owned copy
`/tmp/clashlens-126-history.ljZXzqnE` on Rogue and uniquely named rootless fixture
containers. The final targeted run passed 47 PostgreSQL/usage tests. A subsequent storage
fixture broadened quantity diversity and passed independently.
All 312 website unit tests, lint and type checks passed. Three history browser
tests passed against the physical restore, including accessibility checks.

A fresh PostgreSQL physical base backup was restored into a separate container.
All 12 historical army responses and both player summaries matched exactly.
Battle and army-fact tables were empty. After backup the source totals were
changed by 100; the restored total remained two. This checks the new retained
representation, not remote WAL-G/R2 recovery or seven-day recovery qualification.
The final-source `./dev check` passed: 789 Python tests passed with three
existing suite skips, four fixture tests passed, all 312 website unit tests
passed, and 17 browser tests passed. The one browser skip is the optional
restored-history scenario, which passed in the separate three-test run. Lint,
type checking, production builds and the browser asset boundary check passed.
Those checks predate the whole-season denominator and unit/quantity v3 fix and
are not evidence for the current representation.
The separate restore containers and browser session were stopped afterward.
