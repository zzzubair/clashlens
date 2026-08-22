# Domain contract

## Read this file before changing domain behavior

This file owns the durable meanings, invariants, evidence states, and
versioned calculations for Legend I. It does not define current product scope
or implementation status. Report a conflict with code, tests, migrations, or a
live GitHub issue; do not choose silently.

Use this order when you implement or review a domain change:

1. Preserve the official source observations and their evidence boundaries.
2. Normalize player identities and decide collection eligibility from the accepted profile contract.
3. Attribute battle rows to a ranked day and link repeated or two-sided observations to one canonical battle.
4. Establish ranked-day evidence coverage from reset baselines and the battle-log chain.
5. Reconcile trophy movement and boundary adjustments. Keep exact events, inferred states, and reconciliation claims separate.
6. Publish frozen snapshots and versioned analytics with their provenance, freshness, coverage, and confidence.

A domain change is complete only when every affected source observation, derived record, version, confidence state, and public-facing aggregate follows the rules below, and when incomplete or conflicting evidence remains visible.

## 1. Time and season contract

- A **Legend I ranked day** starts at **05:00 UTC** and ends at **05:00 UTC** the next day.
- A Legend I season lasts exactly **28 ranked days**.
- A Legend I season starts and ends on a Monday at **05:00 UTC**.
- Use a confirmed official season anchor. Use half-open intervals: a season
  includes its start boundary and excludes its end boundary.
- Derive other season boundaries in exact 28-day steps from a confirmed anchor. Store the official season ID and the season-anchor rule version with each derived ranked day and season.
- A newer valid Legend I profile may advance the confirmed anchor when its
  current and previous season boundaries are Mondays at 05:00 UTC and exactly
  28 days apart. If accepted profiles disagree, retain the last confirmed
  anchor and mark the new source contract as conflicting.
- A player can have up to 8 attacks and 8 defenses in one ranked day.
- Store and calculate time in Coordinated Universal Time (UTC).
- The ranked-day boundary remains 05:00 UTC even when reset processing and snapshot publication finish later.

## 2. Player identity and tracking

### Identity and eligibility

- A **known player** is a valid, normalized player tag retained by Clash Lens after submission or discovery through an official Clash of Clans API source.
- An **actively tracked player** is a known player currently confirmed to be participating in Legend I and receiving regular Legend I collection.
- Confirm Legend I membership from the current official player-profile
  `leagueTier` field. Do not use the older `league` field; it can report
  `Unranked` for a current Legend I player.
- Publish eligibility from the newest recognized `leagueTier` evidence independently of season-anchor acceptance. A season-anchor conflict stays visible and cannot replace current profile, trophy, or anchor state, but it does not discard an otherwise recognized Legend I or non-Legend-I tier classification.
- Deactivate an actively tracked player only when a newer valid profile contains
  a tier that the accepted source contract explicitly recognizes as
  non-Legend-I. An unknown tier, an unexpected name, or a missing or malformed
  tier is a source-contract conflict or uncertain evidence. Keep the last
  confirmed eligibility state, mark it stale or conflicting, and do not
  activate or deactivate from that evidence.
- An **inactive known player** is retained with all existing history but does not receive regular Legend I battle collection.
- Accept valid player tags submitted by users.
- Discover additional tags only through official Clash of Clans API sources, including official leaderboards, clan data, and opponents present in official battle logs.
- Normalize and deduplicate a tag before adding it to the known-player registry.
- Check every newly discovered or submitted tag against the current official
  profile and add it to active tracking immediately after confirming that the
  player is in Legend I.
- When newer valid eligibility evidence shows that an actively tracked player left Legend I, retain the tag and history but remove the player from active Legend I tracking.
- Re-evaluate inactive known players during the Monday promotion and demotion transition and whenever a tag is rediscovered or submitted.
- Retaining inactive tags must allow later ranked-tournament support without re-creating player identity or losing history.

### Live Leaderboard ordering

- The **Live Leaderboard** orders actively tracked players by the newest valid trophy observation that Clash Lens has accepted for each player. Its **Rank** means position among players tracked by Clash Lens, not a claim of complete global coverage or one simultaneous official observation.
- Keep a player in this ordering when a later request is missing, delayed, malformed, or unsuccessful. Change the player's trophy value only when Clash Lens accepts a newer valid observation. Remove the player from active ordering only when newer valid evidence shows that the player is no longer eligible for active Legend I tracking.
- Record the trophy observation time, age, and freshness state with each leaderboard entry. Show the observation time as Last updated on the public Live Leaderboard and player page without adding technical freshness panels. Old data remains ranked and remains eligible for that snapshot's cumulative Top-N cohorts and rank bands, but the snapshot and its analytics must record how much membership uses old data.
- Order all Live Leaderboard entries by newest accepted trophies descending and resolve equal-trophy ties with a versioned deterministic hash of the normalized player tag. An official rank is separate provenance, is not a public leaderboard column, and does not change a Live Leaderboard Rank.
- Never use fresh randomness for a snapshot tie-break. The same tag, trophies, and ordering-rule version must reproduce the same position.
- Every snapshot must identify the ordering-rule version it used.

### Official rank is provenance only

- Retain the rank that Supercell supplies with each valid official Top-200 observation, including its ordering of equal-trophy players, as source provenance.
- Official rank does not affect Live Leaderboard Rank and does not create a separate public leaderboard, column, or source badge.

## 3. Official API observations

### Official global Top 200

- The only authoritative source for the current official global player rank is `GET /v1/locations/global/rankings/players?limit=200` from the official Clash of Clans API.
- A complete official Top-200 observation contains exactly 200 entries, exactly 200 unique valid normalized player tags, and the official `rank` values 1 through 200 once each. The same normalized tag at more than one rank is invalid. Use the returned rank. Do not calculate official rank from trophies or response position.
- The verified response does not supply a season identifier. Store official rank as a current observation with its source and observation time. Any Legend I season association is Clash Lens derived context, not an official season field.
- Preserve the untouched response as raw evidence. Add its valid player tags to the known-player registry and request normal profile collection for newly discovered tags.
- Maintain one most recent complete official Top-200 observation. Atomically replace it only after a newer observation passes all completeness checks. Keep a failed, short, malformed, duplicate-tagged, duplicate-ranked, or rank-gapped response inspectable as a failed or partial collection attempt, but do not let it replace the most recent complete observation.
- Record when the current official Top 200 was observed and whether a newer refresh attempt failed. Do not imply that one API response and the latest per-player profile observations were captured at the same instant.

### Player profiles and battle logs

- The official battle-log API returns up to the latest 50 battles.
- One response can mix `legend`, `ranked`, and `homeVillage` battles.
- Legend I rows use `battleType: "legend"`.
- Legend I battle rows use `attack: true` when the reporting player attacked
  and `attack: false` when the reporting player defended, plus a
  `battleTimestamp`.
- Legend I battle rows include stars, destruction, flat
  `opponentPlayerTag`, `opponentName`, and `opponentTownHallLevel` fields, and
  `armyShareCode`.
- Battle rows do not include the trophy change directly. Clash Lens derives it from stars and destruction using the versioned table in `docs/data/legend-trophy-allocation-v1.csv`.
- For each star count, use the last trophy value whose minimum destruction percentage is not greater than the battle's destruction percentage. Reject impossible or out-of-range star and destruction combinations instead of guessing.
- A 0-star attack at 0 through 9 percent destruction gives the attacker 0 trophies. Other 0-star attacks give the attacker the amount in the table, but the defender loses 0 trophies.
- For 1-star, 2-star, and 3-star attacks, the attacker gains the table amount and the defender loses the same amount.
- Store the trophy-allocation rule version with each calculated battle result so later rule changes can replay saved evidence.
- A battle that produces zero trophies is still an exact event and counts toward the player's attack or defense count.
- Repeated polls overlap. Clash Lens must not create duplicate battle events from repeated observations.
- Preserve every raw source observation, including its fetch time and untouched response body.
- Preserve successful observations when a paired endpoint request fails. Mark the collection attempt incomplete until the missing evidence is collected.
- Start tracking a valid tag when Clash Lens first confirms it for active tracking.
- Reconstruct all retained timestamped Legend I events available at first observation.
- Mark history before the first reliable observation as partial or unavailable. Do not invent missing history.

### Battle identity and perspectives

- Identify one Legend I battle by its ranked day, normalized attacker tag, and normalized defender tag. The same attacker cannot attack the same defender more than once in one Legend I ranked day; the same pairing on a later ranked day is a different battle.
- Treat repeated polls and matching attacker-side and defender-side rows as evidence for the same battle. One valid row is enough to store the battle. Track whether the attacker's log and defender's log have each reported it. Show the battle on each player's daily log after that player's own battle-log observation reports it; the other side may appear later. When both sides arrive, attach them to the same saved battle.
- Trust each player's own newest valid battle-log report for that player's daily log and trophy reconciliation. Use the attacker's own report for offense analytics and the defender's own report for defense analytics. A repeated poll of the same side updates that side only when it is newer valid evidence.
- Keep timestamp, army share code, stars, and destruction as battle details and consistency checks, not identity fields. A missing or corrected detail must not create a second battle.
- The source contract expects the two sides to agree. If they do not, preserve both reports and mark a perspective disagreement. Do not let one side overwrite the other. Keep each side in its own player view and analytics lens, and include the disagreement in data-quality counts.
- Make battle ingestion idempotent so one battle contributes only once to analytics.

## 4. Ranked-day evidence coverage

- A **reset-baseline sweep** for one player is one durable boundary attempt that requests both profile and battle-log endpoints after the 05:00 UTC boundary. Its endpoint results retain the same sweep ID and boundary time even when one endpoint succeeds before a retry completes the other.
- A valid **reset baseline** requires a valid profile from that sweep collected before the player's first retained Legend I event in the new ranked day and a valid associated battle-log response. The sweep should finish before the snapshot target, but publication delay alone does not invalidate otherwise ordered evidence. An older profile can keep the player in a leaderboard, but it cannot prove a ranked-day boundary.
- A ranked day has **continuous battle-log coverage** when valid battle-log observations form a chain from the battle-log response in the start reset-baseline sweep through the battle-log response in the next boundary's end sweep. Each consecutive response must either contain fewer than the official 50-row maximum or share at least one stable battle identity with the preceding response. A full 50-row response with no overlap creates a coverage gap.
- Every new row exposed by the chain must be retained and processed. A malformed, unsupported, or identity-conflicting Legend I row creates a visible coverage gap until later valid evidence resolves it.
- Poll success percentage and elapsed time alone do not prove coverage. Use source-row continuity and boundary evidence.
- A ranked day has **complete evidence coverage** only when its start and end sweep IDs each link a valid profile and battle-log response, it has continuous battle-log coverage between those responses, and it has valid applicable Legend I rows, established attack and defense counts, and known boundary adjustments. A legacy profile-only reset attempt cannot prove Complete.
- Late evidence may close a coverage gap and create a corrected ranked-day version. Do not rewrite the previous version in place.

## 5. Derived ranked-day states and adjustments

### Inferred shielded days

- A **shield** prevents a Legend I player from attacking and prevents other players from attacking them for 1 or 2 ranked days.
- The official API does not directly confirm shield state. Clash Lens may classify a day only as **inferred shielded**.
- Infer a shielded day only when all of the following are true:
  - The player remained eligible for active Legend I tracking.
  - The ranked day has complete evidence coverage.
  - The player's trophies did not change across the ranked day.
  - The battle log contains no Legend I attack or defense event timestamped within the ranked day.
  - No automatic defense adjustment applies.
- A zero-trophy battle is still a Legend I event and prevents the day from being classified as shielded.
- Preserve an inferred shielded day as an explicit ranked-day row with zero attacks, zero defenses, and zero trophy change. Do not omit it or classify it as missing.
- One or two consecutive qualifying days may be labeled as an inferred 1-day or 2-day shield. A longer zero-event sequence is not a valid shield duration under the current rule and must be marked uncertain.
- Shielded days contribute no battle events to offense or defense analytics.

### Automatic defense adjustment

- An **automatic defense adjustment** is a reset-time trophy loss applied when a player has fewer than 8 observed defenses in a Legend I ranked day.
- It is a settlement adjustment, not a battle event. It has no opponent, army, destruction result, or battle timestamp.
- There is no automatic offense adjustment.
- Zero-trophy defense events count as observed defenses when determining how many defenses are missing.
- Calculate an automatic defense adjustment only when the previous and current days have continuous battle-log coverage and retained evidence establishes their defense-event counts and observed event losses. If any formula input may be incomplete because collection evidence is missing, do not replace the unknown evidence with an adjustment.
- For a current day with 1 through 7 established defense events, calculate the positive loss magnitude per missing defense as:

  `floor((previous day observed event loss + current day observed event loss) / (previous day defense-event count + current day defense-event count))`

- **Observed event loss** in this formula excludes every automatic defense adjustment.
- Multiply the floored loss by `8 - current day defense-event count` to calculate the total automatic defense adjustment.
- Do not apply this inference rule to a day with zero established defense events. Preserve the evidence and mark the day uncertain if that exceptional case occurs.
- A **calculated automatic defense adjustment** uses the versioned averaging rule when the reset outcome cannot yet isolate the exact adjustment.
- A **confirmed automatic defense adjustment** requires continuous battle-log coverage for the previous and current days plus valid start and end reset baselines whose trophy values jointly isolate the adjustment. Trophy reconciliation alone does not prove its cause.
- Show the adjustment separately from battle events and identify whether it is calculated or confirmed.
- Automatic defense adjustments affect ranked-day trophy reconciliation but never contribute to army usage, three-star rate, or other battle-event analytics.

### Weekly and season trophy resets

- At each Monday 05:00 UTC boundary, a player who remains in Legend I with fewer than 5,000 trophies resets to 5,000 trophies.
- At the start of each 28-day Legend I season, every Legend I player resets to 5,000 trophies. Excess trophies above 5,000 become Legend trophies under the official game rule.
- Store a weekly or season reset as an explicit boundary adjustment. It is not a battle, attack gain, defense loss, or automatic defense adjustment.
- Reconcile the ended ranked day before the weekly or season reset. Apply the boundary adjustment after that reconciliation, and use the adjusted value as the next ranked day's starting trophies.
- Show each boundary adjustment and its official rule version separately. Do not include it in offense, defense, army, or battle-outcome analytics.

## 6. Ranked-day and leaderboard snapshots

- At 05:00 UTC, event ownership moves to the new ranked day. A battle first observed later is still added to the ended day when its timestamp falls before the boundary.
- A **frozen leaderboard snapshot** is the accepted, versioned ordering of actively tracked players at a reset baseline.
- Continue serving the previously frozen snapshot while the next snapshot is assembled. Publish the replacement atomically so users never receive a mixture of snapshot versions.
- Target snapshot publication at approximately 05:05 UTC on normal days, after the daily no-attack matchmaking window.
- Target snapshot publication at approximately 05:10 UTC on Mondays, after the longer promotion and demotion transition.
- The one public **Live Leaderboard** uses the latest available observation for every actively tracked Legend I player. Frozen snapshots support reproducible history and analytics; they do not create a separate official-versus-tracked leaderboard.
- Retain each entry's observation time, measured coverage, freshness, confidence, and applicable official rank provenance.
- Publishing at the target time means accepting the best official observations available under those rules; it does not claim that every API response was generated simultaneously or that every entry has equal freshness.
- If later evidence proves a frozen snapshot inconsistent, retain the prior version and publish a corrected version rather than silently rewriting it.

## 7. Legend I meta analytics

### Composition and rates

- Preserve the raw `armyShareCode` from each battle observation.
- An **army composition** is the exact decoded set and quantity of troops, spells, siege machines, and other units represented by an `armyShareCode`.
- Record each decoded component's numeric identifier, quantity, encoded section, and origin.
- Preserve an unknown numeric identifier, quantity, encoded section, and origin while retaining the known components from the same army. Keep its semantic category unresolved when the encoded section and current catalog cannot distinguish it. Unknown IDs indicate catalog work still to do and must not be discarded, guessed, or silently grouped.
- **Unit usage rate** is the share of unique Legend I attacks in a stated cohort and time period that contain the stated unit.
- **Three-star rate** is the share of attacks in a stated population, time period, and filter that achieved three stars. It is exposed canonically as `three_star_rate`; there is no duplicate hit-rate field.
- Confirmed individual components from a partial decode contribute to individual usage and outcomes. Their usage denominator is every fully or partially decoded eligible attack.
- A relationship or complete composition uses fully decoded attacks plus only partial attacks where unresolved evidence cannot change whether that relationship is present. Publish the row's exact denominator and unknown exclusion count.
- Equipment conditional on its owning hero uses attacks where that hero is confirmed and the equipment assignment can be proved present or absent.
- One battle contributes at most one usage regardless of component quantity. Every aggregate keeps small samples visible.

### Population filters and lenses

- For a trophy-range filter, the **defense lens** groups attacks by the defender's trophies at battle time.
- For a trophy-range filter, the **offense lens** groups attacks by the attacker's trophies at battle time.
- For a frozen leaderboard-cohort or rank-band filter, the defense lens includes attacks whose defenders belong to the selected snapshot population.
- For a frozen leaderboard-cohort or rank-band filter, the offense lens includes attacks whose attackers belong to the selected snapshot population.
- For a rank-streak filter, the defense lens includes attacks against players in the resulting streak set during the selected consecutive period.
- For a rank-streak filter, the offense lens includes attacks made by players in the resulting streak set during the selected consecutive period.
- Do not substitute current, snapshot, or season-end trophies for battle-time trophies without labeling the value as an estimate.
- Do not count the same battle twice when it appears in both the attacker's and defender's battle logs.
- Frozen Top-N cohorts are cumulative and use Top 5, 10, 20, 50, 100, 200, 500, and 1,000. Membership is frozen at the end of the final selected Legend day; Top 100 is the default.
- Frozen rank bands are ranks 1–5, 6–10, 11–20, 21–50, 51–100, 101–200, and each 100-rank band from 201–300 through 901–1,000. Membership is frozen at the end of the final selected Legend day.
- A **rank streak** is available only for a Top-N preset and contains players in that Top-N cohort in every frozen daily snapshot of the selected inclusive range. A stale, missing, or uncertain membership cannot establish a confirmed streak; report excluded membership and shielded-day evidence.
- A trophy range is an arbitrary inclusive minimum and maximum, with minimum at least 5,000 and maximum not lower than minimum. Use the lens-specific battle-time trophy value; missing battle-time evidence is an exclusion, never a substituted observation.
- Exactly one trophy range, frozen Top-N cohort, frozen rank band, or Top-N rank streak applies at a time.

### Aggregate evidence

- Every aggregate must state its population filter, time period, observed sample size, measured coverage, freshness, decoder and catalog version, unknown-ID count, malformed or partial count, and analytics-rule version.
- When a selected one-snapshot cohort includes entries that use old trophy observations, return the available analytics and state the old-entry count and age. Do not silently replace the requested population or present it as fully fresh.
- Do not silently exclude malformed, partial, zero-trophy, or unknown-ID observations. Show their effect on coverage and confidence.
- Preserve previously published analytics with their original rule labels when decoding, catalog, or calculation rules change.

## 8. Evidence and confidence states

Use the same confidence meanings on every applicable surface.

- An **exact event** is a valid timestamped Legend I battle observation from the official Clash of Clans API.
- An **incomplete collection attempt** has valid evidence from one requested endpoint but is still missing its paired evidence.
- An **inferred shielded day** is a derived ranked-day state supported by complete observations; it is not an official shield confirmation or an exact battle event.
- A **reconciled ranked day** satisfies `final trophies before a weekly or season boundary reset = start trophies + attack gain - defense loss`, including any automatic defense adjustment. A separate boundary adjustment explains any change to the next ranked day's starting trophies.
- A **complete ranked day** is reconciled, has complete evidence coverage, and has no unresolved event, eligibility, or settlement-adjustment input. Otherwise it is partial with a machine-readable reason.
- Timestamps allow exact event attribution, but timestamps alone do not prove that a ranked day is complete.
- If Clash Lens cannot prove completeness, mark the ranked day as partial or uncertain and preserve the reason.
- Player-profile trophies and battle-log events may become visible at different times. Preserve both observations and require eventual reconciliation rather than assuming paired responses are atomic.
