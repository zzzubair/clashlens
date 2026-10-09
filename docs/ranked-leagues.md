# Ranked leagues and Legend League

Checked **2026-10-04**. This is the game-rules reference for Clash Lens. Dates below distinguish announcements from released changes. Supercell's current support pages and dated release notes are primary evidence; the Clash of Clans Wiki is secondary evidence. Where they disagree, the disagreement stays visible.

All times use Coordinated Universal Time, **UTC**. For how Clash Lens assigns battles to Legend days, models Seasons and establishes reliable trophy counts, see the [domain contract](domain.md#1-time-and-season-contract). This document does not change those implementation rules.

## Current ladder

Ranked unlocks at Town Hall 7. Ordinary Battles allow unlimited attacks without changing Ranked standing. Ranked uses limited attacks and Tournament results to determine movement through the ladder. Below Legend I, matchmaking considers Town Hall level and league placement; in Legend I, Supercell identifies trophy count as the most important factor. [Launch, 2025-10-06][launch]; [mode comparison][modes]; [Ranked scoring][ranked-scoring]; [Legend matchmaking][matchmaking].

The table covers every tier. Promotion and demotion figures below Legend I are bracket places per full 100-player group, not guaranteed counts of players who move or worldwide totals. Supercell groups lower tiers into percentage ranges; the Wiki supplies the individual percentages shown below. Those exact lower-tier splits are therefore **Wiki-reported**, consistent with the current support ranges but not independently verified in-game. Legend III/II's promotion counts are explicit in the April release. [Ranked support][ranked]; [Wiki Ranked Battles][wiki-ranked]; [2026-04-27 release][april].

| Tier | Attacks/week | Promotion bracket/group | Demotion bracket/group |
| --- | ---: | ---: | ---: |
| Skeleton 1 | 6 | 50 | 0 |
| Skeleton 2 | 6 | 50 | 5 |
| Skeleton 3 | 6 | 50 | 5 |
| Barbarian 4 | 6 | 50 | 5 |
| Barbarian 5 | 6 | 50 | 5 |
| Barbarian 6 | 6 | 50 | 5 |
| Archer 7 | 8 | 50 | 5 |
| Archer 8 | 8 | 50 | 5 |
| Archer 9 | 8 | 40 | 5 |
| Wizard 10 | 8 | 35 | 10 |
| Wizard 11 | 8 | 50 | 10 |
| Wizard 12 | 8 | 40 | 10 |
| Valkyrie 13 | 10 | 35 | 10 |
| Valkyrie 14 | 10 | 50 | 10 |
| Valkyrie 15 | 10 | 40 | 10 |
| Witch 16 | 10 | 35 | 10 |
| Witch 17 | 10 | 50 | 10 |
| Witch 18 | 10 | 40 | 10 |
| Golem 19 | 12 | 35 | 10 |
| Golem 20 | 12 | 50 | 10 |
| Golem 21 | 12 | 40 | 10 |
| P.E.K.K.A 22 | 12 | 30 | 15 |
| P.E.K.K.A 23 | 12 | 50 | 15 |
| P.E.K.K.A 24 | 12 | 40 | 15 |
| Titan 25 | 12 | 30 | 15 |
| Titan 26 | 12 | 50 | 15 |
| Titan 27 | 12 | 40 | 15 |
| Dragon 28 | 14 | 30 | 15 |
| Dragon 29 | 14 | 25 | 15 |
| Dragon 30 | 14 | 25 | 15 |
| Electro 31 | 18 | 20 | 15 |
| Electro 32 | 18 | 20 | 15 |
| Electro 33 | 18 | 15 to Legend III | 15 |
| Legend III | 24 | 5 to Legend II | 15 to Electro 33 |
| Legend II | 30 | 3 to Legend I | 15 to Legend III |
| Legend I | 56, allocated as 8/day | None | Weekly global places 10,001 onward to Legend II |

Below Legend III, groups can be smaller than 100. Supercell's minimum group sizes are 15 for Skeleton/Barbarian, 20 for Archer/Wizard, 25 for Valkyrie/Witch, 30 for Golem/P.E.K.K.A/Titan, 40 for Dragon and 50 for Electro. Legend III and II list both minimum and normal size as 100. Percentages cannot be converted into exact counts for smaller groups without a rounding rule. [Ranked support][ranked].

The Wiki describes one-tier promotion/demotion, with other players staying put. A player in a demotion place at their Town Hall floor stays in that league. Its tie order is highest average attack destruction, lowest average defense destruction, then shortest average attack time; a remaining tie stays tied. A Town Hall upgrade can raise a player multiple tiers to the new floor after the Tournament. These details are secondary evidence. [Wiki Ranked Battles][wiki-ranked].

**Bracket uncertainty:** February's release set Golem 19/20 demotion to 15% and Dragon 29/30 to 20%; current support instead gives 10% and 15% respectively. Other promotion percentages also exceed February's published values. No dated later post reviewed explains every difference. The table records current published support/Wiki values, not a claim that all transitions were verified. [2026-02-23 release][february]; [Ranked support][ranked].

## Weekly cycle, Seasons and entry

| Event | Current published timing or rule | Source |
| --- | --- | --- |
| Weekly movement/inactivity boundary | Monday 05:00 UTC; Legend I's demotion check follows the ending Sunday | [Ranked support][ranked], [April release][april] |
| Weekly battle window opens | Monday 17:00 UTC, replacing Tuesday 05:00 UTC | [April release][april] |
| Legend III and II | Weekly Tournaments | [April release][april], [Legend introduction][legend] |
| Legend I daily Reset | 05:00 UTC; each Legend day lasts 24 hours | [Legend introduction][legend] |
| Legend I Season | Four weeks, exactly 28 Legend days; starts/ends Monday 05:00 UTC | [Legend introduction][legend] |
| Full participating Legend day | Eight assigned attacks and eight possible defenses; unused attacks do not carry over | [Legend introduction][legend], [Wiki Legend][wiki-legend] |
| New Legend I entrant | Starts at 5,000 trophies | [April release][april] |
| Weekly Legend I adjustment | Retained players below 5,000 rise to 5,000; players already above it retain their trophies | [Legend introduction][legend] |
| New Legend I Season | Everyone starts at 5,000; the previous Season's excess above 5,000 becomes permanent Legend Trophies on the profile | [Legend introduction][legend], [Legend scoring][legend-scoring] |
| Weekly Legend I demotion | Finishing worse than global rank 10,000 causes demotion to Legend II on Monday; crossing that rank during the week is not immediate demotion | [April release][april], [Legend matchmaking][matchmaking] |

Below Legend I, weekly trophy totals reset for the next Tournament. Legend I's four-week total survives its intermediate weekly checks. The Monday 17:00 weekly start does not replace Legend I's daily 05:00 Reset. [Wiki Trophies][wiki-trophies]; [April release][april]; [Legend introduction][legend].

Promotion gives eligibility to join Legend I; entry is optional. Manual entry requires confirming **Join Tournament**. With auto sign-up enabled, enrollment is automatic; enrolled players continue into subsequent Seasons while eligible. Late entry can reduce both that day's attacks and defenses. Auto sign-up is disabled on inactivity demotion after four weeks without enrollment. [Legend introduction][legend].

The current inactivity rule concerns **not signing up**. Four Tournament Weeks are allowed before demotion at the next Monday 05:00 boundary. Further inactivity costs one tier per additional four weeks until the Town Hall floor, the lowest league allowed for that Town Hall. Another four weeks at the floor makes the player unranked. This does not exempt enrolled Legend I players from the weekly rank cutoff. [Ranked support][ranked]; [April release][april].

### Town Hall floors

The full mapping is Wiki-reported. Supercell directly confirms April's changes for Town Halls 16–18 and defines the floor/inactivity behavior. [Wiki Ranked Battles][wiki-ranked]; [April release][april]; [Ranked support][ranked].

| Town Hall | Floor | Town Hall | Floor |
| ---: | --- | ---: | --- |
| 7 | Skeleton 1 | 13 | Wizard 11 |
| 8 | Skeleton 2 | 14 | Valkyrie 14 |
| 9 | Skeleton 3 | 15 | Witch 17 |
| 10 | Barbarian 4 | 16 | Golem 20 |
| 11 | Barbarian 6 | 17 | P.E.K.K.A 23 |
| 12 | Archer 8 | 18 | Titan 26 |

## Battle scoring and matchmaking

| Attack result | Attacker's trophy gain | Detailed calculation reported by the Wiki |
| --- | ---: | --- |
| 0 stars | 0–4 | One per complete 10% destruction; 0–9% earns zero |
| 1 star | 5–15 | 5 at 1%, then one per further 9 percentage points |
| 2 stars | 16–32 | 16 at 50%, then one per further 3 percentage points |
| 3 stars | 40 | Full destruction |

Supercell publishes the ranges and says Legend I attacks depend on stars/destruction, not the opponent's trophy total. An unsuccessful attack does not directly subtract the attacker's trophies. The Wiki gives the detailed steps above and says the attack calculation is shared by Ranked and Legend I. [Ranked scoring][ranked-scoring]; [Legend scoring][legend-scoring]; [Wiki Ranked Battles][wiki-ranked]; [Wiki Legend][wiki-legend].

Below Legend I, Supercell says the defender gains the unused portion of 40 trophies and never loses trophies. In Legend I, it says the defender loses the attacker's gain. **Zero-star exception:** the Wiki instead explicitly gives lower-league defenders all 40 on a zero-star defense and Legend I defenders zero loss, even when the attacker gains 1–4. Clash Lens models the Legend I exception in the [domain trophy rules](domain.md#player-profiles-and-battle-logs). The official summaries omit this exception; it is not resolved by treating either page as a full formula. [Ranked scoring][ranked-scoring]; [Legend scoring][legend-scoring]; [Wiki Ranked Battles][wiki-ranked]; [Wiki Legend][wiki-legend].

Legend I matchmaking happens for each Legend day, using a saved defensive layout. Incoming and outgoing opponents need not be the same. There is no Revenge in Ranked. The current Ranked Wiki says targets cannot be skipped. Supercell's 2019 FAQ also says Legend targets cannot be scouted before attacking; no current published page reviewed independently confirms that restriction. [Legend matchmaking][matchmaking]; [Wiki Legend][wiki-legend]; [Wiki Ranked Battles][wiki-ranked]; [2019 FAQ][old-faq].

Missing **allocated** Legend I defenses are filled using the average of the current and previous day's defensive results. Supercell does not publish the precise averaging, rounding or fallback algorithm. The Wiki also reports compensation for missing weekly defenses below Legend I, based on that week's received defenses, with no compensation if none were received. That lower-league detail is not confirmed by the official pages reviewed. [April release][april]; [Wiki Ranked Battles][wiki-ranked].

### Difficulty and defensive layouts

Battle modifiers change unit strength. Only Legend III/II/I currently use Ranked modifiers; April removed them from Electro 32/33. Percentages below are from the April release, corroborated by the Wiki. Equipment has no level reduction in these three tiers. [April release][april]; [Wiki Ranked Battles][wiki-ranked].

| Tier | Defense damage | Defending Hero health/damage | Guardian health/damage | Attacking Hero health/damage |
| --- | ---: | ---: | ---: | ---: |
| Legend III | +10% | +10% | +5% | -5% |
| Legend II | +15% | +15% | +10% | -10% |
| Legend I | +20% | +20% | +20% | -20% |

Legend I layout and defending Clan Castle changes take effect on the next Legend day. April's release says weekly layouts can change until battles start; the current generic support article instead says they lock when assigned to a Monday group. That weekly cutoff is a source conflict. Temporary troops/spells are excluded from Ranked, and a battle must finish before another begins. [Layout support][layouts]; [April release][april]; [Ranked support][ranked]; [2025-11-17 release][th18]; [2026-02-23 release][february].

## Shields

### What they protect

**Magic Shields** protect resources in ordinary Battles. An ordinary defense losing at least 30% of available loot grants an eight-hour Magic Shield. They can also be bought with Gems in the Shop; attacking does not shorten them. They do not cancel Ranked defenses, including Legend II/III or Legend I. [General shield support][shields]; [2025 launch][launch]; [Legend introduction][legend].

**Legend Shields** are bought in the Shop with Gems and work only in Legend I. Taking a Legend I defense never grants one automatically. When active at Reset, a Legend Shield skips the following whole Legend day: no Legend I attacks and no assigned Legend I defenses. Buying or removing one takes effect the next Legend day, so it cannot cancel today's already allocated defenses. Ordinary Battles remain available. [Legend shield support][legend-shields]; [Legend introduction][legend]; [Wiki Shield][wiki-shield].

A shielded day is not eight completed defenses, eight defensive wins, or eight missed defenses requiring automatic losses. This follows from Supercell's promise that both sides of the day's allocation are skipped, and the Wiki explicitly describes protection from Legend trophy loss. The filled-defense rule concerns missing allocated defenses. Supercell does not separately publish every shield/automatic-loss boundary case or processing order. [Legend shield support][legend-shields]; [April release][april]; [Wiki Legend][wiki-legend].

Signing up and buying a shield are separate actions. A Magic Shield cannot protect a new Legend I entrant. Support says an active shield does not carry into Legend on sign-up; it also contains older wording about shields surviving departure into ordinary multiplayer, while the Wiki says an unused Legend Shield has no effect after demotion. Do not infer a current conversion/refund rule from these conflicting descriptions. No reviewed source promises that a shield pauses Season time or protects a player from the weekly rank cutoff. [Legend shield support][legend-shields]; [Legend introduction][legend]; [Wiki Legend][wiki-legend].

### Durations, Gem prices and purchase waits

Buying shields with Gems and waiting before buying the **same duration** again are officially confirmed. Supercell's general support article illustrates these shop offers; its embedded image uses the older shield artwork and does not identify separate current Legend prices. The Wiki lists all three durations for both Magic and Legend Shields and the same purchase waits. [General shield support and shop image][shields]; [Wiki Shield][wiki-shield].

| Duration | Gems shown in official general shop image | Wait before repurchasing same duration | Strength of Legend-specific evidence |
| --- | ---: | --- | --- |
| 1 day | 100 | 4 days | Explicit in Supercell's 2019 Legend FAQ; still listed by Wiki |
| 2 days | 150 | 7 days | Explicit in Supercell's 2019 Legend FAQ; still listed by Wiki |
| 7 days | 250 | 35 days | Current Wiki lists it; 2019 FAQ allowed it only up to 5,200 trophies |

The costs and waits above are visible in the [official shop image][shield-image]. The one-/two-day Legend waits and historical 5,200 restriction are in the [2019 FAQ][old-faq]. **Current Legend I prices, seven-day eligibility and whether the old trophy restriction survives the 2025/2026 changes are not independently confirmed.** Do not turn the historical restriction into a current rule. The published evidence supports a longer shield option, with that qualification.

General support explicitly permits stacking a one-day and two-day shield into three days. That establishes that two days is not a universal shield-duration ceiling, so Clash Lens infers a shielded run of any length. [General shield support][shields]; [domain shield model](domain.md#inferred-shielded-days); [implementation](../python/src/clashlens/reconciliation.py).

### What Clash Lens can see

The official Clash of Clans application programming interface, **API**, is the service that supplies player profiles and battle logs. The reviewed Clash Lens source contract has no direct shield confirmation field. Its saved `shield_state` is calculated by Clash Lens, not copied from a player-profile field. The exported profile tables contain trophies, league tier and Season identifiers; completed battle records contain results, not a shield purchase, expiry timer or future defense allocation. These are findings about the retained data and parser, not proof that every possible API field has been inspected. [Profile parser](../python/src/clashlens/profile.py); [domain shield model](domain.md#inferred-shielded-days).

In the **2026-10-04 read-only data export**, player `#LY2QQ9L9Q`, fxDefuser, had these results. A date names the Legend day starting at 05:00 UTC. The export was taken approximately 14:59–15:12 UTC, so October 4 was unfinished.

| Legend day | Recorded attacks / defenses | Start → end trophies | Saved shield state | Evidence |
| --- | --- | --- | --- | --- |
| 2026-10-01 | 8 / 8 | 5,401 → 5,424 | `not_inferred` | Complete, exact |
| 2026-10-02 | 8 / 8 | 5,424 → 5,411 | `not_inferred` | Complete, exact |
| 2026-10-03 | 0 / 0 | 5,411 → 5,411 | `inferred_shielded` | Complete, inferred; next Reset also 5,411 |
| 2026-10-04 | 0 / 0 | 5,411 → unknown | `not_inferred` | Live, partial; ending observations absent |

October 3 has complete saved coverage and no automatic defense adjustment: `automatic_defense_evidence_state = not_applicable`, while `automatic_defense_loss` is unset, not an API-reported zero. October 4 has the same automatic-adjustment fields but cannot establish a full-day result yet. The latest retained detailed profile, at 06:23:17 UTC on October 4, has `trophies = 5411`, `leagueTier.name = "Legend I"`, `currentLeagueSeasonId = 1788757200`, and `attackWins = defenseWins = 0`. Those win counters do not establish shield state; earlier days contain real battles despite those latest zero counters.

The latest recorded defense was October 3 at 04:26:52 UTC, belonging to the October 2 Legend day. No later attack/defense for this player was present in the export. Thus the October 3 pattern supports a shield inference. October 4 cannot be confirmed from this partial export. An inactive, unsigned or incompletely observed player can also have no new battles. This example uses retained observations; raw profile/log field changes during a shield purchase or expiry were not observed.

Reproduction against the supplied snapshot, opened read-only; no production query or application change was made:

```sql
SELECT legend_day_start, attack_count, defense_count,
       day_start_trophies, eod_trophies, next_start_trophies,
       shield_state, automatic_defense_loss,
       automatic_defense_evidence_state, day_state,
       day_confidence, coverage_complete
FROM player_days
WHERE tag = '#LY2QQ9L9Q'
ORDER BY legend_day_start;
```

## Legend I capacity

**No fixed simultaneous Legend I cap is published in the reviewed sources.** The 10,000 figure is the weekly retention cutoff. New promotions depend on how many Legend II groups run: three promotions per group. Players promoted but not yet signed up can also hold Legend I. These facts explain membership above 10,000; they do not give an exact worldwide ceiling. With `G` groups, `3 × G` is the number of weekly promotion places, not a published fixed quota. [April announcement][announcement]; [April release][april]; [Legend introduction][legend].

The April 2026 placement was **one time**. Performance from April 20 until release selected the top 12,500 for Legend I, the next 50,000 for Legend II, the remaining eligible players for Legend III, and demoted players for Electro 33. These were initial allocations, not permanent capacities. [2026-04-16 announcement][announcement].

**Clash Lens measurement, 2026-10-04:** 13,263 known Legend I tier holders = **11,968 profiles with the current Season identifier + 1,295 profiles with identifier 0**. All 1,295 had identifier `0`, 5,000 trophies and no recorded battles; the other 11,968 had identifier `1788757200`. The counts were reproduced from each tracked player's latest saved profile version. We interpret the identifier-0 group as likely promoted players who have not enrolled, but the reviewed official sources do not define 0 as an enrollment flag; enrollment was not directly verified. This is a known-player sample using latest observations, not a simultaneous worldwide census or a capacity limit.

For reproduction, group the latest `profile_versions` row per `player_id` by `league_tier_name` and `current_league_season_id`, ordered by `observed_at` then `profile_version_id` descending, and join `players` where `tracked_active` is true. That produces the two counts above. Do not calculate weekly promotions as `11,968 - 10,000`: enrollment timing, discovery and older observations prevent that inference.

## Dated changes

Newest first. This covers Ranked/Legend structure, movement, entry, scoring-related mechanics and league-specific difficulty since the preview; unrelated troop balance, cosmetics and Clan War Leagues are excluded. Announcements are labelled. The Wiki's 2025/2026 version histories were cross-checked against the dated originals. [Wiki 2026 history][wiki-2026]; [Wiki 2025 history][wiki-2025].

| Date | Change or announcement | Source |
| --- | --- | --- |
| 2026-06-15 publication; Wiki dates update 2026-06-16 | Supercell's release notes report rebalanced Ranked achievement Gem rewards without exact new amounts. The precise rollout date was not independently established. No change to the Legend I movement/entry rules is stated. | [June update][june]; [Wiki 2026 history][wiki-2026] |
| 2026-04-27 | Released Legend III/II/I: 24/30 weekly attacks in III/II, top 5/top 3 promote, 8 daily attacks and four-week Seasons in I; weekly global rank-10,000 cutoff; new entrants and weekly sub-5,000 survivors raised to 5,000; all reset at the new Season; missing defenses filled from current/previous-day defensive results. | [Sound of Clash][april] |
| 2026-04-27 | Weekly battles moved from Tuesday 05:00 to Monday 17:00 UTC. Inactivity allowance and later tier decay changed from one to four weeks. Town Hall floors: 16 Golem 21→20; 17 Titan 25→P.E.K.K.A 23; 18 gets Titan 26. | [Sound of Clash][april] |
| 2026-04-27 | Weekly attack counts: Titan 25–27 14→12, Dragon 28–30 18→14, Electro 31–33 24→18. Removed Electro 32/33 modifiers; established the Legend modifier table above. | [Sound of Clash][april] |
| 2026-04-27 | Defensive-layout editing/automatic next-period updates revised; battle logs, promotion/demotion screens and leaderboards redesigned; Town Hall leaderboard added; Legend I displays Top 200 plus own rank; profile retains best rank, its trophies and Legend Trophies. All Legend tiers share bonuses; Star/League Bonus scaling increased and Ranked battle loot reduced; empty armies blocked. | [Sound of Clash][april] |
| 2026-04-20 to release | One-time placement window announced: top 12,500→I, next 50,000→II, remaining eligible players→III, demoted players→Electro 33. | [April 16 announcement][announcement] |
| 2026-04-16 | Announced three Legend tiers, their attack/promotion counts and I's weekly retention cutoff; lower high-league battle counts and four-week inactivity grace previewed. | [April announcement][announcement] |
| 2026-03-24 | Previewed multiple Legend layers culminating in top-10,000 competition and clearer Ranked results/layouts/movement; no released numerical movement change yet. | [State of Gameplay][march] |
| 2026-02-23 | Required finishing one Ranked battle before starting another; fixed false demotion warnings. Weekly attacks: P.E.K.K.A 24 14→12, Titan 26/27 18→14, Dragon 28–30 24→18, Electro 31–33 30→24. | [February update][february] |
| 2026-02-23 | Demotion percentages: Golem 19/20 20→15; Dragon 29/30 and Electro 31 15→20; Electro 32 10→15. | [February update][february] |
| 2026-02-23 | Promotion percentages: Archer 8/9, Wizard 10–12, Valkyrie 13–15, Witch 16/17 25→30; Witch 18/Golem 19 20→30; Golem 21/P.E.K.K.A 22/23 20→25; Dragon 29/30/Electro 31 15→20; Electro 32 10→15. | [February update][february] |
| 2026-01-28 | Legend defense damage/defending Hero bonus +20%→+15%; attacking Hero penalty -10%→-5%. Electro 33 equivalents +14%→+10% and -6%→0%; Electro 32 +7%→+5% and -3%→0%. Equipment unchanged. Later superseded by April's tier modifiers. | [January maintenance][january] |
| 2026-01-26 | Announced fewer high-league attacks, revised promotion/demotion proportions, corrected warnings and adjusted modifiers; exact battle/bracket changes arrived in February. | [Ranked changes preview][january-preview] |
| 2025-11-17 | Excluded temporary troops/spells; made weekly-group matchmaking less predictable. After the then-one-week inactivity grace, next-week enrollment deadline moved Monday 05:00→Tuesday 05:00 UTC, with warning from Friday 05:00. The historical definition also counted enrolling but doing no battles as inactive. | [Town Hall 18][th18] |
| 2025-11-17 | Promotion increased 5 percentage points in tiers 7, 16–27 and 29–31; tier 28 increased 10 points. Demotion increased 5 points in 28–31. Weekly attacks reduced by 2 in 22/23 and by 4 in 25. | [Town Hall 18][th18] |
| 2025-10-06 | Launched separate ordinary and Ranked Battles; Ranked available from Town Hall 7 with weekly progression into the then-single Legend League. Split Magic and Legend Shields; ordinary attacks no longer consume shield time; Legend Shields start next Legend day. | [Get Ready for Ranked][launch] |
| 2025-09-02 | Previewed mode split, trophy resets per Tournament, initial placement using old trophies and Town Hall, unranked placement by Town Hall, and difficulty modifiers at the top. These were plans for October, not rules already live in September. | [Ranked preview][preview] |

Archive review found no later replacement of the April structural rules through October 4. In particular, [May 22][may-state], [May 26][may], [July 9][july] and [August 30][august] were checked. May/August references to Titan/Legend **Clan War League** modifiers concern clan wars, not this individual ladder. September/October archive posts through October 2 concern events/shop changes. Current lower-league percentages have undated revisions that cannot be honestly assigned a release date.

## What is not officially stated or remains conflicting

- A fixed Legend I capacity, fixed worldwide Legend II group count or fixed weekly/Season promotion total is not published in the reviewed sources.
- Exact matchmaking weights, trophy bands, selection algorithm and opponent-assignment processing time are not published; similar trophies are only the main stated factor.
- Filled-defense averaging weights, rounding, empty-history fallback and ordering relative to weekly/Season adjustments are not specified.
- Exact current per-tier lower-league percentages, small-group rounding and tie handling are not fully specified by the official grouped table.
- A complete current weekly sign-up closing window, Legend I delayed-entry deadline, extra fifth grace week or first-week demotion exemption was not established.
- Generic support calls all Legend Tournaments monthly and lists a 100-player Legend I group; specific support/releases establish weekly III/II and four-week I, without establishing a 100-player I matchmaking pool.
- Zero-star defensive scoring, weekly layout lock timing and shield conversion on demotion conflict between the sources identified above.
- Current Legend I-specific Gem prices, seven-day shield eligibility and purchase restrictions remain less certain than the general published shop table.
- A shield purchase/expiry field in the live API was not verified; the example establishes retained observations and an inference, not an official shield flag.

## Sources and review coverage

Reviewed 46 blog posts from the September 2, 2025 preview through the latest October 2, 2026 post, following the archive page by page: [1](https://supercell.com/en/games/clashofclans/blog/), [2](https://supercell.com/en/games/clashofclans/blog/page/2/), [3](https://supercell.com/en/games/clashofclans/blog/page/3/), [4](https://supercell.com/en/games/clashofclans/blog/page/4/), [5](https://supercell.com/en/games/clashofclans/blog/page/5/), [6](https://supercell.com/en/games/clashofclans/blog/page/6/), [7](https://supercell.com/en/games/clashofclans/blog/page/7/), [8](https://supercell.com/en/games/clashofclans/blog/page/8/). Archive pages accessed 2026-10-04. Also reviewed nine support articles, eight Wiki pages and the 2019 shield FAQ: **64 source pages**, excluding the eight archive indexes and the shop image. Unrelated archive posts were screened for relevant rules, not used as evidence for them.

Dated primary sources, with short exact excerpts for key numbers:

- 2026-08-30: [August Update][august]. Clan War League modifier changes, not new individual Ranked tier rules.
- 2026-07-09: [July Balance Update][july]. Checked for Ranked/Legend changes.
- 2026-06-15 article publication: [The Anime Fury Update][june]. Ranked achievement rewards.
- 2026-05-26: [May Update][may]; 2026-05-22: [State of Gameplay, Part 2][may-state]. Checked for subsequent rule changes.
- 2026-04-27: [The Sound of Clash Update][april]. Exact excerpts: "Top 3 players per group promote each week"; "Players below Rank 10,000 demote each week"; "Monday at 5 PM UTC".
- 2026-04-16: [Big changes are coming to Ranked this April][announcement]. "Top 12,500 players move to Legend I"; "Next 50,000 players move to Legend II"; "Top 5 players per group promote each week".
- 2026-03-24: [The State of Gameplay and What's Ahead][march]. Structural preview.
- 2026-02-23: [The February Update has escaped][february]. Exact battle-count and promotion/demotion tables.
- 2026-01-28: [Changes coming to Ranked Mode & League Modifiers][january]; 2026-01-26: [Upcoming changes to Ranked Mode][january-preview]. Maintenance values versus preview intentions.
- 2025-11-17: [Town Hall 18 Crash Lands][th18]. Matchmaking, inactivity and bracket changes.
- 2025-10-06: [Get Ready for Ranked][launch]; 2025-09-02: [Battle and Ranked Modes Are Coming][preview]. Preview excerpt: "Ranked Battles unlock for all players in Town Hall 7".
- 2019-06-17: [Legend League redesign FAQ][old-faq]. Historical shield evidence only: "1-Day Shield: 4-day cooldown"; "2-Day Shield: 7-day cooldown". Its other superseded rules are not current authority.

Undated primary sources, all **accessed 2026-10-04**:

- [Ranked Leagues][ranked]. Grouped ladder, minimum sizes, grace and floors. Excerpt: "four Tournament Weeks".
- [Intro to Legend League][legend]. Daily allocation, optional/automatic enrollment, four-week Season and trophies. Excerpts: "Take on eight other Legend I players each day"; "5:00AM UTC"; "Tournaments in Legend I last for 4 weeks."
- [Tournaments & Matchmaking in Legend League I][matchmaking]. Weekly demotion, Season reset and trophy-based matching.
- [Trophy Calculation in Legend League][legend-scoring]; [Ranked Battles & Trophy Calculation][ranked-scoring]. Different defensive scoring.
- [Battle vs Ranked Battle][modes]. Ordinary Battles and Ranked standing.
- [Shields in Legend League][legend-shields]. Excerpts: "Legend Shields are exclusive to Legend I"; "Legend Shields are active for entire League Days".
- [Shields][shields], including its [shop image][shield-image]. Gems, stacking, same-duration cooldowns; image transcriptions: "100", "150", "250", "4d", "7d", "35d".
- [Clan Castle & Layouts in Legend League][layouts]. Next-day defensive changes.

Wiki secondary sources, all **accessed 2026-10-04** through the Wiki's public article interface:

- [Ranked Battles][wiki-ranked]. Individual tier/floor tables, scoring detail and tie order. Marked outdated/under maintenance; its three-week inactivity wording conflicts with current official four-week grace, and its general history is incomplete.
- [Legend League Tournaments][wiki-legend]. Daily mechanics and zero-star defensive exception; the history heading is empty. Its historical 5,000-entry opening and Helsinki-time wording are not substitutes for current promotion and UTC rules.
- [Trophy Leagues][wiki-leagues]. Explicitly marked removed content; the old Bronze–Legend ladder and calendar-month cycle are historical.
- [Trophies][wiki-trophies]. Current weekly/four-week distinction; its Original Use section is historical.
- [Version History/2026][wiki-2026]; [Version History/2025][wiki-2025]. Cross-checks of releases, not independent official announcements.
- [Shield][wiki-shield]. Durations/cooldowns and Legend protection; some surrounding examples still use old automatic-shield rules.
- [Player Profile][wiki-profile]. Profile context; no public-API shield field established from this page.

Clash Lens figures and the player example are our read-only measurements, not Supercell or Wiki claims. No game-account experiment or live API request was performed. The observed shield-duration limitation and source conflicts remain follow-up questions; this reference changes no collection or scoring behavior.

[preview]: https://supercell.com/en/games/clashofclans/blog/news/battle-and-ranked-modes-are-coming-to-clash-/
[launch]: https://supercell.com/en/games/clashofclans/blog/release-notes/get-ready-for-ranked-update/
[th18]: https://supercell.com/en/games/clashofclans/blog/release-notes/town-hall-18-crash-lands-update/
[january-preview]: https://supercell.com/en/games/clashofclans/blog/news/upcoming-changes-to-ranked-mode/
[january]: https://supercell.com/en/games/clashofclans/blog/news/balance-changes-4/
[february]: https://supercell.com/en/games/clashofclans/blog/release-notes/the-february-update-has-escaped/
[march]: https://supercell.com/en/games/clashofclans/blog/news/the-state-of-gameplay-and-whats-ahead/
[announcement]: https://supercell.com/en/games/clashofclans/blog/news/big-changes-are-coming-to-ranked-this-april/
[april]: https://supercell.com/en/games/clashofclans/blog/release-notes/the-sound-of-clash-update/
[may-state]: https://supercell.com/en/games/clashofclans/blog/news/state-of-gameplay-part-2/
[may]: https://supercell.com/en/games/clashofclans/blog/release-notes/may-update/
[june]: https://supercell.com/en/games/clashofclans/blog/release-notes/the-anime-fury-update-is-here/
[july]: https://supercell.com/en/games/clashofclans/blog/news/july-balance-update/
[august]: https://supercell.com/en/games/clashofclans/blog/release-notes/august-update-3/
[old-faq]: https://supercell.com/en/games/clashofclans/blog/news/faq-legend-league-redesign/
[ranked]: https://support.supercell.com/clash-of-clans/en/articles/ranked-leagues-4.html
[legend]: https://support.supercell.com/clash-of-clans/en/articles/legend-league-4.html
[matchmaking]: https://support.supercell.com/clash-of-clans/en/articles/legend-league-matchmaking-3.html
[legend-scoring]: https://support.supercell.com/clash-of-clans/en/articles/legend-league-attacking-defending-3.html
[ranked-scoring]: https://support.supercell.com/clash-of-clans/en/articles/trophy-matchmaking-and-calculation-in-multiplayer-2.html
[modes]: https://support.supercell.com/clash-of-clans/en/articles/battle-vs-ranked-battle-whats-the-difference.html
[legend-shields]: https://support.supercell.com/clash-of-clans/en/articles/legend-league-shields-3.html
[shields]: https://support.supercell.com/clash-of-clans/en/articles/multiplayer-attacking-defending-2.html
[shield-image]: https://support.supercell.com/images/shieldscc.webp?v=1759308102
[layouts]: https://support.supercell.com/clash-of-clans/en/articles/ll-clan-castle-layouts-5.html
[wiki-ranked]: https://clashofclans.fandom.com/wiki/Ranked_Battles
[wiki-legend]: https://clashofclans.fandom.com/wiki/Legend_League_Tournaments
[wiki-leagues]: https://clashofclans.fandom.com/wiki/Trophy_Leagues
[wiki-trophies]: https://clashofclans.fandom.com/wiki/Trophies
[wiki-2026]: https://clashofclans.fandom.com/wiki/Version_History/2026
[wiki-2025]: https://clashofclans.fandom.com/wiki/Version_History/2025
[wiki-shield]: https://clashofclans.fandom.com/wiki/Shield
[wiki-profile]: https://clashofclans.fandom.com/wiki/Player_Profile
