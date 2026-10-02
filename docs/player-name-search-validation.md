# Home-page player name search validation

Measured on October 2, 2026. The query change preserves case-insensitive literal
substring matching, current accepted profiles, active-or-historical membership,
`lower(name), normalized_tag` ordering, and the caller's limit. It does not change
the website's five-second timeout, add an index or dependency, or require a migration.
Nothing was deployed.

## Cause and change

The old production plan built a set of qualifying players by reading the entire
daily-history table before joining the matching names. Its first timed plan spent
7,652 ms scanning daily logs, including decoding stored battle arrays. It also
searched historical profile names that cannot appear in the results.

The new query uses the existing profile primary key to read each player's current
profile, as the Live Leaderboard already does. `OFFSET 0` keeps that lookup from
being flattened into a scan of historical profiles. A materialized `matches`
query establishes the name matches before history is considered. Each history
subquery returns at most one boolean, preventing PostgreSQL from replacing it
with a scan of the entire history table. Active players need no history lookup.
There is no retained-data growth or new database storage cost.

## Production read-only measurement

Connected through `ssh fedora`, confirmed hostname `rogue`, and executed SQL in
the running `clashlens-postgres` container. Every production transaction was
read-only with a 30-second statement timeout; search measurements used
`SET LOCAL ROLE clashlens_python_api`. No production tables, settings, services,
or deployed application files changed.

The initial inventory contained 23,816 players, 13,263 active players and 11,830
current profiles. Discovery held 9,210,695 rows; official league history held
111,216 rows. Estimated profile-version and daily-log counts were about 237,000
and 240,000. Collection continued during the investigation; the final snapshot
had 11,838 current profiles. Discovery is not read by either search query.

The final comparison used `EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON)`
inside one repeatable-read snapshot. Disabling per-node timing avoids stopwatch
overhead on thousands of short indexed reads; total execution time is still
measured. These are SQL execution times, excluding connection, signing, account
search and website/network time. Existing production traffic continued.

The old `hiroya`, limit 5 query took **7,995.214 ms**. Five samples per query with
the new SQL measured:

| Query | Median | Minimum | Maximum |
| --- | ---: | ---: | ---: |
| `hiroya` | 75.438 ms | 64.993 ms | 118.482 ms |
| `hi` | 69.675 ms | 64.666 ms | 70.436 ms |
| `a` | 75.334 ms | 73.667 ms | 84.267 ms |
| `no-such-name-6789` | 70.568 ms | 65.659 ms | 74.595 ms |

The old plan scanned 216,782 qualifying daily-log rows and 111,216 league-history
rows. The new `hiroya` plan made 11,838 current-profile primary-key lookups and
zero history lookups, because all three matches were active. One warm sample
read zero blocks from storage, compared with 94,301 in the old plan.
Full result rows, including their ordering, were equal between old and new SQL
for `hiroya`, `hi`, and `a` in the same snapshot. No cache flush was attempted.

## Local production-size measurement

Used a disposable PostgreSQL 18.6 cluster inside this worktree and the real
production migrations. Synthetic data contained 23,816 players with 13,000 current
profiles, 12,000 active players, 236,680 profile versions, 240,366 daily logs,
111,216 league-history entries and **9,210,695 discovery records**. Each daily
log had 16 synthetic battle objects. Season summaries were empty, as in production.
No production player records were copied. The discovery table and its indexes
occupied 1.61 GB temporarily; the search does not touch them.

After `ANALYZE`, ran each old/new query three times with the same explain options
and API role as the production comparison. Full ordered rows matched for every
query. Medians were:

| Query | Old SQL | New SQL |
| --- | ---: | ---: |
| `hiroya` | 1,302.020 ms | 36.907 ms |
| `hi` | 1,309.947 ms | 33.206 ms |
| `a` | 1,134.196 ms | 70.549 ms |
| `no-such-name-6789` | 1,357.105 ms | 45.479 ms |

All 12 new-query samples were 31.974–88.763 ms. The first old `hiroya` sample was
2,927.016 ms; no cache eviction separated runs. This synthetic workload is not
identical to production battle JSON or concurrent collection. Setup, seeding,
measurements and schema cleanup completed in 246.74 seconds. A first local attempt
failed in the benchmark's discovery insert because its modulo operator needed
parameter escaping; the corrected run above completed without errors.

## Correctness checks and limits

The focused PostgreSQL/application run covered `test_player_lookup_postgres.py`,
`test_api_db_public_ops.py`, and `test_private_api.py`: **37 passed**, no skips,
in 38.51 seconds. The new cases exercise inactive players admitted by each history
source, ineligible-only empty history exclusion, filtering before limits, tied
names, mixed-case substring matching, literal `%`, `_` and backslash, Unicode,
and exclusion of old names. Existing tests check response fields and the signed
private search endpoint. After expanding the membership case to reject an
unaccepted profile and a player without a current profile, that case passed again
in 2.66 seconds, with 19 other tests deselected. Ruff and `git diff --check` passed.

The changed application was not deployed, so successful browser suggestions and
the new signed endpoint's end-to-end latency remain unverified. The complete
suite, hosted checks and no-mistakes validation remain for the delivery pipeline.
Measurements cover one reader during ordinary production traffic, not concurrent
search load, a cold database restart, or substantially more current players.
