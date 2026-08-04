# Legend I Season Anchor and Current API Values

**Research date:** 2026-08-04
**Status:** The timing rule is supported by official Supercell material. Current field names and numeric values were characterized once through a maintainer-approved live API check; no raw player response was retained.

## Decision

Use the official player-profile `currentLeagueSeasonId` as the current Legend I season anchor. The safely characterized official profile route supplied this value:[4]

```text
currentLeagueSeasonId = 1783918800
2026-07-13T05:00:00Z
```

The same Legend I profile supplied:

```text
previousLeagueSeasonId = 1781499600
2026-06-15T05:00:00Z
```

The values are exactly 28 days apart. Both are Mondays at 05:00 UTC. This agrees with the official support rule that Legend I tournaments reset every four weeks on Monday at 05:00 UTC.[1][2]

The maintainer separately confirmed that the current season started at `2026-07-13T05:00:00Z` and that ranked day 1 ended at `2026-07-14T05:00:00Z`. This corroborates the live API value and the official day-boundary rule. It is not used as the authority for a universal schedule.

Use `2026-07-13T05:00:00Z` as the bootstrap anchor for version 1. Derive earlier or later season boundaries by exact 28-day intervals. Preserve the official integer field and the derived UTC timestamp.

## Live API characterization

A maintainer-approved check used an existing protected collector key through the approved fixed-egress path. It did not print or save the key, proxy address, player tag, player name, clan, or response body.

The check selected one current official global Top-200 entry, fetched that player's official profile in memory, and printed only safe field names and season values. It observed:

- `leagueTier.id = 105000036`;
- `leagueTier.name = Legend I`;
- `currentLeagueSeasonId = 1783918800`;
- `previousLeagueSeasonId = 1781499600`;
- the older `league` object was `Unranked` in both the ranking entry and selected player profile while `leagueTier` was `Legend I`.

The numeric season values converted to UTC as follows:

```text
1783918800 -> 2026-07-13T05:00:00Z
1781499600 -> 2026-06-15T05:00:00Z
```

Their exact difference is `2,419,200` seconds, or 28 days.

## Domain contract

### Season identity

- Store the official `currentLeagueSeasonId` and `previousLeagueSeasonId` unchanged as source values.
- For adapter version 1, validate each value as Unix seconds and derive its UTC timestamp.
- Require a valid season boundary to fall on Monday at 05:00 UTC.
- Require adjacent season IDs to differ by exactly 28 days.
- Treat the interval starting at the current season ID and ending 28 days later as the current Legend I season.
- Use half-open intervals: the season includes its start boundary and excludes its end boundary.
- Number ranked days 1 through 28 from the season start. Each ranked day is one half-open 24-hour interval starting at 05:00 UTC.

### Updating the anchor

- Bootstrap version 1 with `1783918800` (`2026-07-13T05:00:00Z`).
- Accept a later anchor only from a valid official profile that is currently in Legend I and passes the weekday, time, and 28-day checks.
- Require other accepted Legend I profiles to agree. A conflicting official value is a visible source-contract conflict; keep the last confirmed anchor and do not silently select one value.
- Keep the anchor rule version with each derived season, ranked day, boundary adjustment, snapshot, and analytic result.

### Legend I membership

- Use `leagueTier.id = 105000036` as the one-time characterized Legend I membership value for adapter version 1.
- Store the accompanying official name for display and diagnostics.
- Do not use the older `league` field to decide Legend I membership. The live characterization showed `league.name = Unranked` while `leagueTier.name = Legend I` in the same player's ranking and profile responses.
- Deactivate a player only when the accepted adapter table explicitly recognizes the returned tier ID and name as non-Legend-I. Treat an unknown tier ID or a conflict between a known ID and its expected name as a source-contract change that requires adapter review. Keep the last confirmed eligibility state until review; do not infer deactivation from unknown evidence.

### Boundary adjustments

Official rules also establish these 05:00 UTC boundary changes:[1][2][3]

- League days reset daily.
- Legend I players below 5,000 trophies reset to 5,000 on Monday.
- All Legend I players reset to 5,000 at a four-week tournament start.
- Players below rank 10,000 at the end of Sunday demote to Legend II on Monday.

Store weekly and tournament trophy resets separately from battles and the ended ranked day's performance. Re-evaluate active Legend I membership after the Monday transition.

## Evidence limits

The official league-season list route returned `400 badRequest` for both the characterized response's `league.id` and `leagueTier.id`. This check did not establish a season-list route for the new Ranked system. It is not the anchor source for this contract.

The profile fields were current values in one official response. The characterization response was deliberately not stored in the repository. Production collection must preserve future profile responses through the normal immutable raw-evidence pipeline.

## Sources

[1] https://support.supercell.com/clash-of-clans/en/articles/legend-league-4.html — official Legend I league-day and tournament reset times

[2] https://support.supercell.com/clash-of-clans/en/articles/legend-league-matchmaking-3.html — official four-week Legend I tournament rule

[3] https://supercell.com/en/games/clashofclans/blog/release-notes/the-sound-of-clash-update — official Legend I tournament, demotion, and trophy-reset rules

[4] https://api.clashofclans.com/v1/players/%7BplayerTag%7D — official player-profile route safely characterized on 2026-08-04
