# Domain contract

## Read this file before changing domain behavior

This file owns the durable meanings, invariants, evidence states, and
versioned calculations for Legend I. It does not define current product scope
or implementation status. Report a conflict with code, tests, migrations, or a
live GitHub issue; do not choose silently.

The [dated product map](product-status.md) identifies requirements not yet
implemented. In particular, automatic entry from all tag sources and the new
completed-season views below are agreed behavior, not claims of deployed support.

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
- A newer valid Legend I profile may advance the confirmed anchor from its
  current season ID and observation time. That ID must be a Monday at 05:00 UTC,
  no later than the observation, and aligned in exact 28-day steps from the
  confirmed bootstrap anchor. Derive the previous boundary by subtracting 28
  days; do not require or trust a previous-season field in the profile. If accepted profiles disagree, retain the last confirmed
  anchor and mark the new source contract as conflicting.
- A player can have up to 8 attacks and 8 defenses in one ranked day.
- A battle belongs to the ranked day of its report's `battleTimestamp` less
  5 minutes. No new-day attack can start in the first 5 minutes after the
  Reset, so a report stamped 05:00:00 to 05:04:59 UTC finished an attack of
  the day before. The attacker's report is stamped when the attack ends, often
  1 to 4 minutes after the defender's, so the two reports of one battle can
  straddle the Reset; from 2026-09-29 to 2026-10-02, 90 battles did. Apply
  this to attacks and defenses alike, and keep the original timestamp. The
  Reset stays at 05:00 UTC, and Mondays use the same 5 minutes.
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
- Confirm that a submitted tag represents a real player using official evidence;
  valid spelling alone does not establish existence. Retain confirmed tags
  regardless of Town Hall or league, then check Legend I eligibility.
- Discover additional tags only through official Clash of Clans API sources, including official leaderboards, clan data, and opponents present in official battle logs.
- Normalize and deduplicate a tag before adding it to the known-player registry.
- Apply this same identity rule to overlapping imported lists, exact-tag searches
  and player visits, saved/group/verification inputs, and official discoveries.
  Repeated input reuses the existing identity and history. A saved/group entry
  does not prove ownership or bypass eligibility.
- Discovery and eligible tracking are automatic; no separate Start tracking
  button or sign-in is required for a public tag lookup. Name search covers
  actively tracked players and players with recorded history. Other known tags
  remain directly accessible. A non-Legend-I page shows the eligibility
  explanation and any history, without a full current profile section.
- Discover clan members when a clan is first encountered and check its member
  list daily thereafter. Keep duplicate work bounded without dropping new tags.
  Starting collection from supplied lists does not wait for clan discovery.
- Start the official existence/eligibility check immediately when a previously
  unknown tag is encountered. Add it to active tracking immediately after
  confirming that the player is in Legend I.
- When newer valid eligibility evidence shows that an actively tracked player left Legend I, retain the tag and history but remove the player from active Legend I tracking.
- Recheck already-known players every Monday after Reset at 05:00 UTC, using
  post-Reset profile evidence. Fresh post-Reset profiles from active collection
  can satisfy that check; inactive players need the weekly eligibility pass.
  This is a Monday boundary, not seven days after each player's last check.
  Repeated submissions, overlapping imports, visits
  and daily clan membership checks reuse that week's result. They do not each
  start a new eligibility check. This supersedes the earlier every-rediscovery
  recheck rule. Initial checks for new tags and bounded retries of unfinished
  or failed work are separate from another routine weekly pass.
- Active players continue supplying current eligibility evidence through their
  normal profile collection; the weekly known-player check does not replace or
  slow their live collection. The launch planning target is 12,500 active players
  within a total pool initially containing 22,157 supplied tags, not 22,157
  continuously tracked players. Actual existence/eligibility remains to be checked.
- Retaining inactive tags must allow later ranked-tournament support without re-creating player identity or losing history.

### Player page freshness

- The player page's Updated time and freshness use the retained last-confirmed
  time for the profile currently shown.
  Show the time without adding technical freshness panels.
  Advance it when a successful check matches the applied profile in the fields
  Clash Lens uses, or when a changed profile is applied. A failed check or a
  changed response awaiting processing preserves the previous confirmation. Older
  responses cannot replace a profile confirmed more recently. The displayed
  time never moves backward. "Stale saved profile" appears when this time is
  more than 15 minutes old, even if the saved trophies have not changed.
  The private player response's `observed_at`, `age_seconds`, `freshness`, and
  screen-ready provenance use this same time; see
  [`get_player_page`](../python/src/clashlens/api_players.py).
- When that time is more than 15 minutes old, the page also shows its age.
  Battle history updated shows separately when the newest shown daily result
  was published, with its age once that is more than 15 minutes old, or "not
  yet". A successful battle log request alone does not move it. These ages, and
  the delayed-updates notice's ages, keep advancing every 30 seconds while the
  page stays open.

### Delayed updates notice

- Every page shows one "Updates are delayed" notice when no official API
  profile or battle log answer has succeeded for 15 minutes, or when work
  needed to publish shown results (processing a saved response, or rebuilding
  a player's Legend day) was saved over 15 minutes ago and is still unfinished,
  including retries and waits for storage. Work scheduled for later, such as
  a day's recalculation after its Reset, counts only from when it is due. It names only the delay the data
  shows: no API answer since a time, or when the oldest waiting data was
  saved. Saved values stay visible with their age; a delay
  never turns them into zero or removes a player. Affected Legend days stay
  incomplete until the existing battle log continuity checks prove otherwise.
  See [`get_update_status`](../python/src/clashlens/api_status.py).

### Completed-season player history

- Retain each day's EOD, attack gain, defense loss and change from the previous
  Legend day's EOD. Day 1 uses 5,000 as its comparison baseline; Day 28 EOD is
  the season-ending trophy count. EOD 5,050 then 4,950 means a change of -100.
- Missing EOD evidence stays unknown. Keep this movement separate from battle
  results and reset adjustments; do not fold a boundary reset into attack gain
  or defense loss. Preserve the evidence that explains a difference.
- A player page's Daily Legend log shows only the current Season's days, each
  numbered from that Season's start. An ended Season's saved days appear under
  that Season in the page's Seasons list, dated by its end. That list offers
  only Seasons with Clash Lens days; one known only from the game's league
  history is left out. A link to a day of an ended Season opens that Season
  with the day marked, or says no Legend log is saved for that date.
- In a Season's saved-day view, final rank means the official in-game placement from
  Clash of Clans league history, which names a Season by the Reset that ended
  it. Until that placement is saved, show it as not published yet; never fall
  back to the Clash Lens leaderboard position. The separate Older Seasons table
  follows the [website behavior](../website/README.md).

### Live Leaderboard ordering

- The **Live Leaderboard** orders actively tracked players by the newest valid trophy observation that Clash Lens has accepted for each player. Its **Rank** means position among players tracked by Clash Lens, not a claim of complete global coverage or one simultaneous official observation.
- Keep a player in this ordering when a later request is missing, delayed, malformed, or unsuccessful, unless the not-found rule below applies. Change the player's trophy value only when Clash Lens accepts a newer valid observation. Remove the player from active Legend I tracking only when newer valid evidence shows that the player is no longer eligible.
- Leave out a player when their most recent profile check returning 404 (player not found, usually a banned or deleted account) is newer than their last successful profile check, or when there is a recorded 404 and no successful profile check. Clash Lens durably records both times, keeps tracking and checking that player at the normal pace, and the next newer successful profile check puts them back. Timeouts and server errors neither hide a player before any 404 nor restore one after a 404.
- By the same rule, the player's page says Clash of Clans did not find the player instead of that Clash Lens is tracking them, warns that the saved trophies are from the last check that found them and calls the profile uncertain, until a newer successful profile check. Their saved Legend days stay as recorded. The player response names the time of that 404 in `profile_not_found_at`.
- Leave out a player whose current profile does not name the calendar Season, such as one still naming the previous Season after a Season-opening Reset. On a Season's first Legend day, also leave out a player whose current trophies still equal their trophies on the ended day's published Daily board, unless that value is 5,000, whatever Season the profile names. On that day also leave out a Legend I player whose trophies are not 5,000 and are not explained by that day's recorded battles: the gap from 5,000 must equal their recorded net change or, when it does not, be at most 40 trophies per recorded attack or defense. This needs no published Daily board. Those trophies predate that player's Season reset. Report how many tracked players wait this way, and rank each one again once a profile names the new Season. The Daily board for an ended day likewise leaves out a player whose saved profile names another Season than that day's, so the last day of a Season keeps its pre-Reset values.
- Record Last updated, age, and freshness state with each leaderboard entry. Last updated follows the [player page freshness rule](#player-page-freshness). Show that time on the public Live Leaderboard without adding technical freshness panels. Age alone does not remove a player from this ordering or from a snapshot's cumulative Top-N cohorts and rank bands, but the snapshot and its analytics must record how much membership uses old data.
- Live Leaderboard entries are fresh through 600 seconds since Last updated and stale beyond that. Population counts use this same ten-minute limit. See [alert conditions](alerts.md) for when this raises an operator alert.
- Order all Live Leaderboard entries by newest accepted trophies descending. Order equal trophies, on the Live Leaderboard and every Daily board alike, by the player's Season average attack destruction, highest first: the destruction percentages of the Season's attacks their own battle log recorded, zero-star attacks included, summed and divided by those attacks, compared exactly, not rounded. A player with no recorded attack comes last. Then more attacks come first, then the MD5 hash of the normalized player tag, then the tag. The owner chose this on 8 October 2026: in the October 2026 data the game's own Season-end placements agreed with average destruction on 75.4% of equal-trophy pairs. A Daily board counts the attacks of the Season's Legend days before its Reset. The Live Leaderboard counts those recorded so far, recounted every five minutes, so its order can lag a new attack by up to that long. Boards ordered this way are labelled `tracked-trophies-attack-destruction-v2` (Live) and `tracked-player-order-v2` (Daily). An official rank is separate provenance, is not a public leaderboard column, and does not change a Live Leaderboard Rank.
- Never use fresh randomness for a snapshot tie-break. The same tag, trophies, Season attack counts and ordering-rule version must reproduce the same position.
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
- Battle rows do not include the trophy change directly. Clash Lens derives it from stars and destruction using the versioned table in `docs/data/legend-trophy-allocation-v2.csv`, which follows Supercell's published formula: a 2-star attack at 55% destruction is worth 17 trophies, and 56% is worth 18.
- `docs/data/legend-trophy-allocation-v1.csv` is the earlier table. It differs only in giving a 2-star attack at 55% 18 trophies. Battle parser `supercell-battle-parser-v3` reads the same live rows as `supercell-source-parser-v2` but uses table v2; results saved under parser v2 or v1 keep their table v1 numbers until a separate repair recalculates them.
- For each star count, use the last trophy value whose minimum destruction percentage is not greater than the battle's destruction percentage. Reject impossible or out-of-range star and destruction combinations instead of guessing.
- A 0-star attack at 0 through 9 percent destruction gives the attacker 0 trophies. Other 0-star attacks give the attacker the amount in the table, but the defender loses 0 trophies.
- For 1-star, 2-star, and 3-star attacks, the attacker gains the table amount and the defender loses the same amount.
- Store the trophy-allocation rule version with each calculated battle result so later rule changes can replay saved evidence.
- A battle that produces zero trophies is still an exact event and counts toward the player's attack or defense count.
- Repeated polls overlap. Clash Lens must not create duplicate battle events from repeated observations.
- Preserve every raw source observation, including its fetch time and untouched response body.
- Preserve successful observations when a paired endpoint request fails. Mark the collection attempt incomplete until the missing evidence is collected.
- Start tracking a valid tag when Clash Lens first confirms it for active tracking.
- Reconstruct all retained timestamped Legend I events available at first observation. When a player's first saved battle log was saved on Day 1, or holds their battles from an earlier day of the Season it was saved in, recalculate that day and every later saved day of the Season, again whenever an older battle log of theirs is processed after newer ones. A Day 1 recalculation waits until both that log and the player's accepted Legend I profile naming the Season are saved, whichever comes last.
- Mark a day `not_enrolled` (shown as "Not enrolled") only when it is proven: the player had no battle that day, a saved Legend I profile observed after the day ended still showed Season ID 0 (not signed up), and a later saved profile in the same Season shows them signed up for it. Whenever either profile is processed and the proof holds, recalculate each such day of the Season, including days with nothing saved yet. A player who never signs up, or whose earlier days have no such profile, keeps those days as they were.
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
- A ranked day has **continuous battle-log coverage** when valid battle-log observations form a chain from the battle-log response in the start reset-baseline sweep through the battle-log response in the next boundary's end sweep. Each consecutive response must either contain fewer than the official 50-row maximum or share at least one saved row with the preceding response, a Legend battle or any other battle, since every row carries its own battle time. A full 50-row response with no overlap creates a coverage gap.
- A player first tracked after a day's start has no start-sweep battle log. Their first saved battle log starts the chain instead when it was saved on or after the day's start and either its oldest row is older than the day's battles or it has fewer than 50 rows, the whole log the game keeps. Saved after the day's last battle, with no end-sweep battle log, it is the whole chain.
- Every new row exposed by the chain must be retained and processed. A malformed, unsupported, or identity-conflicting Legend I row creates a visible coverage gap until later valid evidence resolves it.
- A live row with no `battleTimestamp` is malformed: its `battleTime` is the battle's length in seconds, never a date, so it is a gap, not a battle fought in 1970 (lab finding, synthetic input); only an archived text date, or a number of seconds since 1970 that reaches 2001, still stands in. A row that is not an object, whose `battleType` is not text, or whose type spells "legend" other than exactly is a gap too (`unsupported_legend_row`), not a row of another mode to ignore, and such rows count as content in the collector's change check so a new one is always saved and processed.
- A Legend row with no opponent tag, 0 stars, 0% destruction and a 0-second battle (`battleTime` 0) is the live log's "no opponent, no battle" row, which logs keep for days. It is retained as a rejected row but is not a battle: it adds no attack or defense, except as a used slot for the [automatic defense adjustment](#automatic-defense-adjustment), and creates no coverage gap. Any other row without an opponent still creates one.
- Poll success percentage and elapsed time alone do not prove coverage. Use source-row continuity and boundary evidence.
- A ranked day has **complete evidence coverage** only when its start and end sweep IDs each link a valid profile and battle-log response, it has continuous battle-log coverage between those responses, and it has valid applicable Legend I rows, established attack and defense counts, and known boundary adjustments. A legacy profile-only reset attempt cannot prove Complete. A Day 1 started by the Season rule with no reading from the Season-opening Reset needs no start sweep: the first saved battle log that starts its chain stands in for one.
- Late evidence may close a coverage gap and create a corrected ranked-day version. Do not rewrite the previous version in place.
- Once a ranked day has ended and its Reset work is done, at least 30 minutes after the Reset, its replaced versions and their daily logs are deleted unless a publication, analytics row, queued correction or the next day's newest version points at them. Players only ever see the newest version, and later corrections are calculated from it. See [history-retention.md](history-retention.md#extra-ranked-day-copies).

## 5. Derived ranked-day states and adjustments

### Inferred shielded days

- A **shield** prevents a Legend I player from attacking and prevents other players from attacking them for 1 or 2 ranked days.
- The official API does not directly confirm shield state. Clash Lens may classify a day only as **inferred shielded**.
- Infer a shielded day only when all of the following are true:
  - The player remained eligible for active Legend I tracking.
  - The ranked day has complete evidence coverage.
  - The player's trophies did not change across the ranked day.
  - The battle log contains no Legend I attack or defense event belonging to the ranked day.
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
- A "no opponent, no battle" Legend row (no opponent, 0 stars, 0%, a 0-second battle) is not a battle, but for this adjustment only the game counts it as a used attack or defense slot, a defense one with no loss. The **used defense slots** below are the defense events plus these defense rows, and the attacks are the attack events plus these attack rows; each row counts once, on the day of its report time. On 6 October 2026, 3,623 of 3,643 Day 1 results checked against a profile read after the loss matched counting them, against 3,581 without; the other 20 remain unexplained. They never become battle events or enter army or battle analytics. The Reset settlement check counts them the same way.
- Calculate an automatic defense adjustment only when the previous and current days have continuous battle-log coverage and retained evidence establishes their defense-event counts and observed event losses. If any formula input may be incomplete because collection evidence is missing, do not replace the unknown evidence with an adjustment.
- A Season's Day 1 leaves the previous Season's last day out: its average uses Day 1's own defenses only, so only Day 1 needs that coverage. On 6 October 2026, 1,119 Day 1 results with differing averages matched a profile read after the loss and before any Day 2 battle using Day 1 alone, and none using the previous Season's day.
- For a current day with 1 through 7 used defense slots, calculate the positive loss magnitude per missing defense as:

  `floor((previous day observed event loss + current day observed event loss) / (previous day used defense slots + current day used defense slots))`

- **Observed event loss** in this formula excludes every automatic defense adjustment.
- Multiply the floored loss by `8 - current day used defense slots` to calculate the total automatic defense adjustment. On a Season's Day 1 only, a player with at least as many attacks as defenses is charged for `attacks - defenses` missing defenses instead, so equal counts lose nothing. Profiles read on 6 October 2026 after the loss and before any Day 2 battle: 66 of 69 players with more attacks than defenses lost `attacks - defenses` times the average and 3 lost `8 - defenses`; of 120 with equal counts, 59 still showed the Reset reading after 05:14 and 6 lost `8 - defenses`; 37 of 38 with fewer attacks lost `8 - defenses`. Measured on one Day 1, so check it again on the next. Recounted with "no opponent, no battle" rows, this matched 1,597 of 1,615 Day 1 results, against 1,442 for `8 - defenses` and 1,566 for `attacks - defenses` alone.
- A day with zero used defense slots may be charged for all 8, averaged over the previous day alone, or charged nothing, and its battles do not say which. On 6 October 2026, 3 such days lost exactly that (304, 272 and 248) while 86 kept their trophies, including every one read again later that day, with the same battle counts on the days around them. So the charge is taken only when a reading after the Reset shows it (the reading rule below), on an otherwise clean day whose previous day's battles are all known, and never on a Season's Day 1; a reading showing the charge outranks an earlier one showing none, because the charge can land at any time. On a day ending at the weekly raise, a charge that would bring the end to 5,000 or below is not taken, because the raise hides it: the day stays uncharged with inferred confidence. Such a day's calculated end does not start the next day until a reading settles it. Uncharged days are calculated again two hours after their Reset.
- A **calculated automatic defense adjustment** uses the averaging rule when the reset outcome cannot yet isolate the exact adjustment.
- The game applies the adjustment about 7 to 13 minutes after the Reset, and the Reset reading is usually taken before that. A Reset reading that sits exactly the calculated adjustment above the ended day's calculated end is taken as read before the adjustment: the day is complete with the adjustment still calculated, its confidence is inferred, and the next day starts from the reading less that adjustment, its **unsettled loss**. The ended day's saved next start and the next day's saved start are both that settled value, and each saved result records the reading and the loss taken off it. Any other gap is still a mismatch. On 2 and 3 October 2026 that explained 589 and 998 ended days and, of the next days a later reading could check, 728 of 733 and 503 of 506; where a later reading existed, the first trophy change after the Reset was exactly the calculated adjustment in 623 of 625.
- **The reading rule.** Every profile of the player read from the day's end Reset to the next Reset judges the day's end (`reading_rule.py`): at the moment it was read, its trophies must equal the day's start plus every battle the game had shown by then, less the automatic loss once the game applied it, plus any new-day battle the profile already showed. An attack shows in the attacker's profile about 4 minutes after its report and a defense about 2 minutes after it starts (measured 7 October 2026), either can take longer, so a battle reported within 10 minutes before the reading may or may not be in it and the reading is read both ways. The automatic loss landed no earlier than 7 minutes 38 seconds after the Reset in 733 sampled days (6 and 8 October 2026) and as late as 05:29 in lab data; neither bound is official, so a reading may show the loss at any time after the Reset and its value says whether it did. The last clean reading, one with every battle landed and none in flight, decides. Only trustworthy readings decide; a confirm-only one (below) confirms only when none did, and a reading showing no loss never outranks an earlier one showing it landed, since a loss cannot be undone. One that equals the ledger proves the day, outranking every contradiction before it: the day is complete and exact when it shows a certain loss landed, and complete with inferred confidence when read before the loss (the reading then settles the day's battles, the loss stays calculated as an **unsettled loss**, and the next day starts from the day's calculated end, with nothing taken off it again). One that equals nothing contradicts the day (`trophy_equation_mismatch`), even after an earlier reading equalled the ledger. A reading that fits only with a battle not yet shown, or read one way, is a guess: it completes the day with inferred confidence only when the day's start is proven and no clean reading contradicts it, recording the battles it missed (`next_start_battles_after_reading`). The Reset pair's own profile is one such reading; one taken at or after the player's first new-day battle is judged like any other, less the new-day battles it already showed, but can only confirm, since a new-day battle it shows may not be known yet, which is how the slow 7 October 2026 Reset's 3,688 late readings become usable. A Legend I profile naming Season 0, which the game also sends to signed-up players, and any profile read after the last of the player's battle logs that are continuous from the day's end Reset log (no row gap, and each full log sharing a row with the one before), can confirm the day but never contradict it: the first because the profile is not trusted, the second because a battle after that log is not known yet. A reading taken after an unreadable row of a battle log saved since the Reset is not used, and none is when even that row's time is unreadable; a reading with a disputed battle in flight is not judged. Each saved result records the reading that judged it (`end_reading` in its evidence) and, when the Reset reading was short of it, the difference (`next_start_reading_correction`). On 6 October 2026, `#P20G0CUJY` read 4,766 at 05:02:41 with all 308 of its attack gains missing and 5,074 at 05:09:56; on 7 October `#2GL8CJL` read 4,839 at 05:00:31 before its 05:02:15 attack added 40; the 2, 3 and 5 October Resets read 589, 998 and 2,889 days before their loss landed: one rule reads all of them. A day not proven by a reading, one ending in a mismatch, or a zero-slot day showing no charge is calculated again two hours after its Reset, when later readings have usually been saved; when that changes its state, end or next start, the following saved day of the Season is calculated again with it, and so on until a day's result stays the same. Profiles read in the first 30 minutes after a Reset are always processed, even after a newer one, so such readings are kept.
- Each saved result records the reconciliation rule version label it was built under (currently `legend-ranked-day-reconciliation-v3`). The label is raised only when every saved result must be rebuilt; a narrower rule change, such as Season Day 1 averaging its own defenses, counting "no opponent, no battle" rows, counting any shared saved row as battle-log overlap, charging a zero-defense day or settling a Reset reading from a later one, keeps the label and raises the day-rule revision instead, and the Season's saved days and boards are then recalculated once with `republish-current-season --repair`; see [operating](operating.md).
- A **confirmed automatic defense adjustment** requires continuous battle-log coverage for the previous and current days (on a Season's Day 1, Day 1 alone) plus valid start and end reset baselines whose trophy values jointly isolate the adjustment. Trophy reconciliation alone does not prove its cause.
- Show the adjustment separately from battle events and identify whether it is calculated or confirmed.
- Automatic defense adjustments affect ranked-day trophy reconciliation but never contribute to army usage, three-star rate, or other battle-event analytics.

### Weekly and season trophy resets

- At each Monday 05:00 UTC boundary, a player who remains in Legend I with fewer than 5,000 trophies resets to 5,000 trophies.
- At the start of each 28-day Legend I season, every Legend I player resets to 5,000 trophies. Excess trophies above 5,000 become Legend trophies under the official game rule.
- Store a weekly or season reset as an explicit boundary adjustment. It is not a battle, attack gain, defense loss, or automatic defense adjustment.
- At every Reset, a reading whose profile fails the Reset checks never becomes the next day's starting trophies or the ended day's next starting trophies. It stays saved as evidence and those totals stay unavailable, unless the Season rule below gives them. A day whose Reset reading cannot start it, rejected, late or missing, on any day but a Season's Day 1, instead starts from the day before's calculated end once that day's battles are all known: continuous battle logs, a saved result and no 9th attack or defense, whether or not that day's own readings were usable; never from a day the player was not enrolled or not in Legend I, never after a reading whose league we cannot recognise, never from a day with no defense slot used that no reading settled, since only a reading shows its automatic defense loss for all 8, and never onto a weekly Monday where any profile read from its Reset or its Reset reading, before the next Reset, shows a league below Legend I, which also keeps the ended day from the raise to 5,000. Such a start is recorded with source `previous_day_end` and the version it came from, its day is never more than inferred, and the player page labels the start Calculated. That day before's defenses also average the automatic defense loss on the same terms. On 7 October 2026, 3,574 of 13,253 days held every battle and no start for this reason alone, after the slow 7 October Reset rejected 3,688 readings.
- An untrustworthy profile, one reporting Season ID `0` or another unknown or conflicting Season, a league we cannot recognise, or another player, is never used at all: not as a starting total, not as the current profile, and not for Season reset status. Its Season stays unknown rather than waiting for a Season reset. The one exception: the player's own page may show a Legend I profile with Season ID `0` (its name, clan and trophies) to explain why there are no current results.
- A trustworthy profile rejected only as a Reset reading still becomes the current profile. One naming a recognised league below Legend I with a valid Season, such as Legend II after a demotion, may decide eligibility and Season reset status, but never a Legend I day's saved start. For one collected after the day's first battle, the player page may calculate the day's start as current trophies minus the battles recorded since, labelled Calculated. On a Season's first Legend day the player page shows a start, saved or calculated, only when it is exactly 5,000; any other value leaves that start unavailable.
- At every Reset, a reading whose accepted profile names another Season than the Reset's calendar Season never becomes the next day's starting trophies or the ended day's next starting trophies, whatever its value. At a Season-opening Reset, a Legend I reading is a starting total only when it shows exactly 5,000 trophies, the start every Legend I player gets. The reading stays saved as evidence and those totals stay unavailable, except by the Season rule: when a Season-opening Reset's reading cannot give the start, for any reason, including a rejected or missing profile, and the player has an accepted Legend I profile naming the new Season, whenever it is seen, both totals are 5,000 by the Season rule and the player counts as Legend I there. The reading's own trophies stay unused. A player first tracked after the Season-opening Reset, with no reading from it, starts Day 1 at 5,000 by the Season rule the same way, once they have that profile. A player who signs up later in the Season starts their sign-up day at 5,000 by the Season rule too: at any Reset whose reading is a Legend I profile at 5,000 naming Season 0 (not signed up), when no profile before it named the Season, no Legend battle of the Season came before it, and a profile after it does name the Season. On 6 October 2026, `#YPG2LRYQ` read Season 0 at 05:00, signed up at 05:19 and ended the day at 4,937 after gaining 230 and losing 293, exactly from 5,000; 48 such sign-up days had no start. The game also sends Season 0 to players already signed up, so a Season-0 reading after the player's first profile naming the Season, or after their first Legend battle of the Season, gives no start. Saved sign-up days are recalculated with `republish-current-season --repair`. The Reset's evidence is complete only when its battle log also passes the Reset checks; otherwise the days it bounds stay incomplete with those totals. A day starting that way records Season rule as its start's source in its saved evidence, is never more than inferred, and the player page shows that start labelled Season rule. The first Reset of the new Season that finds the Season rule gives a player's totals rebuilds, once, the ended Season's last day and every saved day of the new Season, so a player who first logs in on any later day of that Season is covered too. Later Resets skip the rebuild once the newest saved Day 1 starts by the Season rule and was built on the ended Season's newest saved last day, which ends by it. Player pages, group comparisons, group member lists and player search show that player as waiting for their Season reset instead of showing the old total as current trophies, and the tracked-player average leaves them out. A group comparison or group member list left open across the Season-opening Reset switches its members to that waiting state at the Reset and reloads before showing current trophies again.
- Home, the Live Leaderboard and a current player page left open across a Season-opening Reset stop showing their saved trophies as current at that Reset and reread saved data, then reread at most once a minute while visible until the newer data arrives and while any player still waits for their Season reset. See [`SeasonReread.ts`](../website/app/components/SeasonReread.ts).
- Reconcile the ended ranked day before the weekly or season reset. Apply the boundary adjustment after that reconciliation, and use the adjusted value as the next ranked day's starting trophies.
- Show each boundary adjustment and its official rule version separately. Do not include it in offense, defense, army, or battle-outcome analytics.

## 6. Ranked-day and leaderboard snapshots

- At 05:00 UTC, event ownership moves to the new ranked day, except for reports stamped in the next 5 minutes, which still belong to the ended day (section 1). A battle first observed later is still added to the ended day when it belongs there.
- A battle saved after its ended day's result was published is added to that
  day by a once-per-Reset check. From 05:30 UTC, every 10 minutes until it
  runs, the worker waits for the Reset sweep to finish and for every response
  fetched before it finished to be processed, except
  [settlement checks'](collector-polling.md#settlement-check-20-minutes-after-reset).
  It then looks at every battle
  on the previous 7 Legend days with a report saved within 5 minutes of its
  day's end or later. For each player who reported that battle, the day's
  latest saved result must list their report and whether the two players'
  reports currently agree; a late report from the other player can change
  that, so both players are checked. Days of a season whose detail is retired
  cannot be recalculated and are skipped. Older days are not read or
  corrected, because the recalculation supports only the current and previous
  Season. A saved day in those 7 days, or
  today if it already has a saved result, whose latest result was built from
  an older version of the day before it than the
  one now current also counts as a mismatch, so a rolled-back correction whose
  first day another recalculation then fixed still has its later days
  recalculated. For each player with a mismatch in
  those 7 days, in one database transaction, the worker recalculates and
  publishes that day and then every later day that already has a saved result,
  oldest first, because each day's result uses the day before it. It never
  creates a day that was never saved, and no job is queued, so a correction is
  never left half done. If any day fails, that player's changes are rolled
  back, logged with status `player_failed`, and retried at the next check 10
  minutes later; checks for that Reset stop once every player has succeeded.
  Each check reads, from their own indexes, only the reports saved late in
  those 7 days (about 300 a day) and a few index entries for each saved result of
  those days and today (about 100,000 at 12,500 players), so its cost does not
  grow with the days kept. On fake data shaped like production, with 14 days
  of battles, it took 0.07 s and 0.8 s, against 5-8 s and 68-77 s before; a
  window still holding the 197,898 reports backfilled on 2026-09-28 and
  2026-09-29 took 1.8 s for the first part. A battle saved after the last
  check of a Reset is added after the next Reset. Known limitation: a late
  battle that is older than 7 Legend days by the time the check runs, for
  example after a worker outage of over a week, is not corrected.
- A **frozen leaderboard snapshot** is the accepted, versioned ordering of actively tracked players at a reset baseline.
- A frozen snapshot ranks each player by their trophies at its Reset before the automatic defense loss. A Complete ended Legend day already proves them: its Reset readings at both ends and every battle between agree, so its EOD plus its automatic defense loss is the total, whatever the player's last reading shows, and the entry is proven. At a Reset that resets trophies, a Season's end or a weekly raise to 5,000, the end reading proves nothing, so the last reading must agree as well. Otherwise the total is the player's newest accepted profile reading at or before the Reset plus the trophy change of every battle their ended Legend day counts that is stamped after that reading. A battle stamped after a reading cannot be in it, so none is counted twice, and battles the [late-battle check](#6-ranked-day-and-leaderboard-snapshots) adds after 05:30 reach the board through its correction. This needs the day's battle logs to be continuous, its trophies to add up, and the reading taken at least 15 minutes after the day's Reset, after the previous day's last battle reports and automatic defense loss can land; otherwise the board keeps the reading and saves the entry as uncertain. It also keeps the reading as uncertain when one of those battles is stamped between the reading's request and its response, since the reading may or may not include it, or a defense is stamped in the 4 minutes before that request, since its attack can end up to 4 minutes after the defender's report, or when the two players' battle logs disagree on a battle's trophies. A reading does not prove it holds every battle stamped before it: an attacker's profile can show an attack minutes after its report time. So that total is saved as uncertain unless the day's start reading plus all its battles, or, with no start reading, its end Reset reading, with or without the day's known automatic defense loss, comes to it too. On 8 October 2026 the Day 3 board showed #2QCYU8C2G at 4,902 as proven, after a 04:37:05 reading of 4,703 that did not yet hold its attack stamped 04:34:08 for 29; its Complete day ended at 4,931, as did its next Reset reading. Under the 8 October 2026 tracking plan this Complete-day rule deploys on its own, and published boards are rebuilt with the Season repair (`republish-current-season --repair`) or `--boards`. Two limits remain until the plan's later per-player day ledger replaces it: a Complete day whose start and end Reset readings both missed the same delayed trophy credit is still taken as proven, because no later reading is checked, and with no start reading the end Reset reading alone can confirm a total. Boards published before this rule show readings alone until rebuilt. The October 2026 Season's Day 2 board missed late battles for 290 of 11,737 players, such as RAIN, shown at 5,088 instead of 5,168 after two attacks at 04:53 and 04:57; with them, its top 100 had the same trophies as Clash Spot's. Equal trophies follow the [Live Leaderboard's tie order](#live-leaderboard-ordering), with each player's Season attacks counted up to the Reset and saved with the board's other inputs. The snapshot also leaves the player out when a profile check after that reading and before the Reset returned 404 (player not found) and no later successful profile check came before the Reset, as the [Live Leaderboard](#live-leaderboard-ordering) does from the latest check. A 404 after the Reset never removes a player from an earlier day's board, and timeouts and server errors change nothing, so a player whose last good reading is old only because later checks timed out or failed stays on the board. On the October 2026 Season's Day 1 and Day 2 boards this left out 24 and 34 players, including the first and second on Day 2. Boards frozen before these rules are rebuilt with `republish-current-season --boards`; see [operating](operating.md).
- Continue serving the previously frozen snapshot while the next snapshot is assembled. Publish the replacement atomically so users never receive a mixture of snapshot versions.
- Target snapshot publication at approximately 05:05 UTC on normal days, after the daily no-attack matchmaking window.
- Target snapshot publication at approximately 05:10 UTC on Mondays, after the longer promotion and demotion transition.
- The one public **Live Leaderboard** follows the [Live Leaderboard ordering rules](#live-leaderboard-ordering). Frozen snapshots support reproducible history and analytics; they do not create a separate official-versus-tracked leaderboard.
- Retain each entry's observation time, measured coverage, freshness, confidence, and applicable official rank provenance.
- Publishing at the target time means accepting the best official observations available under those rules; it does not claim that every API response was generated simultaneously or that every entry has equal freshness.
- If later evidence proves a frozen snapshot inconsistent, retain the prior version and publish a corrected version rather than silently rewriting it.
- Each corrected version rebuilds the Reset's whole leaderboard and army records, so a Reset older than the newest one starts rebuilding at most once every 6 hours after its last rebuild, and never between 04:30 and 07:00 UTC. Corrections arriving in between wait and are applied together in the next rebuild; none is dropped. No leaderboard or army build for an older Reset, first or corrected, starts between 04:30 and 07:00; one not yet started waits until 07:00 without using up a retry; builds already running finish and publish. The newest Reset rebuilds as soon as a correction arrives. See [`past_reset_pacing.py`](../python/src/clashlens/past_reset_pacing.py).
- A frozen snapshot's observed time is its newest saved player update. The Daily view calls the snapshot incomplete when that time is more than 30 minutes before its Reset: it shows a notice with the gap and the newest update, and marks each row saved more than 30 minutes before Reset with its age at Reset. Collection pauses at 04:55, so normal snapshots end about 5 minutes before Reset; on 2026-10-03 no update was saved after 00:00. The notice names no cause, because the snapshot does not record one. Older saved times on a complete snapshot are not marked: an unchanged player is confirmed without saving a new update. See [`tracked-leaderboard.tsx`](../website/app/routes/tracked-leaderboard.tsx).

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

The following day-range filters describe current-season analytics. Army
percentages use completed Legend days and update after Reset, not from the
unfinished live day. Completed-season scope is defined separately below.

For `season=current`, the requested range ends no later than the latest Legend
day that has ended at Reset. Except for Consistent top, use the available
completed army days within that range. The returned selection starts and ends
at the first and last covered days; `collection_coverage.covered_days` lists
the exact days used, including gaps. The Armies page keeps the requested range
in its controls so later days can appear as they become ready, and names gaps
between the requested start and the last covered day. If no days can be used,
the view is unavailable.

Frozen Top-N cohorts and rank bands use membership from the frozen leaderboard
at the end of the final covered Legend day. For `season=current`, stop at the
latest completed army day in the requested range with a published frozen
leaderboard. Earlier completed army days can contribute without their own
leaderboard. Trophy-range filters need no leaderboard.

- For a trophy-range filter, the **defense lens** groups attacks by the defender's trophies at battle time.
- For a trophy-range filter, the **offense lens** groups attacks by the attacker's trophies at battle time.
- For a frozen leaderboard-cohort or rank-band filter, the defense lens includes attacks whose defenders belong to the selected snapshot population.
- For a frozen leaderboard-cohort or rank-band filter, the offense lens includes attacks whose attackers belong to the selected snapshot population.
- For a rank-streak filter, the defense lens includes attacks against players in the resulting streak set during the selected consecutive period.
- For a rank-streak filter, the offense lens includes attacks made by players in the resulting streak set during the selected consecutive period.
- Do not substitute current, snapshot, or season-end trophies for battle-time trophies without labeling the value as an estimate.
- Do not count the same battle twice when it appears in both the attacker's and defender's battle logs.
- Frozen Top-N cohorts are cumulative and use Top 5, 10, 20, 50, 100, 200, 500, 1,000, 2,000, 5,000, and 10,000; Top 100 is the default.
- Frozen rank bands are ranks 1–5, 6–10, 11–20, 21–50, 51–100, 101–200, each 100-rank band from 201–300 through 901–1,000, then 1,001–2,000, 2,001–5,000, and 5,001–10,000. A rank range is one band or several consecutive bands, such as 1–100.
- A **rank streak**, shown as Consistent top, is available only for a Top-N preset up to Top 1,000 and contains players in that Top-N cohort in every frozen daily snapshot of the selected inclusive range. Completed army data and a published frozen leaderboard are required on every day in that range; a missing day makes the view unavailable. Membership follows each day's saved pre-Reset leaderboard as published: a player at position N or better on every selected day qualifies even when an entry is stale or uncertain, and nobody below the cutoff moves up to fill a gap. This ranks each day's trophies at its Reset before the automatic defense loss, or the last reading before it on a board published before battles after readings were added and not rebuilt since, not settled EOD ranks; until settled EOD evidence exists, the page says the comparison with settled ranks is unavailable. Report the qualifying player count, players absent from the Top N on at least one day, qualifying players with stale or uncertain entries on at least one day, and shielded-day evidence. Each army result names the ended days in the requested range that lack completed army data or a published frozen leaderboard. The Armies page offers Consistent top only when that list is empty, explains how Top N is decided, and otherwise links to the latest run of consecutive days that have both.
- A trophy range is an arbitrary inclusive minimum and maximum, both whole numbers from 0 to 99,999, with maximum not lower than minimum. Legend I players can sit below 5,000 between weekly raises, so the range may start below it. Use the lens-specific battle-time trophy value; missing battle-time evidence is an exclusion, never a substituted observation.
- Exactly one trophy range, frozen Top-N cohort, frozen rank band, or Top-N rank streak applies at a time.

### Completed-season army statistics

- Keep all-tracked and final-season Top 100 views across the whole season.
  Top 100 uses the final Clash Lens ranking and those players' recorded battles
  from the entire season; it is not official rank or changing daily membership.
- Attacks are BY the selected players; defenses are AGAINST them. The Clan
  Castle toggle switches regular troop usage to individual Clan Castle troop
  usage. Combinations do not replace individual usage.
- Preserve unit IDs, quantities, using-battle counts, outcomes, denominators
  and unknown-unit evidence for both views. Measure their storage cost and
  bring an unaffordable result back before dropping an agreed view.
- Historical day ranges, arbitrary trophy filters and battle drilldown remain
  outside this scope. See [history-retention.md](history-retention.md) for the
  implemented format, missing additions and correction/cleanup requirements.

### Aggregate evidence

- Every aggregate must state its population filter, time period, observed sample size, measured coverage, freshness, decoder and catalog version, unknown-ID count, malformed or partial count, and analytics-rule version.
- Calculate public URL-filtered army results on demand from retained versioned facts. Derive their stable identity from the selection, result, and source-evidence hashes; opening a new URL must not create persistent per-selection rows.
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
- A Season reset, or a weekly raise of a total at or below 5,000, makes the next starting trophies 5,000 whatever the day ended on, so that 5,000 cannot prove the final total. Such a day can still be complete, but its confidence is inferred, not exact, and its automatic defense adjustment stays calculated.
- A Season-ending Reset reading proves no Season's end: it shows a survivor reset to 5,000, and a player dropped from Legend I (ranked below 10,000) gets no Legend I reading at all, so their last day had no end: all 1,993 such players on 5 October 2026, though 1,030 of 1,269 calculated ends equalled the game's own total. Once the player's league history gives the official Legend I total for that Season, which includes the automatic defense adjustment, that total, as saved from the newest league history, is the end every player's last day's calculation is checked against and the day's saved next start, survivor or dropped, even when no Season-ending Reset reading was saved for them, while the day's battle logs still decide whether its battles are all known: no reset to 5,000 applies, the day is exact when its calculation equals it, a day whose calculation differs is Inconsistent and keeps its calculated EOD, and nothing read after the Reset, a profile or the day's last battles, can settle that mismatch. On 5 October 2026, 778 of 9,593 Complete last days that had an official total disagreed with it. Saving that history whenever it is read, even weeks after the Season's end, recalculates a kept last day, at backfill priority, once per saved total and saved result of the day, while it is saved without the saved total as the end it is checked against or only under older calculation rules, even when an older response arriving later names another total, whatever a Season repair already did for the player; a Season whose details are retired is left as saved. The official total corrects only a kept last day of the current or previous Season, the two Seasons a day's calculation accepts; an older Season's kept last day is not recalculated and stays as saved. Each profile reading counts at the time it was read, even when an identical profile was saved earlier.
- A player ranked below 10,000 at a weekly Monday Reset drops to Legend II there too: by league history, 478 to 1,053 of the previous Season's survivors alone on each such Monday from 17 August to 21 September 2026. Legend I gives no official total for a week. Profiles still show Legend I for about 13 minutes after the Reset, so once a profile read after the player's Monday Reset reading, and before the next Reset, shows a league below Legend I, that Reset reading is the last day's end: no weekly raise to 5,000 applies, and the Monday's own day is not a Legend I day for them (`player_not_eligible`), so the player page leaves it out unless it has battles. The first such profile, for a player with a Reset reading there, recalculates those days once, when no other work waits and no earlier than 2 hours after the Reset, once calculations already running have saved. A last day with no used defense slots stays partial (`automatic_defense_basis_unavailable`), with no EOD: lower-league profiles cannot show the automatic defense loss the game may charge it, and a Reset reading below the day's end can be that loss or a credit not yet added. A Reset reading that already shows the lower league leaves the last day without an end. Not handled yet: a first lower-league profile read more than a day after the Monday Reset, such as after a collection outage, is not seen as that Monday's drop, so the last day keeps the weekly raise and the Monday stays a Legend I day. The live board, its player count and the Monday's daily board leave the player out once that profile is saved, and the promotion list keeps them for the Monday re-check.
- Timestamps allow exact event attribution, but timestamps alone do not prove that a ranked day is complete.
- If Clash Lens cannot prove completeness, mark the ranked day as partial or uncertain and preserve the reason.
- A ranked day without continuous battle-log coverage shows no EOD or net change, even when its trophies add up, unless it holds 8 attacks and 8 defenses with neither side disputed: a missed battle is missing from the sums too. Its EOD change, the next day's EOD change, and its Season's net change stay unavailable, and so do Day 28's season-ending trophies. Saved results follow the same rule when read; a saved day without its coverage or battle counts is withheld only when its saved reasons name a battle-log gap. See [`day_totals_supported`](../python/src/clashlens/reconciliation.py).
- Player-profile trophies and battle-log events may become visible at different times. Preserve both observations and require eventual reconciliation rather than assuming paired responses are atomic.
- A Reset's **boundary settlement** records whether its trophies are known to include the previous day's automatic defense adjustment, which the game can apply minutes after 05:00 UTC. It is `provisional` until proven, `settled` only with accepted trophies and the proof that established them, or `unresolved` when a completed check could not prove them. A complete Reset pair means its responses were processed, not that trophies settled. Day results do not read this state yet.
  - Once a Reset's 05:20 settlement check has finished and its responses are processed, the worker judges it. Only an ordinary Reset, not a Monday or Season one, with one to seven used defense slots on the ended day can settle, and only when all of these hold: the check's profile was requested from 05:20 and its battle log only after that profile arrived, both before 04:55; the profile, the log and the 05:00 profile were all processed as this player's; the log reaches back before both the ended day and the day before it, every row is readable, and no battle of those days was saved or corrected after it; neither player reported a battle between the 05:00 profile and the check's profile, or a new-day battle before the check's profile, and every profile read before the first new-day battle was processed and agrees with it; and the previous Reset is itself `settled`, independently of these readings. The target is that previous total plus the ended day's attack gains, minus its defense losses and its automatic loss, calculated as in [Automatic defense adjustment](#automatic-defense-adjustment). The check's profile must equal the target and the 05:00 profile must sit exactly the automatic loss above it. A check still waiting for collection or processing stays `provisional`; one that fails a guard is `unresolved` with its reasons.
  - New `settled` verdicts are admitted only while `CLASHLENS_ENABLE_NEW_RESET_PROOFS` is on, which is off by default. Off, a check passing every guard stays `provisional` with reason `new_reset_proofs_disabled`, its candidate proof kept for assessment. The switch never keeps a verdict whose proof later evidence takes away, and never falls back to the 05:00 total. Each verdict copies what it was proved from (the readings, the previous Reset's total and fingerprint, the battle counts and the automatic-loss arithmetic) into its proof. A change re-judges the next Reset, which the verdict roots, and a finalized Season's verdicts no longer change. No Reset can settle until a previous one has, so with no independently settled starting Reset none settles.
