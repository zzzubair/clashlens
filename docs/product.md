# Product Scope

## Ownership

This file owns the product mission, Phase 1 scope, user-facing terms, capabilities, product rules, out-of-scope work, and open product specifications.

- [`docs/domain.md`](domain.md) owns exact meanings for time, players, observations, events, adjustments, snapshots, cohorts, analytics, and confidence states.
- [`docs/architecture.md`](architecture.md) owns the accepted runtime shape, implementation boundaries, security rules, and open technology choices.

Preserve the distinction between product scope, domain rules, and implementation choices.

## Mission

Clash Lens democratizes competitive Clash of Clans ranked data. It brings scattered observations together so players can make evidence-led decisions instead of relying on isolated logs, opinions, or guesswork.

Clash Lens provides data and analysis. Users choose their armies, bases, and actions.

## Phase 1 boundary

Phase 1 supports Legend I. Its north star is that a Legend I player should not need another data product to compete effectively. The product is global and English first. Ranked tournaments other than Legend I come after Phase 1.

The scope is final. The implementation-level details that remain open are listed in [Open specification work](#open-specification-work).

### Functional beta restrictions

The functional beta accepts Google authentication only. Discord login, the Discord bot, and exports are disabled by default. A player link already owned by another account does not move automatically. It requires fresh verification and a restricted audited support action.

## Primary user

The primary user is an individual competitive rank pusher. The same public data supports creators, players with several accounts, clans, friends, and followers.

## Product terms

### Public and account terms

- A **player page** is a public web page for one Clash of Clans player tag.
- A **Clash Lens account** is an optional profile authenticated through Discord or Google.
- Every Clash Lens account has one required unique **username** and one required **display name**. Display names do not need to be unique.
- A **user page** is a public web page identified by the account's username. It shows the account's display name and every verified player account linked to that Clash Lens account.
- A **saved player tag** is a public player tag added to a Clash Lens account for convenience. Saving a tag does not prove ownership of the game account.
- A **linked player account** is a player tag verified through the official `POST /players/{playerTag}/verifytoken` endpoint with the one-time player API token from the game. The link records verified control at the time of verification.
- Every linked player account is public on the account's user page. Phase 1 has no per-account visibility controls.
- One player tag can link to only one Clash Lens account at a time. During beta, moving it requires fresh player-token verification and a restricted audited support action.
- Clash Lens never requests a player's game login credentials. It does not retain or log the one-time player API token after its verification request finishes.
- A **multi-account view** summarizes the verified player accounts linked to one Clash Lens account and links to each player page.
- A **group** is a named set of player tags organized by a Clash Lens account.

### Domain terms

[`docs/domain.md`](domain.md) owns the exact definitions and rules for the Tracked Players leaderboard, official rank, frozen and live leaderboards, inferred shielded days, rank bands, rank streaks, and offense and defense lenses. Product capabilities use those terms without redefining them here.

## Phase 1 capabilities

### Legend I coverage and discovery

- Accept any valid player tag submitted by a user.
- Discover additional player tags only through official Clash of Clans API sources, such as leaderboards and clan data.
- Track as much of the Legend I population as official sources allow. Show measured coverage and gaps. Claim full-league coverage only when Clash Lens can prove it.
- When Clash Lens first sees a valid tag, retain it as a known player and show the available profile and retained Legend I observations immediately.
- Begin regular Phase 1 battle collection after confirming that the player is in Legend I. Mark earlier coverage as partial or unavailable.

### Legend I meta analysis

- Preserve and decode the `armyShareCode` from timestamped Legend I battles.
- Keep the exact decoded army composition and classify it into a versioned army archetype without discarding the raw source value.
- Let users analyze army usage and three-star rate for a trophy range, cumulative Tracked Players leaderboard cohort, rank band, or rank-streak cohort within a stated time period.
- Offer cumulative frozen-snapshot presets for Top 10, Top 50, Top 100, Top 200, Top 500, Top 1,000, Top 2,000, Top 5,000, and Top 10,000 tracked players.
- Let users select rank bands such as ranks 51–100 or ranks 201–1,000.
- Let users list players who remained within a selected Top-N cohort or rank band for a consecutive period. The default period is 7 days.
- Exclude a player with a stale, missing, or uncertain entry on any required day from the confirmed rank-streak list.
- Count an inferred shielded day toward a rank streak when the player's fresh frozen-snapshot rank remains within the selected cohort or band. Show how many days in the streak were inferred shielded days.
- Let users inspect the observed offense armies, offense outcomes, defenses received, and defense outcomes for the rank-streak cohort over the same period.
- Use rank streaks when the question is which players remained consistently within a rank selection. A changing Top-N population is not one multi-day cohort.
- Offer a trophy-centered view from 5 trophies below through 5 trophies above a selected value.
- Keep offense and defense perspectives separate.
- Use observed attack patterns and outcomes to support army-selection and base-selection decisions.
- Show sample size, measured coverage, freshness, classification confidence, and unclassified observations with every aggregate.

### Personal tracking

- Track each player's timestamped daily attacks and defenses.
- Preserve a fully observed zero-event day as an inferred shielded day. Keep it separate from a missing day.
- Show inferred shield status and its evidence separately from exact battle events.
- Make the current season's day-by-day log the primary player-page view. Show the active day as a live row.
- Label the active ranked day **Live**. After the day ends, label it **Complete** only when trophy movement and required evidence reconcile. Otherwise label it **Partial** and state the missing or uncertain evidence.
- Let users expand a ranked day to inspect its timestamped attacks, defenses, opponents, results, army data, and confidence.
- Add late evidence to the affected ended day. Rebuild only that day and dependent summaries. Publish a corrected version of any changed frozen result instead of silently replacing it.
- Preserve and expose every ranked season collected by Clash Lens.
- Show the first tracked day and all known coverage gaps.

### Public discovery

- Provide a public player page for each tracked player tag without requiring a Clash Lens account.
- Show the latest saved player data immediately with its observation time and freshness.
- Let a user request a live refresh without blocking the initial page. Update the page after the new observation has been processed.
- Provide one public Tracked Players leaderboard for the actively tracked Legend I population.
- On that leaderboard, show the official top-200 rank as a separate provenance-backed field when the official Clash of Clans API supplies it.
- Maintain the most recent complete official global Top 200 from the official API. Replace it only after one newer response contains 200 unique valid normalized player tags and valid ranks 1 through 200 once each.
- Keep the prior complete official Top 200 available when a refresh fails or is partial. Show the failed refresh state.
- Show the official observation time. The official response has no season identifier. A derived Legend I season label is not an official API field.
- Describe only the source-backed positions as official. Clash Lens positions are not official positions.
- Keep serving the previous frozen leaderboard until its replacement publishes as one internally consistent snapshot version.
- Show missing, partial, stale, or uncertain entries within a published snapshot. Atomic publication does not prove complete coverage.
- Offer a separately labeled live view based on newer observations.
- Show tracked population, measured coverage, source provenance, snapshot time, and freshness. Link entries to player pages.
- Keep the latest accepted Tracked Players ordering available when a refresh is delayed or fails. Keep entries that use older valid trophy observations in the ordering and label their observation time, age, and freshness. Update the ordering when newer valid observations arrive.
- Provide a public user page for each Clash Lens username. Show its non-unique display name and all player accounts currently linked through successful player-token verification.

### Accounts and groups

- Offer optional Clash Lens accounts authenticated through Discord or Google.
- Let one Clash Lens account link both a Discord identity and a Google identity. Each provider identity can belong to only one Clash Lens account.
- Let one Clash Lens account save multiple player tags.
- Provide one multi-account summary page for verified linked player accounts, with links to each public player page.
- Link player accounts only after successful official player-token verification. One player tag can link to only one Clash Lens account at a time. During beta, moving it requires fresh verification and a restricted audited support action.
- Let users modify only their own saved tags, linked player accounts, groups, and preferences.
- Let account users create named groups of player tags for easy tracking.
- Use authentication to organize public data and preferences. It does not unlock hidden player data.

### Product surfaces

- Use the website as the primary product surface.
- Keep Discord access disabled during beta. The exact Discord workflow remains a later specification decision.
- Keep Google Sheets exports disabled during beta. Define export formats and enablement after beta.
- Provide an OBS browser overlay as a creator-facing growth and publicity surface.
- Keep shared data meanings, confidence states, and freshness consistent across applicable surfaces.

## Product rules

### Trust and evidence

- Clash Lens must be trustworthy.
- Treat event accuracy, ranked-day reconciliation, and ranked-day completeness as separate claims.
- Mark a ranked day **Complete** only when stored events and final trophy balance reconcile and available evidence shows that no relevant event or settlement adjustment is missing. Apply the exact evidence rules in [`docs/domain.md`](domain.md).
- Show partial, stale, missing, malformed, or uncertain data explicitly.
- Do not overstate accuracy, coverage, completeness, or classification confidence.

### Evidence, not prescriptions

- Provide observed data and reproducible analysis so users make their own decisions.
- Keep army-selection and base-selection decisions with the user. Do not prescribe a specific army or base.
- Keep opinion separate from measured evidence.

### Free access and monetization

- Keep all of Clash Lens free.
- Keep public player data and public analytics available without sign-in.
- Keep authentication for personal organization and preferences; it may be required to store them. Authentication does not unlock hidden player data.
- Keep subscriptions, premium tiers, paywalls, and paid feature gates out of the product.
- Monetization remains an open choice only if it does not restrict public access and complies with the current Supercell Fan Content Policy and API terms.

### Sources and Supercell policy

- Use official Clash of Clans API sources and user-submitted tags for player discovery.
- Keep competitor-service scraping outside the product.
- Comply with the current Supercell Fan Content Policy and API terms.
- Keep required fan-content notices on applicable public surfaces. Do not imply Supercell endorsement.
- Provide no delegated or shared game-account access. Account sharing violates Supercell's terms. Clash Lens verifies only that the current user can supply the account's one-time player API token.

## Out of scope for Phase 1

- Ranked tournaments other than Legend I.
- War, Clan War League, clan-management, and base-management systems.
- Prescriptive “use this army” or “use this base” recommendations.
- Paid access to player data or analytics.
- Competitor-service scraping to seed player tags or copy player data.
- The TypeScript framework and the remaining infrastructure-product, cloud-provider, and implementation choices that remain open in [`docs/architecture.md`](architecture.md).

## Open specification work

The product scope is final. These implementation-level details remain for later specifications and issues:

- Time-period defaults other than the confirmed 7-day rank-streak default.
- Rank-band input limits and interaction design.
- The full metric set and visualization design for meta analysis.
- Army-share-code decoding and army-archetype classification rules.
- Exact player-page, leaderboard, multi-account, and group interactions.
- Discord commands and response formats.
- Google Sheets export formats and refresh behavior.
- OBS overlay layouts and creator workflows.
- Username syntax and case normalization, profile editing, account deletion, player-account unlinking, and verification-audit retention.
- Product success measures and validation thresholds.
