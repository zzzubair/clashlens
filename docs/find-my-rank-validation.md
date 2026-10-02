# Find your rank validation

Validated on October 2, 2026 with synthetic players. The signed search keeps
whole-board ranks and hidden-player exclusions; selection locates the current
page, highlights the player and includes neighbors across page boundaries.
Database tests, website tests and browser checks passed in the validation
pipeline. The published version also passed hosted checks (5 passed, 1 skipped).

At 13,000 players, 12 warm-cache local samples measured median name search at
64.51 ms, selected-page lookup at 66.55 ms and the ordinary first page at
68.33 ms. Both new reads use the current-profile primary-key index and rank
the current board without scanning historical profiles. These measurements
predate the page-edge neighbor adjustment; they do not measure concurrent
production load.

Evidence is hosted outside the repository:

- [Name search at 375 px](https://files.zubairshaik.net/public/agent-files/clashlens-pr-183-evidence/name-search-375.png)
- [Selected row at 375 px](https://files.zubairshaik.net/public/agent-files/clashlens-pr-183-evidence/selected-rank-375.png)
- [Selected row on desktop](https://files.zubairshaik.net/public/agent-files/clashlens-pr-183-evidence/selected-rank-desktop.png)
- [Name search query plan and samples](https://files.zubairshaik.net/public/agent-files/clashlens-pr-183-evidence/name-common-plan.json)
- [Selected-page query plan and samples](https://files.zubairshaik.net/public/agent-files/clashlens-pr-183-evidence/focus-row-plan.json)

Screenshots show the initial implementation before the parallel layout update
and review fixes, including the later correction to highlight only the chosen
row. Later browser checks cover page-edge neighbors, tag/name matching and Back
navigation. Physical phone keyboard/touch behavior was not tested. Nothing was
deployed.
