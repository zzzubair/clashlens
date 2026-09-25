# Product decisions and launch map

Agreed with Zubair on September 25, 2026. Source inspected at `01f746f` on
`main`. This is a documentation checkpoint, not a release or new runtime proof.
Zubair defines intended behavior. [#119](https://github.com/zzzubair/clashlens/issues/119)
is the one working launch checklist; this document and the contracts below hold
the detail. Supporting issues preserve requirements and evidence, rather than
acting as separate work queues. Their closure as consolidated is pending because
the approved checkpoint excluded issue closures. Nothing is marked implemented
merely by consolidation. Update this map when decisions or status change.

## Launch order

1. **Tracking from October 5 at 05:00 UTC, 06:00 UK time.** Start from about
   22,000 supplied tags plus the September 30 list. Add the October 6 list while
   collection continues. Normalize and deduplicate within/across lists and every
   discovery source. Count supplied tags, distinct real players and eligible
   Legend I players separately. This replaces the old 12,500-player target.
2. **Public website launch can follow mid-season.** It requires the real domain
   and logins, complete feature checks, community/private support, invited
   testing and final operating handover. Clan discovery must be ready for this
   release but must not delay starting collection from supplied lists.
3. **Expanded history before the first tracked season ends.** If October 5 is
   the first complete season, that is November 2 at 05:00 UTC. Protect required
   detail until the new views are ready and verified, even if cleanup is delayed.
4. **Later:** private group comparison and the Jev army-classification experiment.

[#119](https://github.com/zzzubair/clashlens/issues/119) is the dated overview.
The start date does not waive accuracy, delivered alerts, backup/restore proof,
capacity or cost checks. Smaller runs are tests, not a reduced launch population.
Plan approved warm-up before Reset; a cold import at 05:00 does not establish
complete Day 1 coverage. Real traffic, deployment, spending and kept-data
deletion still require their specific approvals.

## Whole-product map

| Area | Agreed behavior and implementation gap | Supporting detail |
| --- | --- | --- |
| Collection and discovery | Keep every confirmed real tag regardless of Town Hall or league. Automatically track eligible Legend I players, without a Start tracking button or public-lookup login. Existing opponent/ranking paths retain identities, but production discovery is disabled; repeat imports and clan fetching are missing. | [#125](https://github.com/zzzubair/clashlens/issues/125) |
| Clan discovery | Fetch a clan on first encounter and its member list daily. Reuse identities across lists, opponents, user input and clans. Size automatic growth before enabling it; a permanent newcomer waiting list is not the chosen product. | #125, [#128](https://github.com/zzzubair/clashlens/issues/128) |
| Search and player pages | Name search includes currently tracked players or players with recorded history. Other known tags remain directly accessible with an eligibility explanation and any history, without a full current profile. Current search/page behavior needs changing. | #125, [#127](https://github.com/zzzubair/clashlens/issues/127) |
| Accuracy, Reset and rankings | Preserve evidence, honest missing/partial results, one battle across duplicate reports, 05:00 UTC Legend days and 28-day seasons. Public rank is among Clash Lens tracked players. A full real Legend day at the revised population is still unproven. | [Domain contract](domain.md), #128 |
| Player history | Keep daily EOD, gain, loss and EOD difference from the previous day; Day 1 uses 5,000 and Day 28 supplies season-ending trophies. Show only Clash Lens final rank. Existing summaries have daily entries, but stored battle net and official final rank are not these new meanings. | [#139](https://github.com/zzzubair/clashlens/issues/139) |
| Army analytics | Percentages use completed Legend days after Reset. Attacks are BY selected players; defenses AGAINST them. Historical all-player and final-season Top 100 views must retain individual Clan Castle usage. Current history has all-player unit/quantity/star summaries only. | #139, [Retention contract](history-retention.md) |
| Accounts, saved players and groups | Usernames are fixed; display names remain editable. Verified ownership is public, saved lists/groups private, and membership proves no ownership. New tag inputs share discovery rules. No export product. Real-provider completion and whole-product privacy checks remain. | [#123](https://github.com/zzzubair/clashlens/issues/123), #127 |
| Website and performance | Preserve desktop controls/detail and usable phone, tablet, older-device and slow-network behavior. #137 improved measured Chromium cases and the current-season Clan Castle toggle. Physical-device/browser breadth and full production load remain unproven. | #127, [#130](https://github.com/zzzubair/clashlens/issues/130) |
| Recovery and retention | Prove seven-day recovery with required raw bytes still available during restore. Allow season corrections for seven days, then checked finalization and bounded cleanup. These are separate windows. Scheduling and physical-expiry protection are not finished. | [#122](https://github.com/zzzubair/clashlens/issues/122), [#129](https://github.com/zzzubair/clashlens/issues/129) |
| Discord and support | Public discussion/feedback, Discord-only private tickets through an existing bot, and a separate private operator-alert channel using an incoming webhook. No custom bot or email fallback. Setup and delivery are unproven. | [#138](https://github.com/zzzubair/clashlens/issues/138), [#124](https://github.com/zzzubair/clashlens/issues/124) |
| Release and operation | Tracking and public website have separate readiness records. Keep exact revisions, usable data checks, restore/reboot/upgrade evidence, costs and an operating guide. | [#131](https://github.com/zzzubair/clashlens/issues/131) |

## Capacity and implementation limits

- `bootstrap-population` in `python/src/clashlens/bootstrap.py` accepts at most
  20,000 tags, rejects duplicates within the input and refuses a later new
  import. Reuse/extend the existing path for overlapping lists and interrupted
  imports; do not create a parallel import framework.
- `./dev up` supports 200 or 12,500 fake players; `./dev trial` caps at 12,500.
  The existing tools and their reported projections need the revised population.
  Old 12,500-player evidence remains valid only for its measured workload.
- The collector supports multiple keys, but `./ops` wires four regular keys
  and one separate interactive key. Each regular key has a configured allowance
  of 30 starts/second. At 22,000 active players, two requests every five minutes
  require about 146.7 starts/second before discovery, retries and Reset work;
  four keys allow at most 120. This is arithmetic, not measured throughput or a
  claim about the provider's actual limits. Zubair can supply additional keys.
- Measure collection, processing, imports, clan growth, disk, database, archive,
  backups and restore load together. Keep swap out of the capacity budget.
  Report known-tag/clan growth and eligibility-check cost as well as active
  players. Expand capacity before promising automatic growth; extra keys alone
  do not establish processing or storage capacity.
- Aim around the existing EUR 50–60/month guide, not a hard cap. Flag an overrun
  with measured rates and six-month storage headroom. No upgrade, subscription
  or narrower retained history has been approved by this checkpoint.

## Evidence and status corrections

- #120 provider choice and #121 storage wiring are closed. #126 was delivered
  by #135 and remains closed. Its unit/quantity format and unknown-ID recovery
  evidence are in [issue-126-validation.md](issue-126-validation.md); the newly
  agreed history requirements are #139. Old requests for retained destruction
  and relationships are not proof those fields were delivered.
- #136 fixed usernames. #137 merged as `8107609` and its PR records preview
  deployment on September 25, with main production unchanged. The saved army
  capture is still September 22 data; it does not grow daily. Preview has five
  existing Python-file differences from main, so it is not an exact assembled
  main release. See [website/README.md](../website/README.md) and the PR evidence.
- #137 records remaining mobile army-breakdown overflow, heavy-view stalls and
  untested physical devices/Safari/Firefox. These belong in #127/#130 checks;
  no general scrolling improvement or production-load proof is claimed.
- The September 25 source review found #132 findings 4 and 15 no longer present:
  Refresh uses the configured-origin helper and `player.currentDay!` assertions
  are absent. The other findings need scoped follow-up, not automatic severity
  acceptance. `dev` is 1,553 lines, the domain-processing database test 1,668,
  and the Python-client website test 1,516; report rather than grow those files.
- Earlier read-only production observation at September 25 17:33 UTC found zero
  active players, a 15,292,095-byte database and 22 successful change-log uploads
  with zero recorded failures. The backup timer was active, with three listed
  backups. This proves neither a seven-day restore nor full-load readiness.
- Source/docs/GitHub were inspected for this checkpoint. No product tests,
  real API collection, imports, restore drills or deployment were run. Existing
  validation documents retain their original dates, revisions and limitations.

## Open decisions and inputs

- The current 22,000-tag list location/delivery. The old issue path
  `/home/zubair/clash_players` did not exist on ser5ver during the review.
  September 30 and October 6 lists are future inputs, not imported populations.
- Actual distinct/real/eligible counts, needed key count, measured growth capacity
  and cost. Establish the operational discovery budget and how daily clan checks
  reuse recent eligibility evidence without losing Monday/rediscovery checks.
- Added storage for both historical populations and Clan Castle units; measured
  raw-deletion restore allowance and the revised six-month projection.
- Discord server/invite/channel identifiers, ticket provider/plan, permissions,
  transcript retention and support staffing. Alert thresholds for queues,
  uploads and Reset delays still need explicit values in #124.
- [#61](https://github.com/zzzubair/clashlens/issues/61) stays post-launch. Its
  14-day cross-season comparison needs a retention review: compact history does
  not retain all requested daily destruction/star data, and seven-day detail
  retirement can remove part of that window. Do not promise those comparisons
  or extend storage silently as part of launch.
- Jev remains a later experiment for comparing named army types by usage and
  three-star rate. One main type per army, uncertain results unclassified,
  current season only. A labeled evaluation, reusable classifications and
  measured accuracy, latency and cost are prerequisites; no integration or
  paid trial is approved. Full compositions are not permanent current history.

## Acceptance to carry into implementation

Follow #119 for order. Supporting issue descriptions retain these acceptance
details and their historical evidence:

- Imports: more than 22,000 tags; duplicates within/across lists and other
  sources; concurrent imports; retry after interruption; new arrivals during
  collection. Each real tag keeps one identity and its history, and existing
  collection is not restarted. Report unique/new/existing/eligible/unresolved
  counts without publishing source lists.
- Eligibility: malformed and nonexistent tags differ from temporary failures.
  Recognized entry/exit changes collection and ranking; missing/unknown tier
  evidence preserves the last confirmed state. Re-entry reuses the same player.
  Earlier days for a late arrival remain missing rather than invented.
- Discovery and search: anonymous exact-tag visits begin automatically with
  useful progress/failure/retry. Name results contain tracked/historical players
  only. Non-Legend-I direct pages show explanation/history. Daily clan scans and
  clan moves add new players without duplicates or starving normal collection.
- Trophy history: Day 1 EOD 5,050 means +50; Day 2 EOD 4,950 means -100. Check
  missing previous EOD, late arrival, boundary adjustments and Day 28. Official
  rank must not replace unknown Clash Lens rank or determine final Top 100.
- Army history: a player entering final Top 100 contributes their recorded
  earlier-season battles. All-player and Top 100 samples differ correctly;
  regular/individual Clan Castle and BY/AGAINST counts retain their denominators.
  Renaming an unknown unit after raw/detail retirement preserves usage/outcomes.
- Cleanup/recovery: seven-day timing alone cannot bypass pending work or absent
  summaries. Interrupted/repeated cleanup preserves dependencies. Restore the
  oldest promised point and both sides of physical expiry with required raw
  references readable, including time to finish recovery.
- Website/accounts: both real providers, fixed usernames/editable display
  names, private saved lists/groups, public ownership disclosure, shared URLs,
  keyboard/no-JavaScript operation and honest empty/partial/failure states.
  Unsupported exports enqueue nothing. Check phones/tablets/older devices and
  slow connections while collection runs; preserve desktop controls and detail.
- Support: two test members cannot read one another's ticket or transcript;
  support can respond; ordinary members cannot see operator alerts. Test direct
  links and signed-out website entry. A ticket does not itself prove ownership.
- Operations: delivered failure/recovery alerts with no unchanged-message flood,
  alert-delivery failure handling, restart/reboot, upgrade and isolated recovery
  from a failed upgrade. Distinguish running processes from readable player data.

Record exact revisions and measured limits. Do not turn a checked documentation
entry into a claim that the corresponding runtime work passed.
