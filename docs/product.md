# Product Scope

## Mission

Clash Lens democratizes competitive Clash of Clans ranked data. It brings scattered observations together so players can make evidence-led decisions instead of relying on isolated logs, opinions, or guesswork.

Clash Lens provides data and analysis. It does not prescribe a specific army or base.

## Phase 1 Goal

Phase 1 supports Legend I. Its north star is that a Legend I player should not need another data product to compete effectively.

Clash Lens combines broad Legend I meta analysis, trustworthy personal tracking, official leaderboard context, and convenient access across several surfaces. It is global and English first.

Ranked leagues below Legend I come after Phase 1.

## Target User

The primary user is an individual competitive rank pusher. The same public data also supports creators, players with several accounts, clans, friends, and followers.

## Terms

- A **player page** is a public web page for one Clash of Clans player tag.
- A **Clash Lens account** is an optional profile authenticated through Discord or Google.
- A **saved player tag** is a public player tag added to a Clash Lens account for convenience. Saving a tag does not prove ownership of the game account.
- A **multi-account view** summarizes the player tags saved to one Clash Lens account and links to each player page.
- A **group** is a named set of player tags organized by a Clash Lens account.
- The **defense lens** analyzes the armies and outcomes seen by defenders in a selected trophy range and time period.
- The **offense lens** analyzes the armies and outcomes produced by attackers in a selected trophy range and time period.

## Phase 1 Capabilities

### Legend I Coverage

- Let anyone submit a valid player tag for tracking.
- Discover additional player tags only through official Clash of Clans API sources, such as leaderboards and clan data.
- Track as much of the Legend I population as official sources allow.
- Show measured coverage and gaps. Do not claim full-league coverage unless Clash Lens can prove it.
- When Clash Lens first sees a valid tag, show the available profile and retained Legend I observations immediately. Mark earlier coverage as partial or unavailable and begin tracking.

### Legend I Meta Analysis

- Preserve and decode the `armyShareCode` from timestamped Legend I battles.
- Keep the exact decoded army composition and classify it into a versioned army archetype without discarding the raw source value.
- Let users analyze army usage and three-star rate by trophy range and time period.
- Keep offense and defense perspectives separate.
- Use observed attack patterns and outcomes to support army-selection and base-selection decisions.
- Show sample size, measured coverage, freshness, classification confidence, and unclassified observations with every aggregate.

### Personal Tracking

- Track each player’s timestamped daily attacks and defenses.
- Make the current season’s day-by-day log the primary player-page view. Show the active day as a live row.
- Let users expand a ranked day to inspect its timestamped attacks, defenses, opponents, results, army data, and confidence.
- Preserve and expose every ranked season collected by Clash Lens.
- Show the first tracked day and all known coverage gaps.

### Public Discovery

- Provide a public player page for each tracked player tag without requiring a Clash Lens account.
- Provide a full public leaderboard based on official Clash of Clans API rankings.
- Show the leaderboard source and freshness, and link entries to player pages.
- Keep public player data and public analytics available without sign-in.

### Accounts and Groups

- Offer optional Clash Lens accounts authenticated through Discord or Google.
- Let one Clash Lens account save multiple player tags.
- Provide one account summary page for saved tags, with links to each public player page.
- Let account users create named groups of player tags for easy tracking.
- Use authentication to organize public data and preferences. Do not use it to unlock hidden player data.

### Surfaces

- Use the website as the primary product surface.
- Provide minimal and easy Discord access. The exact Discord workflow remains a later specification decision.
- Provide Google Sheets exports for analysis and sharing.
- Provide an OBS browser overlay as a creator-facing growth and publicity surface.
- Keep shared data meanings, confidence states, and freshness consistent across all applicable surfaces.

## Product Rules

### Trust

- Clash Lens must be trustworthy.
- Treat event accuracy, ranked-day reconciliation, and ranked-day completeness as separate claims.
- Mark a ranked day complete only when the stored events and final trophy balance reconcile and the available evidence shows that no relevant event or settlement adjustment is missing.
- Show partial, stale, missing, malformed, or uncertain data explicitly.
- Do not overstate accuracy, coverage, completeness, or classification confidence.

### Evidence, Not Prescriptions

- Provide observed data and reproducible analysis so users can make their own decisions.
- Do not prescribe a specific army or base.
- Do not present opinion as measured evidence.

### Free Access and Monetization

- Keep all of Clash Lens free.
- Do not add subscriptions, premium tiers, paywalls, or paid feature gates.
- Do not require a Clash Lens account to view public player data or public analytics.
- Authentication may be required to store personal organization and preferences.
- Monetization remains open, but it must not restrict access and must comply with the current Supercell Fan Content Policy and API terms.

### Supercell Policy

- Comply with the current Supercell Fan Content Policy and API terms.
- Do not imply that Supercell endorses Clash Lens.
- Include all required fan-content notices on applicable public surfaces.

## Out of Scope for Phase 1

- Ranked leagues below Legend I.
- War, Clan War League, clan-management, and base-management systems.
- Prescriptive “use this army” or “use this base” recommendations.
- Paid access to player data or analytics.
- Scraping competitor services to seed player tags or copy player data.
- Architecture, hosting, deployment, and implementation choices.

## Open Specification Work

The product scope is final. The following implementation-level details remain for later specifications and issues:

- Exact trophy-range behavior, time-period defaults, and evidence thresholds.
- The full metric set and visualization design for meta analysis.
- Army-share-code decoding and army-archetype classification rules.
- Exact player-page, leaderboard, multi-account, and group interactions.
- Discord commands and response formats.
- Google Sheets export formats and refresh behavior.
- OBS overlay layouts and creator workflows.
- Account, group, preference, and privacy behavior.
- Product success measures and validation thresholds.
