# Start tracking from supplied lists

Runbook for the Wednesday September 30, 2026 tracking launch. Follow
[the launch issue](https://github.com/zzzubair/clashlens/issues/140) and the
[manual-import verification](manual-list-import-validation.md). These are
commands for an approved future operation, not permission to change production.

Use one operator session on rogue. Keep lists and receipts outside Git. Never
use `bootstrap-population` for this pool, force eligibility, delete evidence,
or run `./ops up --fixture` on the production account. General health checks,
per-alert responses and restore instructions belong in the separate operating
notes and [deployment guide](deployment.md).

## 1. Choose the first complete Legend day

Record the operator, exact release commit, approvals, source fingerprints and
target Reset in the launch record. If starting on Wednesday evening, September
30 at 05:00 UTC has passed. Target **October 1 at 05:00 UTC, 06:00 UK time** for
the first complete Legend day. The season starts October 5 at 05:00 UTC.

Start approved warm-up the evening before that Reset. By **04:45 UTC**, finish
imports, initial profiles and battle reads, and the alert/recovery test. Check
again before **04:55 UTC**, when the planned Reset pause begins. This is a
preparation deadline, not a claim that 22,157 checks take 15 minutes. If missed,
record partial coverage and move the first-complete-day claim to a later Reset.
Do not invent earlier battles for late arrivals.

Firstmate must supply accepted capacity, backup/restore and small real-player
comparison evidence required by the launch issue. If these gates are incomplete,
prepare files only. Stop before step 4. A fake rehearsal waives none of them.

## 2. Check the production checkout and create a private record

```sh
ssh fedora
cd "$HOME/development/ClashLens"
pwd -P
git status --short
git rev-parse HEAD
./ops status
command -v python3
```

Expect the production path, a clean Git status, the installed commit and running
services. A running collector with zero active players is not proof of real
collection. Stop if the checkout is dirty. Record the installed commit; the
exact new release is selected only after approval in step 4.

```sh
umask 077
LAUNCH_DIR="$HOME/clashlens-launch-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$LAUNCH_DIR"
mkdir "$LAUNCH_DIR/sources"
git rev-parse HEAD > "$LAUNCH_DIR/previous-commit.txt"
PG_CONTAINER=clashlens-postgres
db() {
  podman exec -i "$PG_CONTAINER" sh -c \
    'exec psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' sh "$@"
}
db -c "SELECT count(*) AS known, count(*) FILTER (WHERE active) AS active FROM players;"
```

Expect zero active players from the handover. Review any different result before
continuing. The connection uses settings inside the database container and
prints no credential. Confirm the existing database supports this import:

```sh
db -c "SELECT max(version) AS schema_version FROM clash_lens_schema_migrations;"
db -c "SELECT to_regprocedure('clashlens_enqueue_discovery_profiles(bigint[])') AS import_function;"
db -c "SELECT last_success_at FROM collector_response_state LIMIT 0;"
```

Expect schema version at least 31, a named import function, and no SQL error.
A missing prerequisite needs an approved deployment plan before this import,
not an improvised schema edit. Abort here by leaving the private directory in
place; no application data has changed.

## 3. Normalize the four sources and today's additional list

```sh
for day in 2026-07-29 2026-08-05 2026-09-08 2026-09-22; do
  curl --fail --silent --show-error \
    "https://files.zubairshaik.net/public/clash_players/legend-player-tags-$day.txt" \
    --output "$LAUNCH_DIR/sources/legend-player-tags-$day.txt" || break
done
```

Copy today's supplied file to `$LAUNCH_DIR/sources/new-list.txt`. Its location
is an input from firstmate. Do not guess its URL or silently omit it. From the
machine holding it, use `scp /actual/path/to/new-list.txt
fedora:/actual/launch/directory/sources/new-list.txt`, substituting the recorded
directory. Run the following only when all five files exist.

This accepts the invisible UTF-8 encoding header, trims whitespace, uppercases
tags and requires a leading `#`. It rejects empty files, blank lines, invalid
characters and invalid encoding before writing the combined list. Sources
remain untouched. On rejection, correct the input and use a fresh directory.

```sh
POOL_TAGS="$LAUNCH_DIR/combined.txt"
python3 - "$LAUNCH_DIR" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
days = ["2026-07-29", "2026-08-05", "2026-09-08", "2026-09-22"]
paths = [root / "sources" / f"legend-player-tags-{d}.txt" for d in days]
paths.append(root / "sources" / "new-list.txt")
pool, original_pool, sources = set(), set(), []
for index, path in enumerate(paths):
    raw = path.read_bytes()
    lines = raw.decode("utf-8-sig").splitlines()
    if not lines:
        raise SystemExit(f"Empty file: {path.name}")
    tags = set()
    for number, line in enumerate(lines, 1):
        tag = line.strip().upper()
        if not re.fullmatch(r"#[0289PYLQGRJCUV]+", tag):
            raise SystemExit(f"Invalid tag: {path.name}, line {number}")
        tags.add(tag)
    sources.append({"file": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
                    "rows": len(lines), "unique": len(tags),
                    "new_to_supplied_pool": len(tags - pool)})
    pool.update(tags)
    if index == 3:
        original_pool = pool.copy()
data = ("\n".join(sorted(pool)) + "\n").encode()
receipt = {"sources": sources, "supplied_rows": sum(s["rows"] for s in sources),
           "original_unique": len(original_pool), "supplied_unique": len(pool),
           "additional_unique": len(pool - original_pool),
           "combined_sha256": hashlib.sha256(data).hexdigest()}
with (root / "combined.txt").open("xb") as target:
    target.write(data)
with (root / "sources.json").open("x") as target:
    json.dump(receipt, target, indent=2)
print(json.dumps(receipt, indent=2))
PY
```

Expect `original_unique: 22157`, original rows totalling **50,830**, and separate
`additional_unique`. Check the four fingerprints against
[the recorded sources](product-status.md#supplied-lists-checked-september-25).
`supplied_unique = 22157 + additional_unique`. No player is yet confirmed real.
The fake rehearsal uses smaller counts with this same block. A mismatch means
stop before importing; leave all source files and receipts for review.

## 4. Approval: stage the release and hold collection stopped

**Get Zubair's approval for the exact release, service interruption, supplied
counts, subsequent real requests and simultaneous alert deployment.** Record
it before these production-changing commands. Keep global rankings and automatic
discovery at their existing disabled settings. Candidates alone cause requests.
Do not display `app.env` or the webhook file.

```sh
read -r -p 'Approved full release commit: ' RELEASE_COMMIT
[[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit'; exit 1; }
git fetch --no-tags origin "$RELEASE_COMMIT"
git cat-file -e "$RELEASE_COMMIT^{commit}"
./ops down
git checkout --detach "$RELEASE_COMMIT"
test "$(git rev-parse HEAD)" = "$RELEASE_COMMIT"
test -f deploy/quadlet/clashlens-alert.timer
git rev-parse HEAD > "$LAUNCH_DIR/release-commit.txt"
./ops build
systemctl --user start clashlens-postgres.service
systemctl --user is-active clashlens-collector.service
systemctl --user is-active clashlens-postgres.service
systemctl --user is-enabled clashlens.target
test -O /srv/clashlens-secrets/clashlens-discord-alert-webhook
test "$(stat -c %a /srv/clashlens-secrets/clashlens-discord-alert-webhook)" = 600
```

Expect the collector `inactive`, PostgreSQL `active` and the target `disabled`.
The stopped-state inspections return nonzero as expected. `build` stages images
without deploying them and prints `Release production built from ...; run ./ops up
separately`. `down` stops the website and backup timer too; schedule
this as a short maintenance window. Starting PostgreSQL alone permits import
without collection. The secret checks succeed silently. Stop on a failed check.
If configuration overrides the standard webhook location, privately verify that
path without displaying its contents.

Do not stop only `clashlens-collector.service`: the parent target requires it,
so systemd can stop the whole application, including PostgreSQL. Use this
explicit full stop, followed by database-only startup. To abandon, use step 9.
After import commits, even an old collector can send real requests on restart.

## 5. Approval: commit the manual import

**Confirm approval for writing these candidates and queuing real checks.** The
stopped collector sends no requests during the initial import. A later import
while collection is running can send requests immediately at commit, so it also
requires real-traffic approval.

The transaction below saves all changes together or none of them. It keeps
existing identities and history and serializes
manual imports with a database lock and queues at most 500 IDs per call.
Unfinished checks are reused across all five-minute cycles. Applied profile
results and finished checks with profile responses from this week are reused,
including failed checks which need separate review. Use the profile response
time, including the latest unchanged response, to decide the week.
A Sunday profile does not satisfy Monday's check just because its league history
finishes on Monday. A failed check with no profile is reused only if queued this
week. Monday starts at 05:00 UTC, not midnight.
Use `$POOL_TAGS` as the import and report input. Step 3 sets it to the supplied
combined list; the October 5 procedure below selects a wider, saved pool.

```sh
set -o pipefail
{
  cat <<'SQL'
BEGIN;
SET LOCAL statement_timeout = '5min';
SET LOCAL lock_timeout = '15s';
SELECT pg_advisory_xact_lock(hashtextextended('manual-list-import', 0));
CREATE TEMP TABLE launch_tags (tag text PRIMARY KEY) ON COMMIT DROP;
\copy launch_tags(tag) FROM STDIN
SQL
  cat "$POOL_TAGS"
  printf '\\.\n'
  cat <<'SQL'
CREATE TEMP TABLE launch_new ON COMMIT DROP AS
WITH inserted AS (
  INSERT INTO players (normalized_tag, active, eligibility_state)
  SELECT tag, false, 'unknown' FROM launch_tags
  ON CONFLICT (normalized_tag) DO NOTHING RETURNING id
) SELECT id FROM inserted;
CREATE TEMP TABLE launch_due ON COMMIT DROP AS
WITH cutoff AS (
  SELECT (date_trunc('week', clock_timestamp() AT TIME ZONE 'UTC' - interval '5 hours')
          + interval '5 hours') AT TIME ZONE 'UTC' AS since
)
SELECT p.id, row_number() OVER (ORDER BY p.id) AS n
FROM launch_tags t JOIN players p ON p.normalized_tag = t.tag CROSS JOIN cutoff c
WHERE NOT (p.active AND p.eligibility_state = 'eligible')
AND NOT EXISTS (
  SELECT 1 FROM collector_work w
  LEFT JOIN collector_observations o ON o.id = w.profile_observation_id
  LEFT JOIN collector_response_state r ON r.player_id = w.player_id
    AND r.endpoint = 'profile' AND r.last_observation_id = w.profile_observation_id
  WHERE w.player_id = p.id
  AND w.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
  AND (w.status IN ('pending', 'waiting_retry')
       OR (w.status IN ('complete', 'failed')
           AND greatest(o.response_completed_at, r.last_seen_at) >= c.since)
       OR (w.status = 'failed' AND w.profile_observation_id IS NULL
           AND w.created_at >= c.since))
)
AND NOT EXISTS (
  SELECT 1 FROM collector_response_state r
  JOIN player_profile_effects e ON e.observation_id = r.last_observation_id
    AND e.effect_kind = 'current_profile'
  JOIN player_profile_versions v ON v.id = e.profile_version_id
  WHERE r.player_id = p.id AND r.endpoint = 'profile'
  AND v.id = p.current_profile_version_id AND e.source_http_status = 200
  AND r.last_success_at >= c.since AND v.source_contract_state = 'accepted'
  AND v.eligibility_state IN ('eligible', 'ineligible')
);
SELECT coalesce(sum(clashlens_enqueue_discovery_profiles(ids)), 0) AS checks_queued
FROM (SELECT array_agg(id ORDER BY id) AS ids FROM launch_due GROUP BY (n - 1) / 500) b;
SELECT (SELECT count(*) FROM launch_tags) AS pool_unique,
       (SELECT count(*) FROM launch_new) AS new_identities,
       (SELECT count(*) FROM launch_tags) - (SELECT count(*) FROM launch_new) AS existing_identities;
COMMIT;
SQL
} | db | tee "$LAUNCH_DIR/import-$(date -u +%Y%m%dT%H%M%S%N).txt"
```

Expect `COPY N`, `checks_queued`, new/existing counts adding to `N`, and final
`COMMIT`. `N` is the number of tags in `$POOL_TAGS`; the October 5 pool can be
larger than the supplied list. An empty database gets `N` identities and checks.
Repeating the same input adds zero identities. It adds zero checks while work is
unfinished or this week's result is reusable, including an applied non-Legend result that cancelled
discovery before league history finished. Old inactive results become due after
Monday Reset. Printed counts before `COMMIT` are not proof of a durable import.

Before commit, Ctrl-C and close the database session to roll back the whole
transaction. Practise abandonment by replacing only final `COMMIT;` with
`ROLLBACK;`; before/after player and work counts must match. If commit
acknowledgement was lost, repeat identical input and read the receipt. After
commit, abandon by keeping collection stopped and retaining candidates, work
and files. Do not delete rows or mark work cancelled to undo the import.

## 6. Approval: deploy alerts and start tracking together

**Get Zubair's final go-ahead for real traffic and deployment**, with the import
receipt and exact staged release ready. Do not start the alert timer hours
before collection. Run these adjacent commands:

```sh
./ops up
./ops status
systemctl --user is-active clashlens-collector.service clashlens-alert.timer
./ops alert-check
```

`up` suppresses checks during startup, installs alert units, starts tracking and its one-minute timer, then records
successful startup. Expect `Clash Lens production is ready ... enabled after
reboot`, healthy/running services and two `active` lines. A healthy alert check
succeeds silently. Startup gives the fetch-gap check ten minutes; verify that
real fetches actually begin. Preserve normal Reset handling. On startup failure,
use step 9; never bypass release-fingerprint or migration guards.

## 7. Report outcomes and verify warm-up

Run this read-only report for the saved `$POOL_TAGS` pool, including the new file.
Repeat while checks finish; save the final receipt. Pending includes processing and
uncertain evidence, so zero failed alone is not completion.

```sh
{
  cat <<'SQL'
BEGIN;
CREATE TEMP TABLE launch_tags (tag text PRIMARY KEY) ON COMMIT DROP;
\copy launch_tags(tag) FROM STDIN
SQL
  cat "$POOL_TAGS"
  printf '\\.\n'
  cat <<'SQL'
CREATE TEMP TABLE launch_evidence ON COMMIT DROP AS
WITH latest_work AS MATERIALIZED (
  SELECT DISTINCT ON (w.player_id) w.player_id, w.status, w.failure_category,
    w.battle_log_status, w.league_history_status
  FROM collector_work w JOIN players p ON p.id = w.player_id
  JOIN launch_tags t ON t.tag = p.normalized_tag
  WHERE w.kind IN ('discovery_profile', 'initial_collection', 'live_refresh')
  ORDER BY w.player_id, w.created_at DESC, w.id DESC
)
SELECT t.tag, p.active,
  w.status AS work_status, w.failure_category AS work_failure,
  ep.endpoint, ep.required, r.last_success_at, o.http_status,
  processing.outcome, processing.failure_category, job.status AS processing_status,
  coalesce(ep.endpoint = 'profile' AND v.source_contract_state = 'accepted'
    AND e.profile_version_id = p.current_profile_version_id
    AND v.eligibility_state = p.eligibility_state
    AND v.eligibility_state IN ('eligible', 'ineligible')
    AND p.active = (v.eligibility_state = 'eligible'), false) AS resolved_profile
FROM launch_tags t LEFT JOIN players p ON p.normalized_tag = t.tag
LEFT JOIN latest_work w ON w.player_id = p.id
CROSS JOIN LATERAL (VALUES
  ('profile', true),
  ('battle_log', coalesce(p.active OR w.battle_log_status = 'observed', false)),
  ('league_history', coalesce(p.active OR w.league_history_status = 'observed', false))
) ep(endpoint, required)
LEFT JOIN collector_response_state r ON r.player_id = p.id AND r.endpoint = ep.endpoint
LEFT JOIN collector_observations o ON o.id = r.last_observation_id
LEFT JOIN LATERAL (
  SELECT id, outcome, failure_category FROM observation_processing_outcomes
  WHERE observation_id = o.id ORDER BY created_at DESC, id DESC LIMIT 1
) processing ON true
LEFT JOIN LATERAL (
  SELECT status FROM python_processing_jobs
  WHERE observation_id = o.id AND work_type = 'process_observation'
  ORDER BY id DESC LIMIT 1
) job ON true
LEFT JOIN player_profile_effects e ON ep.endpoint = 'profile'
  AND e.observation_id = o.id AND e.processing_outcome_id = processing.id
  AND e.effect_kind = 'current_profile'
LEFT JOIN player_profile_versions v ON v.id = e.profile_version_id;
CREATE TEMP TABLE launch_states ON COMMIT DROP AS
SELECT tag, active,
  bool_or(coalesce(endpoint = 'profile' AND http_status = 200
    AND outcome = 'processed', false)) AS real,
  CASE
    WHEN bool_or(work_status = 'failed' OR (required AND (
      http_status NOT BETWEEN 200 AND 299
      OR outcome IN ('non_success', 'malformed', 'unsupported', 'integrity_failure')
      OR processing_status = 'failed'))) THEN 'failed'
    WHEN bool_or(work_status IN ('pending', 'waiting_retry') OR (required AND (
      http_status IS NULL OR outcome IS DISTINCT FROM 'processed'
      OR failure_category IS NOT NULL))) THEN 'pending'
    WHEN bool_or(resolved_profile)
      THEN CASE WHEN active THEN 'legend' ELSE 'not_legend' END
    ELSE 'pending'
  END AS result,
  bool_and(coalesce(last_success_at >= clock_timestamp() - interval '10 minutes', false))
    FILTER (WHERE endpoint IN ('profile', 'battle_log')) AS fresh_initial_reads
FROM launch_evidence GROUP BY tag, active;
SELECT count(*) AS pool_unique, count(*) FILTER (WHERE real) AS confirmed_real,
  count(*) FILTER (WHERE result = 'legend') AS legend,
  count(*) FILTER (WHERE result = 'not_legend') AS not_legend,
  count(*) FILTER (WHERE result = 'failed') AS failed,
  count(*) FILTER (WHERE result = 'pending') AS pending_or_uncertain
FROM launch_states;
SELECT endpoint, category, count(*) FROM (
  SELECT DISTINCT tag, 'work' AS endpoint, coalesce(work_failure, work_status) AS category
  FROM launch_evidence WHERE work_status IN ('failed', 'waiting_retry')
  UNION ALL
  SELECT tag, endpoint, CASE
    WHEN http_status NOT BETWEEN 200 AND 299 THEN 'http_' || http_status::text
    WHEN failure_category IS NOT NULL THEN failure_category
    WHEN outcome IS DISTINCT FROM 'processed' THEN coalesce(outcome, processing_status, 'awaiting_processing')
    WHEN endpoint = 'profile' AND NOT resolved_profile
      THEN 'uncertain_profile'
    ELSE processing_status END
  FROM launch_evidence WHERE required AND (
    http_status NOT BETWEEN 200 AND 299 OR failure_category IS NOT NULL
    OR outcome IS DISTINCT FROM 'processed' OR processing_status = 'failed'
    OR (endpoint = 'profile' AND NOT resolved_profile))
) failures GROUP BY 1, 2 ORDER BY 1, 2;
SELECT count(*) AS active_in_pool,
  count(*) FILTER (WHERE result <> 'legend' OR NOT fresh_initial_reads)
    AS unresolved_or_stale_initial_reads
FROM launch_states WHERE active;
COMMIT;
SQL
} | db | tee "$LAUNCH_DIR/counts-$(date -u +%Y%m%dT%H%M%S%N).txt"
```

`legend + not_legend + failed + pending_or_uncertain = pool_unique`.
Confirmed-real is separate evidence, not an extra population: a real player can
still have a failed league-history check. A successfully processed profile
proves a real player even when its league tier or season data is uncertain;
eligibility stays pending. A timeout does not prove nonexistence
or non-Legend status. A not-found response stays unconfirmed and counts as
failed, even when collection work says complete. Completion means the response
was saved, not that the player exists. The report checks the latest saved profile,
battle-log and league-history responses and their processing results. It links
eligibility to the applied profile response, including one that reused an older
stored profile. An unknown tier cannot borrow the previous accepted profile's
eligibility. Missing processing, partial results and conflicting evidence stay
pending. Report failure and uncertainty categories without publishing tags or
profile bodies; their counts describe endpoint problems and need not equal the
number of failed players.

An inactive player's trusted non-Legend profile can cancel discovery before
league history arrives. Unfetched league history is then unnecessary for that
player. History is required for active players and when the relevant work fetched
it. Older history from another check does not change a newly confirmed inactive
player's result. Recent successful profile and battle reads cannot hide a failed
history response for an active player.

Completion requires every tag accounted for, zero unresolved initial checks,
and every intended Legend player active with both recent, successfully processed
initial reads and successful, processed league history. Report actual
counts against the 12,500 active planning target; never force that count.
Firstmate must resolve the launch impact of outstanding failures before claiming
all players checked successfully. Recheck initial reads before 04:55 UTC and
after Reset. These reads alone do not prove a complete accurate real Legend day.

For another list during tracking, use a new private directory, copy the original
sources plus updated additional file there, and repeat steps 3, 5 and 7 with
approval. Use the same applied-response and profile-time selection in step 5;
step 3 resets `$POOL_TAGS` to the new combined list, so ordinary additional-list
imports stay limited to their supplied tags. Healthy tracking need not restart.

On October 5, after promotions and the Monday Reset, prepare the updated sources
with step 3. With approval for rechecking all stored inactive players, run this
read-only selection once before step 5. It saves the union of supplied tags and
all stored inactive players, including players absent from the newest list.

```sh
(
  set -o pipefail
  set -o noclobber
  {
    cat <<'SQL'
BEGIN;
CREATE TEMP TABLE launch_tags (tag text PRIMARY KEY) ON COMMIT DROP;
\copy launch_tags(tag) FROM STDIN
SQL
    cat "$LAUNCH_DIR/combined.txt"
    printf '\\.\n'
    cat <<'SQL'
INSERT INTO launch_tags (tag)
SELECT normalized_tag FROM players WHERE NOT active
ON CONFLICT (tag) DO NOTHING;
SELECT tag FROM launch_tags ORDER BY tag;
ROLLBACK;
SQL
  } | db -qAt > "$LAUNCH_DIR/monday-pool.txt"
) || { echo 'Monday pool creation failed; stop and review'; exit 1; }
test -s "$LAUNCH_DIR/monday-pool.txt" || exit 1
POOL_TAGS="$LAUNCH_DIR/monday-pool.txt"
wc -l "$POOL_TAGS"
```

Expect one tag per line and a count at least as large as `supplied_unique` in
`sources.json`. Keep the source receipt and combined list unchanged. Run steps
5 and 7 with this same saved pool. Do not recreate it between imports or reports;
players promoted by these checks must remain included after becoming active.
Unfinished checks are still reused, but a pre-Reset profile completed after Reset
does not satisfy that week. Repeat step 5 after those checks finish to queue any
still due, then step 7, retaining `$POOL_TAGS` as `monday-pool.txt`. In a new
operator session, restore that path before resuming. Automatic Monday scheduling
remains separate work due by October 12.

## 8. Approval: observe a problem alert and recovery

**Get Zubair's approval for a brief private API interruption**, away from
04:55–05:10 UTC and before the warm-up deadline. This tests the data-read alert
while collection, the database and backups continue. Pausing the container
keeps the parent target and alert timer running. Stopping the required service
can stop both. Keep the pause under 60 seconds and arm
recovery in the same shell:

```sh
trap 'podman unpause clashlens-python-api' EXIT HUP INT TERM
date -u +%FT%TZ
podman pause clashlens-python-api
timeout 60 ./ops alert-check
podman unpause clashlens-python-api
systemctl --user is-active clashlens-api.service
./ops alert-check
trap - EXIT HUP INT TERM
date -u +%FT%TZ
```

If the first check fails, restore the API immediately before investigating. The
service must be active before clearing the trap. If startup is not ready yet,
repeat the recovery check once it is. Delivery prints no message receipt: record
one problem message and its recovered message, timestamps and message links in
the private Discord channel. The old September 27 test message does not count.
Run `./ops alert-check` again and expect no unchanged-message duplicate. Observe
one natural timer run using
`journalctl --user -u clashlens-alert.service --since '5 minutes ago' --no-pager`.
A manual check alone does not prove scheduling. Missing delivery fails this gate.

## 9. Stop safely or abandon the launch

The approved launch should include these stop actions on failure. Otherwise
obtain approval before changing services. Use the existing full shutdown to
stop collection, withdraw alerts and keep the application stopped after reboot:

```sh
./ops down
./ops status
systemctl --user is-enabled clashlens.target
```

Expect `disabled and stopped; database, spool and fixture archive data were
kept` and a disabled target. This also stops the backup schedule and website;
record that consequence. Alerts are withdrawn by stopping their units and
parent target, without deleting unit files or secrets. Never prune backups,
remove volumes, reverse migrations or delete source evidence here.

After review and renewed approval for real traffic:

```sh
./ops up
```

Repeat steps 6–8. Existing identities and work remain. The safe rollback for a
failed release is a stopped system with its data kept. Selecting an older image
or restoring a database after migrations requires the separate recovery
procedure and approval; do not guess a compatible combination.

## Practice evidence and limits

September 30 practice uses a new clone on rogue, default 200 synthetic players
and synthetic lists. The command/timing record is
`/home/zubair/code/p/firstmate/data/cl-golive-plan/practice.md` on the coordinating
machine. Observed: 269 supplied rows became 203 unique tags. The default stack had already
imported 200; extending only its fake API population to 202 let two additional
Legend candidates and one deliberately missing player exercise the later list.
Rollback kept 200 identities; commit added three; both unfinished and completed
repeat imports added zero work. Final counts were 202 confirmed-real, 202 Legend,
zero non-Legend, one HTTP 404 failure and zero unresolved. All 202 had recent
profile and battle reads. A local receiver got one read-failure alert and one
recovery, without a repeat message. Safe stop retained all three data volumes.

Corrections: preview already owned the default development ports, so only the
practice ports were remapped. The first alert baseline ran before collector
metrics were ready and failed; the corrected run passed. The report needed to
inspect HTTP status because work marked complete can contain a not-found reply.

The fake stack does not install production alert units. The alert rehearsal
mapped container names to its own resources, supplied fake backup/restart checks
and redirected delivery to a local HTTP receiver. It did not execute production
`ops up`/`down`, natural timer firing or the real backup probe. The positive
non-Legend case, a check crossing Monday Reset, real-list downloads, Discord
delivery, real Reset coverage and reboot were not tested here. The command/timing
record above retains these limits and the exact corrections. No product code
changed. Production approval and launch evidence remain required.

The review corrections to weekly reuse and the shared outcome report above have
not been executed against PostgreSQL. The earlier rehearsal did not cover a
reused stored profile after changed experience level, a saved league-history
error, an unknown tier preserving older eligibility, a response crossing Monday
Reset, or a cancelled non-Legend check following an older history error. The
saved Monday pool, including an omitted inactive player who becomes active
before reporting, has not been exercised against PostgreSQL either. The
revised report's runtime for 22,157 tags is also unmeasured. Rehearse these cases
with fake data before using the revised queries in production; the earlier
counts do not validate these corrections.
