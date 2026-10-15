# Testing

How Clash Lens checks that a change to calculations, jobs, boards or the
Reset works before it ships, and how to write the end-to-end test the rule in
`AGENTS.md` asks for.

## The rule

Any change to calculations, jobs, boards or the Reset ships with an
end-to-end test: the real worker against a real PostgreSQL, from saved game
data to a published board. It must fail on the old code. Unit tests alone are
not enough.

Why: on 12 October 2026 the calculation gave two players' Sunday days
numbers that broke one of the database's add-up checks. Unit tests check the
calculation's numbers, not whether the database will save them, so every
save failed in production and the Monday Daily board waited 7 hours. On
9 October a third shielded day in a row was refused the same way. Production
found both, not tests. A test that saves the day and publishes the board
would have failed first.

"Fails on the old code" means: run the new test on the code before your
change and watch it fail for the reason your change fixes. A test that passes
on both protects nothing. See [Checking a test fails on the old
code](#checking-a-test-fails-on-the-old-code).

## Running the tests

From `python/`, with a PostgreSQL 18 server you can create databases on (see
`python/README.md`):

```sh
CLASHLENS_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/clashlens \
UV_PROJECT_ENVIRONMENT=/tmp/clashlens-python-venv \
UV_LINK_MODE=copy uv run --locked --python 3.12 pytest -q tests/test_reset_scenarios_postgres.py
```

The five Reset stories take about 2.5 minutes on the owner's server: about
55 seconds for the four small ones and 90 for the Reset-size one. Add
`-k 12_october` (or another part of a test name) to run one.

Each test builds its own database from `deploy/migrations` through
`domain_database`, so the stories see the real schema and its checks.

## Writing a Reset story

The stories live in `python/tests/test_reset_scenarios_postgres.py`. Each is
one Reset told the way production lives it:

1. **Players and their battles.** `Player(tag)` holds every battle the player
   fights. `attacks(start, 40, 40)` gives attacks an hour apart from an hour
   after `start`; `defenses(start, 30, 30)` defenses an hour apart from 30
   minutes after it. Both take the attacker's army share code as `army=`
   (else the fixture's Other army); `multiplayer(start, n)` non-Legend battles
   that take rows of the 50-row battle log. `Battle(at, trophies, attack)`
   places one battle exactly.
2. **What the collector saves.** `story.reset(boundary, readings)` is a Reset
   sweep: it saves each member's profile, showing the given trophies and
   Season (None for Season 0, and None trophies for a profile that never
   answered), and battle log 4 minutes after the Reset, records the sweep's
   members, then runs the worker. `story.reads(at, totals)` is the
   collector's regular check (profile, then battle log a minute later), used
   for the last checks before a Reset (04:50) and the settlement check after
   it (05:20). `story.read(tag, at, trophies)` saves one profile, or one battle
   log without `trophies`. `story.history(at, season_end, finals)` saves the
   game's official Season-end totals. Each body is built from the player's
   battles as the game would show them at that moment.
3. **The worker.** `story.run_worker()` runs the production publication check
   and every job due, in the worker's own claim order, until nothing is left.
   A job sent to wait (a retry or a dependency) is made due at once, as time
   passing would. Pass `run=False` to `story.reset` to let later readings
   arrive before the worker gets to the Reset, as when it is behind.
   `story.count_armies(at)` runs the rank band totals, army type labelling and
   daily attack and defense counts the worker's maintenance timer runs,
   without waiting a minute between days' defenses.
4. **What a Clasher would see.** `story.saved_day(tag, day_start)` is the
   player's newest saved day (state, start, end, next start, automatic loss,
   Monday raise, attack and defense counts, unexplained difference).
   `story.board_members(boundary)` is each member's status on the Reset's
   newest board. `story.served_board(boundary, offset)` is the page of 100
   the API serves for that Daily board from `offset`, and `_ranked(board)`
   checks a page's order and returns each player's total.
   `story.served_armies(tag, at)` is the player's Armies on defense card the
   API serves at `at` for the Legend day before it.
   `story.served_army_types(at)` is the Armies view the API serves at `at`
   for the current Season's top 10, sorted by Avg. trophies.
   `story.unfinished_jobs()` lists jobs that failed for good or still wait; a
   finished story has none.

Underneath, the stories use the shared test fixtures every PostgreSQL test
uses: `database_url` and `archive_server` (`tests/conftest.py`, a test
database server and a fake archive), `domain_database` and
`store_observation` (`tests/domain_test_support.py`, a fresh database and one
saved game response with its job), and `_processor`
(`tests/test_reconciliation_postgres.py`, the real worker's job runner).

Clock rules, all set by `Story`:

- **Use past dates.** A Legend day counts as ended by the real clock, so a
  story's days must already have ended. The stories use the Season that
  started on 5 October 2026 and the one before it.
- **Boards wait for every member.** A story runs weeks after its Resets, so
  the 05:30 board deadline would pass before the first day saved. `Story`
  holds the deadline back; a test of the deadline itself sets it (see
  `test_reset_reliability_postgres.py`).
- **`story.wait(time)` lets time pass for board rebuilds.** A day saved after
  a board's first build joins it at the next rebuild: at most once an hour for
  the newest Reset, after 6 hours for older ones.
- **No quiet window.** Builds for older Resets normally wait out 04:30 to
  07:00 UTC; stories run as if outside it, so CI gives one result at any hour.

Tell the story the way production does: Reset readings 4 minutes after the
Reset, regular checks during the day and at 04:50, a settlement check at
05:20. A board ranks a day whose end is not proven from a reading at least
15 minutes into the day; without one it falls back to the day's start
reading, marked uncertain, so a story missing those checks tests something
production never sees.

Keep the file under 1,500 lines: at 1,082 lines, the next big story belongs in a
second file that imports `Story` from this one.

## The Reset-size story

`test_reset_at_full_size` is the 12 October 2026 Monday Reset among a
Season's saved battles. That Reset's never-played check read every saved
battle on each call, 500 ms a call, and day results fell 88,000 jobs behind.

- **What it holds:** 10,000 other players and 140,000 battles among them,
  saved directly with their attackers' reports and armies, and 200 players
  swept through the real worker at the Sunday and Monday Resets, half of
  them never played and sitting at 5,000.
- **What it checks:** during the Monday Reset the whole battles table is read
  fewer than 20 times in all, where one read a player would make 200 (a
  board build reads it once or twice); the worker takes under 1 second a
  player (about 0.25 seconds on the owner's server); every player is on the
  board, the 100 never-played ones on its second page at 5,000, after every
  played one.
- **Why not 13,391 players:** the worker takes about 0.2 seconds a player a
  Reset when it runs one job at a time, so a full Reset would take about 45
  minutes. The other players are saved directly instead.
- **Fresh statistics:** it refreshes the database's table statistics before
  the Monday Reset, as production's background maintenance keeps them, so
  the database's plans do not depend on when it last refreshed them itself.
- **Battles carry armies:** seeded first without army codes, the database
  planned the Monday board's army build to read the whole battles table once
  for each Sunday battle: 3,197 reads in 2 of 5 runs of the file, depending
  on when its statistics were last refreshed. Production's saved battles
  carry the army codes the game reports, so the seed does too.
- **What it does not bound:** reads of saved battle records (the
  `battle_evidence` table). The directly saved records are not linked to
  saved battle logs as production's are, so the battle-log coverage query
  can read every one of them for each player here. Production's
  plan estimate (about 105,000) and time for that query (about 103 ms a call,
  in the 12 October reliability reports) rule that out there.

## Season replay

A Season replay answers, for one version of the code: if production had run
this code for the whole current Season, what would every Daily board and
every saved day look like? Run it on `main` and on a fix, and compare the two
`metrics-after.tsv` files before the fix merges. It is not a test and does not
replace the end-to-end test above.

It needs a restored copy of the production database in a PostgreSQL 18
container on the replay machine (`wsl` by default), and about 70 GB free
there per replay. From any checkout:

```sh
development/season-replay start <git-ref> <name>
development/season-replay status <name>
```

`start` copies that ref's `python` and `deploy` folders to
`~/replay/<name>/` on the machine and runs the steps below there in the
background. `status` prints each finished step with its time in seconds, and
the paths of the metrics once written. Settings, as environment variables:
`SEASON_REPLAY_HOST` (default `wsl`), `SEASON_REPLAY_CONTAINER` (`clsnap-pg`),
`SEASON_REPLAY_PORT` (`55432`), `SEASON_REPLAY_SOURCE`, the copy to start from
(`clashlens`), `SEASON_REPLAY_LANES`, worker lanes and background job limit
(`16`), and `SEASON_REPLAY_DRAIN_HOURS`, how long to wait for the queue to
empty before measuring anyway (`8`).

| Step | What it does | Time on `wsl`, 14 October 2026 |
|---|---|---|
| `environment` | Installs the ref's locked Python packages | 22 s |
| `copy` | Copies the source database to `replay_<name>` | 81 min, while other copies were made on the same disk |
| `measure-before` | Scores each board against the game's lists and writes `metrics-before.tsv`: production as the copy left it | 41 s |
| `migrate` | Applies the ref's migrations the copy lacks, as `ops` does | 67 s, with nothing to apply |
| `park` | Sets aside work no worker here can do as production will: saved responses still waiting to be processed, whose raw bytes stayed on production's disk, and jobs not due until after the copy, such as the running day's end | 4 s |
| `worker` | Sets one planner statistic as production has it (below), then starts the real worker with no network | 2 s |
| `repair` | Runs the Season repair (`republish-current-season --repair queue`, up to 1,000 jobs a run, once a minute) until it says `done` | 59 min for the days of 17,366 of 17,369 players; the boards were not timed (below) |
| `drain` | Waits until no job, board rebuild or unpublished board is left | Not timed |
| `measure-after` | Scores the boards again and writes `metrics-after.tsv` | Not timed |

The first replay, of `main` on 14 October 2026, stopped twice in the
repair's board rebuilds when `wsl` restarted, so the last three times are
not known yet. `development/season-replay resume <name>` carries on a
stopped replay from its worker step without copying again.

**The metrics** (`development/season-replay.sql`) are three tables. Per
Reset, the board the website shows: entries on it; entries whose saved day
is Inconsistent, Partial (no start reading first, then automatic loss
unknown, then any other reason), or Complete but marked uncertain; entries
not proven (uncertain, or the day is not Complete) as a count and a share;
members left off, by reason; and, where the game's lists were saved, the
collector's own score of the board against them (`game_rankings.score_day`).
The first columns are those of the per-board table in the boards state report
of 14 October 2026, and on a copy of production the query gives exactly that
table's numbers for the boards not rebuilt since. Then every reason behind
those counts, and per Legend day each player's newest saved day by state.

**How it keeps production's behavior.** It runs the real worker and the
operator's own Season repair. The only changes are the ones the Reset stories
make: board rebuilds start as soon as the one before publishes, with no
6-hour or hourly wait and no 04:30–07:00 quiet window
(`development/season_replay.py cli`). Until the repair has recalculated
every day, no board rebuild starts at all: with no wait between rebuilds,
one always held the Reset locks a player's Season recalculation needs, and on
14 October 2026 the last 3 players waited 2 hours 25 minutes; with the hold
they finished within 5 minutes. The worker and the repair run in a
network namespace with no network: a call to the Clash API or the archive
fails at once, and the replay checks that before it starts the worker. They
reach the database through a local socket.

**One planner statistic.** A copy restored and analyzed marks the empty
table `battle_log_observation_rows` as known to be empty. The database then
plans the battle-log coverage query of every day calculation to read every
saved battle record once per battle log: on 14 October 2026 one such query
took 471 seconds on the copy and the replay finished 1 job in 5 minutes.
Production had the same plan from 16:56 that day, after an automatic vacuum,
until it was reset at 17:40. The `worker` step resets it the same way
(`pg_restore_relation_stats`), after which the replay finished about 420
jobs a minute. Never analyze or vacuum that table in a replay copy.

**What it does not reproduce.**

- Readings, battles and pulls collected after the copy was taken. The
  collector does not run.
- Pacing and timing: when each board would have been built, how late, and
  what a Reset under load does. Boards here are built from every day's final
  saved result, so a board built before a late reading in production shows
  the later state here.
- Responses saved in the last seconds before the copy and not yet processed.
  `park` counts them in its output; their days miss that evidence.
- The running Legend day: its day-end jobs wait for the next Reset, which
  the copy never sees, so `park` sets them aside and its days stay Live.
- The worker's database permissions: it connects as the database owner.
- When clock-driven work runs. The worker runs it by the replay machine's
  clock, not the copy's: on 14 October 2026 the night recheck of every
  active player's last two ended days, 21,706 recalculations, ran from 00:12
  to 03:00 during the replay, and the drain waited for it.
- Army statistics. The copy of 14 October 2026 holds no decoded armies, so
  a rebuilt board's member whose attacks are not decoded would hold that
  board's army records, and every later rebuild of its Reset, for ever.
  The replay marks such a member's army unavailable instead; the metrics
  never read army records.

**How to trust a result.** `metrics-before.tsv` should match production at
the time of the copy. A replay of `main` should match `metrics-before.tsv`
except where production had work still waiting (queued board rebuilds, the
day jobs pending at the copy); explain each difference before comparing a
fix with it. `failures.tsv` lists every job that failed for good during the
replay, `repair.log` each repair run's report, and `drain.log` the work left
over time. A replay that ends `NOT DRAINED` in `drain.log` measured boards
still being rebuilt.

## Checking a test fails on the old code

Reverse the fix in a throwaway copy of the code, never in your checkout (a
migration fix lives under `deploy/`, which the repository rules protect), and
run the new test there:

```sh
# From the repository root, with the new test committed.
OLD=/var/tmp/old-code && rm -rf "$OLD" && mkdir -p "$OLD"
git archive HEAD python deploy ops | tar -x -C "$OLD"
git show <fix commit> -- python/src | (cd "$OLD" && git apply -R)
cd "$OLD/python"
CLASHLENS_TEST_DATABASE_URL=... UV_PROJECT_ENVIRONMENT=/var/tmp/old-code-venv \
uv run --locked --python 3.12 pytest -q tests/test_reset_scenarios_postgres.py -k <story>
```

For a migration fix, name the migration file instead of `python/src`. If a
later change touched the same lines, reverse that one first. This is how each
story was checked on 12 October 2026:

| Story | Fix reversed | What failed on the old code |
|---|---|---|
| `test_monday_reset_of_12_october_2026` | PR 469 (`b8948e97`) | Both players' Sunday days refused by the database: "violates check constraint ranked_day_versions_formula_v2_check", the 12 October error |
| `test_monday_reset_with_sundays_ending_around_5000` | PRs 460 then 447 (`1258ae09`, `e00964df`) | IN_FLIGHT ranked at 4,940, counting an attack that ended after 05:00, instead of 4,900. With PR 460 alone reversed the story passes: that case is PR 447's |
| `test_season_end_reset_and_its_first_day` | PR 441 (`18495dd4`), `reconciliation.py` and `reconciliation_db.py` | DISAGREES's Day 1 saved Inconsistent instead of Complete |
| `test_wednesday_reset_with_days_that_once_could_not_save` | PR 414's migration `0092_shield_runs_any_length.sql` (`a104bbc0`) | SHIELD_RUN's third shielded day refused by the database, as on 9 October |
| `test_weekly_raise_postgres.py` | The late weekly raise fix (13 October 2026), `python/src` | LATE_RAISE's Sunday saved Inconsistent with next start 4,940 instead of Complete at 5,000, as 4,136 Sundays were on 12 October |
| `test_reset_at_full_size` | PR 467 (`2ec9b97b`) | The Monday Reset read the whole battles table about 734 times (about 100 million rows) instead of under 20; the story took 119 seconds instead of about 90 |

## Reset cases and their tests

Every boundary case in section 8 of the three reliability reports of
12 October 2026, duplicates merged. Report rows: **O** is the Opus report's
8.1 row number, **F** the Fable report's, **S** a Sol report family by name.
"Story" names a player in `test_reset_scenarios_postgres.py`.

### Monday Reset

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| Day ends below 5,000 and is raised to 5,000 | O1, F1, S Monday | Monday story RAISED | `test_reconciliation.py` |
| Day ends at exactly 5,000 | F2, S Monday | Monday story EXACT | |
| Below 5,000 only after the automatic loss; the Reset reading fits and a later one contradicts (12 October) | O1, O7, F3, F4, S early Monday | 12 October story, both real players | `test_monday_reset_reading.py` (4 real players, calculation only); `test_reset_reading_before_loss_postgres.py::test_monday_day_contradicted_after_a_fitting_reset_reading_is_saved` |
| Reset reading taken before the raise landed (12 October 2026) | F5 | Monday story UNRAISED; `test_weekly_raise_postgres.py`: Sunday Complete and proven on the board, even when the last reading before the Reset lacked a late attack's gain, Monday starts at 5,000, a Sunday with no battle that the reading shows charged ends after the charge, a reading unraised again after the raise still contradicts, and the full Season repair mends a Sunday and Monday saved before the fix, their board totals confirmed | `test_reading_rule.py::test_the_weekly_raise_can_land_after_the_reset_reading`; `test_monday_reset_reading.py` |
| No defense on a Monday; the raise hides the charge, unless a reading before the raise landed shows it | O6, F10, S no defenses | Monday story ZERO | `test_reset_reading_before_loss_postgres.py::test_monday_charge_read_before_the_raise_starts_no_next_day` |
| Reset reading taken before the automatic loss, on a Monday | O5 | Monday story UNSETTLED | |
| Attack ending after 05:00, with the Monday raise | O8, F19, S battle at 05:00 | Monday story IN_FLIGHT | `test_board_battle_end_postgres.py` |
| Dropped from Legend I at the Monday Reset: no raise | O14, F6, S weekly drop | Monday story DROPPED_WEEKLY | `test_reset_settlement_state_postgres.py::test_player_dropped_at_a_weekly_reset_ends_at_the_reset_reading` |

### Season end and Day 1

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| Day 28's end hidden by the Season reset until the official total arrives | O2, F14, S Season end | Season story, all players | `test_reset_settlement_state_postgres.py::test_last_season_day_ends_at_the_official_total` |
| Official total disagrees with the calculated end | F30, O7 | Season story DISAGREES | same |
| Official total includes the automatic loss | S Season end | Season story END_LOSS | |
| No battles on the last day | O2 | Season story SHIELDED | |
| Dropped to Legend II at the Season's end | F15 | Season story DROPPED | `test_reset_settlement_state_postgres.py` |
| Day 1 starts at 5,000 by the Season rule | O3, F12, S Day 1 | Season story, every Day 1 | `test_season_reset_readings.py` |
| Day 1's Season reset lands late; readings show the old Season's official total | F13, S old Season | Season story LATE_RESET, DISAGREES | `test_season_reset_readings.py` |
| Day 1's automatic loss uses Day 1's own average | O3, S Day 1 | Season story DAY_1_LOSS | `test_first_battle_log_postgres.py::test_day_1_saved_with_the_previous_season_average_is_recalculated_once` |
| Day 1 with no defense is never charged | F11 | Season story NO_DEFENSE | |

### Readings

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| A reading during the day contradicts, and a later one matches | O7, F25, S contradiction | Wednesday story CONTRADICTED | `test_reading_rule.py`, `test_reconciliation.py` |
| A reading taken before the game credited the latest attack | S delayed credit | Wednesday story LAGGED: read 15 minutes after its last attack, past the 10 minutes a battle has to land, still without its gain | `test_reading_rule.py::test_only_the_latest_attacks_can_lag` |
| Reset reading taken before the automatic loss | O5, F7, S before the loss | Wednesday story UNSETTLED_LOSS | `test_reset_reading_before_loss_postgres.py` |
| More than six battles in flight at a reading | S six ambiguous | Wednesday story CROWDED: 7 in flight and a total no mix of them fits. A reading with more than 6 battles possibly in flight (`MAX_AMBIGUOUS_BATTLES` in `reading_rule.py`) is not judged, so the day saves Complete | |
| Reset reading rejected (Season 0), so the day starts from the day before's end | F27, S Season 0 | Wednesday story CHAIN_BROKEN, where the day before has no end either | `test_first_battle_log_postgres.py::test_day_whose_reset_reading_is_rejected_starts_from_the_previous_days_end` |
| Chain start when the day before is Partial | F28 | Wednesday story CHAIN_BROKEN | |
| A reading still missing the day before's automatic loss | F26 | Not in a story: needs the game to apply the loss more than 15 minutes late, seen 0 times on 9 October | `test_reading_rule.py::test_a_season_reset_removes_the_day_befores_pending_loss` |

### Battles, counts and battle logs

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| Automatic loss for 1 to 7 defenses | O4, S 1–7 defenses | 12 October story, Wednesday story UNSETTLED_LOSS | `test_reconciliation.py` |
| Automatic loss when a log gap hides the day before's defenses | F8 | Wednesday story GAP_BEFORE | `test_reconciliation.py::test_incomplete_coverage_keeps_one_to_seven_defense_adjustment_unknown` |
| No defense: charged for all 8 or nothing, as a reading shows | O6, F9, S no defenses | Wednesday story ZERO_CHARGED; Monday story ZERO | `test_reset_reading_before_loss_postgres.py::test_zero_defense_day_read_before_its_loss_is_charged_by_the_recheck` |
| Battle-log gap | O12, F23, S battle logs | Wednesday story GAP_BEFORE, CHAIN_BROKEN | `test_reconciliation.py` |
| Log gap, but all 8 attacks and 8 defenses recorded | F24, S eight defenses | Wednesday story FULL_WITH_GAP | |
| All 16 battles recorded but no start reading | O13 | Wednesday story NO_START | |
| A ninth attack | F21, S nine attacks | Wednesday story NINTH: saved Partial with 9 attacks | |
| "No opponent, no battle" rows | S no opponent | Wednesday story NO_OPPONENT: a used slot for the automatic loss, not a battle | `test_reconciliation.py::test_no_opponent_rows_are_used_slots_for_the_automatic_loss_only` |
| Duplicate or disagreeing reports of one battle | F22, S invalid battles | Not in a story: stories track one side of each battle | `test_reconciliation.py::test_repeated_and_two_sided_contributions_count_once_per_own_perspective`; `test_queue_refresh_postgres.py::test_late_opponent_report_settles_the_battles_day_for_both_players` |
| Battle reported after the Reset reading but belonging to the ended day | F20, S battle at 05:00 | Monday story IN_FLIGHT | `test_queue_refresh_postgres.py::test_a_late_report_queues_the_previous_seasons_days_for_a_week` |
| Shielded run longer than 2 days | O11, F17, S shield runs | Wednesday story SHIELD_RUN (run of 3) | `test_queue_refresh_postgres.py::test_a_third_shielded_day_in_a_row_is_saved` |
| Never played, at 5,000 | O9, F16, S never played | 12 October story, both real players; Reset-size story, 100 players | `test_unplayed_5000_postgres.py` |
| Late starter, first Legend battle mid-Season | O10, F18, S late starter | 12 October story: both real players first played on Day 7 | `test_first_battle_log_postgres.py::test_player_first_seen_during_day_1_gets_a_season_rule_start` |

### Eligibility and Season identity

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| Player ineligible or demoted during a day | F31 | Covered by the drops above: the game moves players between leagues only at the Monday Reset and the Season's end | `test_reconciliation.py::test_dropped_zero_defense_day_stays_partial` |
| Season identity missing or conflicting; retired Season detail | F32, S Season anchor | Not end to end: the game never names two Seasons for one day, and retiring a Season is an operator step | `test_boundary_publication_manifests_postgres.py`, `test_claim_jobs_postgres.py` |
| Season day number outside 1 to 28 | O15 | Not needed: always 1 to 28 by construction | |

### Saving

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| Two saves of one player's day at once | O16, F33 | Not end to end: needs two worker processes racing on one day, which a story cannot time; one lock per player's day guards it | `test_reset_board_concurrency_postgres.py` |
| The daily log allows only Live, Complete and Partial | O17 | Every story saving an Inconsistent day (12 October, Season, Wednesday) | |
| Day saved under older rules, recalculated by Season repair | F34 | Not end to end here: an operator step | `test_domain_repair_postgres.py` |
| Earlier result kept after a recalculation fails for good | O23 | Not covered: no known case fails for good on today's code | |

### Jobs and boards

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| A day calculation fails for good and its board member waits forever | O19, F37, S failed day | 12 October and Wednesday stories: on the code before their fixes, their days fail for good and the stories fail | `test_reset_reliability_postgres.py::test_a_day_that_fails_for_good_leaves_the_board_to_publish_without_it` |
| A job runs out its time limit 3 times | O20, F35 | Not covered as a job failure: known gap in the reports. The 05:30 board deadline publishes without the member | `test_reset_reliability_postgres.py::test_board_publishes_at_0530_without_an_unfinished_player_and_adds_them_once_done` |
| An unexpected Python error in one job stops every lane of the worker | O21, S invalid job | Not covered: known gap with a fix proposed in the reports (Opus fix 14). A story for it would fail on today's code | |
| A job that always times out or finds the Reset busy retries forever | O22 | Backing off is covered; never alerting is not | `test_reset_reliability_postgres.py::test_a_day_meeting_a_busy_reset_hands_its_lease_back_and_backs_off` |
| A day saved after the board's first build | O18, S frozen generation | 12 October story: Sunday days join the board at the hourly rebuild | `test_boundary_publication_manifests_postgres.py` |
| Board member with no accepted profile ("missing") | F36 | Reset-size story: the 100 never-played players are missing but rank | |
| Army build gives up on pending members | F38, S failed build | Every story checks, after its last worker run, that no job is left failed or waiting, army builds included | `test_boundary_publication_manifests_postgres.py::test_army_build_that_gave_up_on_pending_members_runs_once_they_settle` |
| A Reset profile never answered | S missing baseline | Wednesday story NO_START | `test_reset_settlement_state_postgres.py` |
| A job of an unsupported version | S invalid job | Not end to end: needs two worker versions running at once | `test_claim_bounded_postgres.py` |

### Size

| Case | Reports | End-to-end test | Other tests |
|---|---|---|---|
| A Reset query whose cost grows with the battles saved (the never-played check of PR 451) | O9, S never played | Reset-size story | `test_unplayed_5000_postgres.py::test_the_never_played_check_reads_only_the_players_own_battles` |
