# Domain Rules

## Legend I Time

- A **Legend I ranked day** starts at **05:00 UTC** and ends at **05:00 UTC** the next day.
- A Legend I season lasts exactly **28 ranked days**.
- Season dates are calculated from a confirmed anchor date. The anchor date still needs to be recorded.
- A player can have up to 8 attacks and 8 defenses in one ranked day.
- Store and calculate time in Coordinated Universal Time (UTC).
- The ranked-day boundary remains 05:00 UTC even when reset processing and snapshot publication finish later.

## Players and Tracking

- A **known player** is a valid, normalized player tag retained by Clash Lens after submission or discovery through an official Clash of Clans API source.
- An **actively tracked player** is a known player currently confirmed to be participating in Legend I and receiving regular Phase 1 collection.
- An **inactive known player** is retained with all existing history but does not receive regular Legend I battle collection.
- The **Tracked Players leaderboard** orders actively tracked players. A position on it means position among players tracked by Clash Lens, not a claim of complete global coverage.
- Use the official API rank wherever Supercell supplies it, including its ordering of equal-trophy players in the official Top 200.
- Beyond the available official ranks, order players by trophies descending and resolve equal-trophy ties with a versioned deterministic hash of the normalized player tag.
- Never use fresh randomness for a snapshot tie-break. The same tag, trophies, and ordering-rule version must reproduce the same position.
- Every snapshot must identify the ordering-rule version it used.
- Top-N eligibility and ordering behavior for stale or missing entries remain open product specifications.
- Begin Phase 1 with the existing set of approximately 12,370 known tags.
- Accept valid player tags submitted by users.
- Discover additional tags only through official Clash of Clans API sources, including official leaderboards, clan data, and opponents present in official battle logs.
- Normalize and deduplicate a tag before adding it to the known-player registry.
- Add a known player to active tracking after confirming that the player is in Legend I.
- When an actively tracked player leaves Legend I, retain the tag and history but remove the player from active Legend I tracking.
- Re-evaluate inactive known players during the Monday promotion and demotion transition and whenever a tag is rediscovered or submitted.
- Retaining inactive tags must allow later ranked-tournament support without re-creating player identity or losing history.

## Official API Observations

- The official battle-log API returns up to the latest 50 battles.
- One response can mix `legend`, `ranked`, and `homeVillage` battles.
- Legend I rows use `battleType: "legend"`.
- Battle rows include an attack-or-defense flag and a `battleTimestamp`.
- Battle rows include stars, destruction, opponent data, and `armyShareCode`.
- Battle rows do not include the trophy change directly. Clash Lens derives it from the battle result using a versioned trophy-allocation rule.
- A battle that produces zero trophies is still an exact event and counts toward the player's attack or defense count.
- Repeated polls overlap. Clash Lens must not create duplicate battle events from repeated observations.
- Preserve every raw source observation, including its fetch time and untouched response body.
- Preserve successful observations when a paired endpoint request fails. Mark the collection attempt incomplete until the missing evidence is collected.
- Start tracking a valid tag when Clash Lens first confirms it for active tracking.
- Reconstruct all retained timestamped Legend I events available at first observation.
- Mark history before the first reliable observation as partial or unavailable. Do not invent missing history.
- Give each battle a stable identity and make ingestion idempotent.

## Inferred Shielded Days

- A **shield** prevents a Legend I player from attacking and prevents other players from attacking them for 1 or 2 ranked days.
- The official API does not directly confirm shield state. Clash Lens may classify a day only as **inferred shielded**.
- Infer a shielded day only when all of the following are true:
  - The player remained eligible for active Legend I tracking.
  - Successful player-profile and battle-log observations provide sufficient coverage across the ranked day.
  - The player's trophies did not change across the ranked day.
  - The battle log contains no Legend I attack or defense event timestamped within the ranked day.
  - No automatic defense adjustment applies.
- A zero-trophy battle is still a Legend I event and prevents the day from being classified as shielded.
- Preserve an inferred shielded day as an explicit ranked-day row with zero attacks, zero defenses, and zero trophy change. Do not omit it or classify it as missing.
- One or two consecutive qualifying days may be labeled as an inferred 1-day or 2-day shield. A longer zero-event sequence is not a valid shield duration under the current rule and must be marked uncertain.
- Shielded days contribute no battle events to offense or defense analytics.

## Automatic Defense Adjustment

- An **automatic defense adjustment** is a reset-time trophy loss applied when a player has fewer than 8 observed defenses in a Legend I ranked day.
- It is a settlement adjustment, not a battle event. It has no opponent, army, destruction result, or battle timestamp.
- There is no automatic offense adjustment.
- Zero-trophy defense events count as observed defenses when determining how many defenses are missing.
- Calculate an automatic defense adjustment only when retained battle-log observations provide enough coverage to establish the defense-event counts and observed event losses for both the previous and current days. If any formula input may be incomplete because collection evidence is missing, do not replace the unknown evidence with an adjustment.
- For a current day with 1 through 7 established defense events, calculate the positive loss magnitude per missing defense as:

  `floor((previous day observed event loss + current day observed event loss) / (previous day defense-event count + current day defense-event count))`

- **Observed event loss** in this formula excludes every automatic defense adjustment.
- Multiply the floored loss by `8 - current day defense-event count` to calculate the total automatic defense adjustment.
- Do not apply this inference rule to a day with zero established defense events. Preserve the evidence and mark the day uncertain if that exceptional case occurs.
- A **calculated automatic defense adjustment** uses the versioned averaging rule when the reset outcome cannot yet isolate the exact adjustment.
- A **confirmed automatic defense adjustment** requires sufficient battle-event coverage plus post-reset trophy evidence that jointly isolate the adjustment. Trophy reconciliation alone does not prove its cause.
- Show the adjustment separately from battle events and identify whether it is calculated or confirmed.
- Automatic defense adjustments affect ranked-day trophy reconciliation but never contribute to army usage, three-star rate, or other battle-event analytics.

## Ranked-Day and Leaderboard Snapshots

- At 05:00 UTC, event ownership moves to the new ranked day. A battle first observed later is still added to the ended day when its timestamp falls before the boundary.
- A **frozen leaderboard snapshot** is the accepted, versioned ordering of actively tracked players at a reset baseline.
- Continue serving the previously frozen snapshot while the next snapshot is assembled. Publish the replacement atomically so users never receive a mixture of snapshot versions.
- Target snapshot publication at approximately 05:05 UTC on normal days, after the daily no-attack matchmaking window.
- Target snapshot publication at approximately 05:10 UTC on Mondays, after the longer promotion and demotion transition.
- A **live leaderboard** uses the latest available observations after the frozen baseline and must be labeled separately.
- Retain each entry's observation time, measured coverage, freshness, confidence, and applicable official rank provenance.
- Publishing at the target time means accepting the best official observations available under those rules; it does not claim that every API response was generated simultaneously or that every entry has equal freshness.
- If later evidence proves a frozen snapshot inconsistent, retain the prior version and publish a corrected version rather than silently rewriting it.

## Legend I Meta Analytics

- Preserve the raw `armyShareCode` from each battle observation.
- An **army composition** is the exact decoded set of units represented by an `armyShareCode`.
- An **army archetype** is a versioned and reproducible classification derived from an army composition.
- **Army usage rate** is the share of unique Legend I attacks in a stated cohort and time period that belong to an army archetype.
- Count unclassified attacks in an explicit `Unclassified` category.
- **Three-star rate** is the share of attacks assigned to an army archetype that achieved three stars.
- For a trophy-range filter, the **defense lens** groups attacks by the defender's trophies at battle time.
- For a trophy-range filter, the **offense lens** groups attacks by the attacker's trophies at battle time.
- For a frozen leaderboard-cohort or rank-band filter, the defense lens includes attacks whose defenders belong to the selected snapshot population.
- For a frozen leaderboard-cohort or rank-band filter, the offense lens includes attacks whose attackers belong to the selected snapshot population.
- For a rank-streak filter, the defense lens includes attacks against players in the resulting streak set during the selected consecutive period.
- For a rank-streak filter, the offense lens includes attacks made by players in the resulting streak set during the selected consecutive period.
- Do not substitute current, snapshot, or season-end trophies for battle-time trophies without labeling the value as an estimate.
- Do not count the same battle twice when it appears in both the attacker's and defender's battle logs.
- Frozen leaderboard cohorts are cumulative and use these presets: Top 10, Top 50, Top 100, Top 200, Top 500, Top 1,000, Top 2,000, Top 5,000, and Top 10,000 tracked players.
- A **rank band** is lower-exclusive and upper-inclusive. The band `(50, 100]` contains ranks 51 through 100, and `(200, 1,000]` contains ranks 201 through 1,000.
- A **rank streak** contains only players who belong to the selected cumulative Top-N cohort or rank band in every frozen daily snapshot of a consecutive period.
- The default rank-streak period is 7 days.
- Do not treat a changing Top-N population as one multi-day cohort. Use a rank streak when analyzing which players remained consistently within a rank selection.
- A stale, missing, or uncertain entry on any required daily snapshot is not eligible for confirmed rank-streak membership. Exclude that player from the confirmed streak.
- An inferred shielded day remains eligible for a rank streak when the player's fresh frozen-snapshot rank is inside the selected cumulative cohort or rank band. Record the shielded-day count with the streak.
- A selected trophy range includes battle-time trophy values from 5 below through 5 above the selected value, inclusive.
- Trophy ranges, cumulative cohorts, rank bands, and rank streaks are alternative population filters unless a future specification explicitly defines their intersection.
- Every aggregate must state its population filter, time period, observed sample size, measured coverage, freshness, classification version, classification confidence, unclassified count, and analytics-rule version.
- Do not silently exclude malformed, partial, zero-trophy, or unclassified observations. Show their effect on coverage and confidence.
- Preserve previously published analytics with their original rule labels when classification or calculation rules change.

## Data Confidence

- An **exact event** is a valid timestamped Legend I battle observation from the official Clash of Clans API.
- An **incomplete collection attempt** has valid evidence from one requested endpoint but is still missing its paired evidence.
- An **inferred shielded day** is a derived ranked-day state supported by complete observations; it is not an official shield confirmation or an exact battle event.
- A **reconciled ranked day** satisfies `final trophies = start trophies + attack gain - defense loss`, including any automatic defense adjustment.
- A **complete ranked day** is reconciled and has enough evidence to show that no relevant event or settlement adjustment is missing.
- Timestamps allow exact event attribution, but timestamps alone do not prove that a ranked day is complete.
- If Clash Lens cannot prove completeness, mark the ranked day as partial or uncertain and preserve the reason.
- Player-profile trophies and battle-log events may become visible at different times. Preserve both observations and require eventual reconciliation rather than assuming paired responses are atomic.
- Every applicable surface must use the same confidence meanings.

## Other Ranked Tournaments

- Legend II is a weekly tournament with a fixed participant set, not one step in a continuous rank ladder beneath Legend I.
- Legend II and every ranked tournament other than Legend I are outside Phase 1.
- The rules of other ranked tournaments remain unspecified.
- Tournament, promotion, demotion, tracking, and completeness rules must be specified before Clash Lens extends the Legend I model to another tournament.
