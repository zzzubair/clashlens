# Domain Rules

## Legend I Ranked Day

- A Legend I ranked day starts at **05:00 UTC** and ends at **05:00 UTC** the next day.
- A player can have up to 8 attacks and 8 defenses in one ranked day.
- Store and calculate time in Coordinated Universal Time (UTC).
- Reset settlement behavior after the 05:00 UTC boundary still needs a later specification based on observed API behavior.

## Official API Observations

- The official battle-log API returns up to the latest 50 battles.
- One response can mix `legend`, `ranked`, and `homeVillage` battles.
- Legend I rows use `battleType: "legend"`.
- Battle rows include an attack-or-defense flag and a `battleTimestamp`.
- Battle rows include stars, destruction, opponent data, and `armyShareCode`.
- Battle rows do not include the trophy change directly. Clash Lens derives it from the battle result using a versioned trophy-allocation rule.
- Repeated polls overlap. Clash Lens must not create duplicate battle events from repeated observations.

## Player Tags and Tracking

- Accept valid player tags submitted by users.
- Discover additional tags only through official Clash of Clans API sources, such as leaderboards and clan data.
- Do not scrape competitor services for player tags or player data.
- Normalize player tags at system boundaries.
- Start tracking a valid tag when Clash Lens first observes it.
- Reconstruct all retained timestamped Legend I events available at first observation.
- Mark history before the first reliable observation as partial or unavailable. Do not invent missing history.
- Preserve each raw source observation.
- Give each battle a stable identity and make ingestion idempotent.

## Legend I Meta Analytics

- Preserve the raw `armyShareCode` from each battle observation.
- An **army composition** is the exact decoded set of units represented by an `armyShareCode`.
- An **army archetype** is a versioned and reproducible classification derived from an army composition.
- **Army usage rate** is the share of unique Legend I attacks in a stated trophy range and time period that belong to an army archetype.
- Count unclassified attacks in an explicit `Unclassified` category.
- **Three-star rate** is the share of attacks assigned to an army archetype that achieved three stars.
- The **defense lens** groups attacks by the defender’s trophies at battle time.
- The **offense lens** groups attacks by the attacker’s trophies at battle time.
- Do not substitute current or season-end trophies for battle-time trophies without labeling the value as an estimate.
- Do not count the same battle twice when it appears in both the attacker’s and defender’s battle logs.
- Every aggregate must state its trophy range, time period, observed sample size, measured coverage, freshness, and classification version.
- Do not silently exclude malformed, partial, or unclassified observations. Show their effect on coverage and confidence.

## Data Confidence

- An **exact event** is a valid timestamped Legend I battle observation from the official Clash of Clans API.
- A **reconciled ranked day** satisfies `final trophies = start trophies + attack gain - defense loss`, including any explicit settlement adjustment.
- A **complete ranked day** is reconciled and has enough evidence to show that no relevant event or settlement adjustment is missing.
- Timestamps allow exact event attribution, but timestamps alone do not prove that a ranked day is complete.
- If Clash Lens cannot prove completeness, mark the ranked day as partial or uncertain and preserve the reason.
- Every applicable surface must use the same confidence meanings.

## Other Ranked Leagues

Ranked leagues below Legend I are outside Phase 1. Their rules must be specified before Clash Lens applies the tracking and analytics model to them.
