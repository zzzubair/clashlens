# Find your rank validation

Measured on October 2, 2026 in the isolated `fm/cl-find-my-rank` worktree.
The feature uses the private signed API and the existing Live Leaderboard
membership, trophy ordering and tag-hash tie rule. Name matches keep their
whole-board ranks. Only a tag typed with `#` opens a row directly, regardless of
letter case; without `#`, the tag's player is listed alongside name matches, and
a `#` search matching no tag falls back to names.
A selected player within 5 ranks of a page edge also gets up to 5 neighbors
from the next or previous page. Selecting a player recalculates their page and entries in one
database statement, so an intervening rank change cannot open the wrong page.

## Correctness checks

- Python: 4 new database/API tests passed, plus all 17 tests in
  `test_api_db_public_ops.py` and `test_private_api.py`.
  New cases cover 105 equal-trophy players, rank 103 on page 2, movement to rank
  1 before selection, literal `%` and `_` in names, empty results, bounded
  results, exact-tag precedence, hidden/not-found players and their restoration,
  inactive and unaccepted players, signed-request enforcement and invalid input.
  These counts predate the review changes, which added a fifth Python test for
  page-edge neighbors, replaced exact-tag precedence with `#`-only tag selection
  and name fallback, and added website client cases; this record does not
  re-count them.
- Website: 75 tests passed across `python-client.test.ts`, `home-route.test.ts`,
  `leaderboard-search-client.test.ts` and `leaderboard-search-route.test.ts`.
  New cases cover the signed search request, preservation of rank 103, invalid
  responses, direct tag redirect, name selection, recalculated page, disappeared
  player explanation and search failures.
- Type checking, ESLint, formatting of changed website files, Python Ruff,
  `git diff --check`, and the production build/browser asset check passed.
  The build emitted the existing `client-address.server.ts` mixed static/dynamic
  import warning. No browser console messages appeared during the checks.
- Added three browser tests in `website/tests/e2e/leaderboard-search.spec.ts` for
  the standard fake-service stack, including screenshot attachments. Those
  automated browser tests were not run locally because Podman is unavailable;
  they remain for the repository's CI run. The real-page checks below were
  performed through `chrome-devtools-axi` against the production website build,
  actual signed Python API and disposable PostgreSQL database.

## Query cost at 13,000 players

PostgreSQL 18.6, loopback connection, 13,000 synthetic active players with unique
current profile versions, seeded using the existing `seed_profile` test helper.
Tags used four characters from the valid tag alphabet; trophies were
`6500 - index // 20`, giving groups of 20 ties. Names were `Nova <tag>`.
`ANALYZE` ran after seeding. Twelve sequential calls per case measured complete
Python database-function elapsed time; these are warm-cache local measurements,
not concurrent production throughput or network latency claims.

| Request | Median | Maximum |
| --- | ---: | ---: |
| Common name `Nova` | 64.51 ms | 72.75 ms |
| Unique name `Nova #PVP8` | 64.57 ms | 73.53 ms |
| Name with no match | 65.52 ms | 74.77 ms |
| Exact tag `#PVP8` | 61.63 ms | 65.42 ms |
| Open selected player's page | 66.55 ms | 72.83 ms |
| Ordinary first page | 68.33 ms | 91.71 ms |

Both new reads reuse `player_profile_versions_pkey`: 13,000 index lookups,
one current profile per player. The query scans and sorts the current board to
calculate absolute ranks, then filters it; there is no new name index and no
scan through historical profile versions. No database migration, persistent
search cache or growing storage was added. Results are capped at 20 with an
explicit prompt to narrow the search. The existing not-found state lookup and
exclusion rule are shared with the board.

Full `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` output and timing samples:
[name search](name-common-plan.json), [selected page](focus-row-plan.json).
The plans show zero shared disk blocks read for these warm-cache samples.

## Browser evidence

At 375 × 812, searching for `Nova #PVP8` showed rank **6,450** and **6,178**
trophies. Selection opened page **65**, focused and highlighted the player row,
and scrolled it to vertical coordinates 372–441 inside the 812 px viewport.
Ranks **6,449** and **6,451** remained adjacent. Document width was 375 px and
the search input font was 16 px. Both `pvp8` and `#PVP8` jumped directly to that
row in this run, before bare tags stopped opening rows directly; the edge-of-page
neighbors were added afterwards and are not shown in these screenshots. Reopening the selected URL preserved the scroll behavior; browser Back
restored the name query and result. A missing name showed the empty message
while keeping 100 board rows. Desktop selection was checked at 1280 × 900.

- [Name result at 375 px](name-search-375.png)
- [Selected row and neighbors at 375 px](selected-rank-375.png)
- [Selected row and neighbors on desktop](selected-rank-desktop.png)

Physical phone keyboard/touch behavior, concurrent production load and the
full fake-service browser suite were not checked locally. No deployment was
performed. Parallel leaderboard wording/layout work had not landed when the
implementation was completed; these screenshots show the starting wording.

## Change size

The change adds a signed search endpoint, shared ranking and page-location
logic, server response validation, the search controls and focused-row styling.
It removes the old page-only rank calculation. Most additional lines are
behavior tests and this requested evidence. There is no obsolete search feature
to delete. The existing 1,516-line client test file was left unchanged; new
client tests have their own small file.
