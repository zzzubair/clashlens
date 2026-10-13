"""The nightly re-check of finished Legend days (the reconcile pass).

Right after the twice-daily recheck (``queue_refresh.DailyRecheck``) queues
its morning or 23:00 slot, it queues the slot's pass: one ``reconcile_pass``
job at backfill priority, so background pacing runs it only in the room live
work leaves. Each job is a step: one Legend day and the next 500 players the
Reset sweeps starting and ending it tracked. It queues the next step as it
finishes, oldest day first, until every finished day of the Season of the
newest ended day is judged. It compares things produced separately, in plain
SQL over saved battles, days and boards, never the worker's own calculation:

1. ``recount``: the day's attacks, defenses, gain and loss counted again
   from the saved battles against the saved day;
2. ``chain``: the day's start against the day before's saved next start;
3. ``ledger``: start plus gain less losses against the saved end, and at
   most 8 attacks and 8 defenses;
4. ``copies``: a battle whose two players' saved reports differ in stars,
   destruction, army, either player's trophies, gain or loss, or whose
   second report is missing though both players were tracked;
5. ``missing_day``: a player the Reset sweep ending the day tracked, with no
   saved day;
6. ``board``: a confirmed Daily board row against its day's end plus its
   automatic loss, less the battles that ended after the Reset. Rows not
   confirmed are left to later board corrections.

Each problem is a row in ``reconcile_findings`` (migration 0110); one not
found again is resolved and kept. Checks 1 and 5 are fixed only by queueing
the normal recalculation of the day, once per finding, at backfill
priority, at most ``PASS_FIX_LIMIT`` a pass; a finding still there after its
recalculation finished, judged from a read made after that, is marked
escalated and never queued again. A recalculation that failed is left open,
not escalated. The other checks only list. Nothing else is written, and no
recalculation is queued while a Season repair recalculates the Season's days.

The checks read in READ ONLY transactions with a statement timeout and take
no row locks, in the connection pool the worker already has. No pass starts
from 04:00 to 07:00 UTC, and a pass reaching 04:00 stops there. On 9 October
2026 the same checks found 7 recount differences, 0 chain breaks, 0 ledger
breaks, 5 one-sided battles, 1,019 missing days and 850 board rows over the
Season's first 4 days. The alert check posts one line a day with the counts
(``alerts.reconcile_probe``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import db, domain, first_battle_log, job_outcomes

WORK_TYPE = "reconcile_pass"
BATCH_DAYS = 500
PASS_FIX_LIMIT = 2000
STATEMENT_TIMEOUT = "60s"
QUIET_HOURS = range(4, 7)  # UTC: the Reset and its catching up.
FIXED_CHECKS = ("recount", "missing_day")


def _now() -> datetime:
    return datetime.now(UTC)


# The players the Reset sweeps starting and ending the day tracked.
_TRACKED = """
SELECT DISTINCT member FROM collector_reset_sweeps AS sweep
CROSS JOIN LATERAL unnest(sweep.member_ids) AS member
WHERE sweep.boundary_at IN (%(day)s, %(reset)s)
"""
# A step's players: the next tracked ones by id.
_STEP_PLAYERS = f"""
SELECT member FROM ({_TRACKED}) AS tracked (member)
WHERE member > %(after)s ORDER BY member LIMIT %(limit)s
"""
# The batch's players the Reset sweep ending the day tracked: each one's
# day is saved at that Reset.
_EXPECTED = """
SELECT DISTINCT member FROM collector_reset_sweeps AS sweep
CROSS JOIN LATERAL unnest(sweep.member_ids) AS member
WHERE sweep.boundary_at = %(reset)s AND member = ANY(%(players)s::bigint[])
"""
# Each player's saved day of %(day)s and the day before, as their pages
# show it: the newest published log and the saved result it shows.
_DAYS = """
SELECT DISTINCT ON (log.player_id, log.ranked_day_start)
       log.player_id, log.ranked_day_start, day.id, day.official_season_id,
       day.start_trophies, day.final_trophies_before_reset, day.next_start_trophies,
       day.automatic_defense_loss, day.attack_count, day.attack_gain,
       day.defense_count, day.observed_defense_loss
FROM api_player_daily_logs AS log
LEFT JOIN ranked_day_versions AS day ON day.id = log.ranked_day_version_id
WHERE log.player_id = ANY(%(players)s::bigint[])
  AND log.ranked_day_start IN (%(day)s, %(day)s - interval '1 day')
ORDER BY log.player_id, log.ranked_day_start, log.version DESC
"""
# Each player's battles of the day counted from the saved battles: the
# report each side's battle shows, when it is a valid Legend report stamped
# in the day (``domain.battle_window``), as ``ranked_day_inputs`` reads them.
# The battles index holds both players, so the day's battles are read once
# from it and only the batch's are joined.
_RECOUNT = """
SELECT side.player_id,
       count(*) FILTER (WHERE side.perspective = 'attacker'),
       COALESCE(sum(evidence.attacker_gain) FILTER (WHERE side.perspective = 'attacker'), 0),
       count(*) FILTER (WHERE side.perspective = 'defender'),
       COALESCE(sum(evidence.defender_loss) FILTER (WHERE side.perspective = 'defender'), 0)
FROM legend_battles AS battle
CROSS JOIN LATERAL (VALUES ('attacker', battle.attacker_player_id),
                           ('defender', battle.defender_player_id))
    AS side (perspective, player_id)
JOIN battle_perspectives AS shown
  ON shown.battle_id = battle.id AND shown.perspective = side.perspective
JOIN battle_evidence AS evidence ON evidence.id = shown.evidence_id
JOIN battle_source_rows AS source ON source.id = evidence.source_row_id
WHERE battle.ranked_day_start BETWEEN %(day)s - interval '1 day' AND %(day)s + interval '1 day'
  AND (battle.attacker_player_id = ANY(%(players)s::bigint[])
       OR battle.defender_player_id = ANY(%(players)s::bigint[]))
  AND side.player_id = ANY(%(players)s::bigint[])
  AND evidence.battle_timestamp >= %(window_start)s
  AND evidence.battle_timestamp < %(window_end)s
  AND source.outcome = 'valid_legend'
GROUP BY side.player_id
"""
# The day's battles whose two saved reports differ, or whose second report
# is missing though both players were tracked, compared from the reports
# themselves: each side's trophies as the attacker's and the defender's,
# and either side's trophies only when both reports carry them. A battle is
# judged with its attacker, or with its defender when the attacker was
# untracked.
_COPIES = f"""
WITH tracked AS ({_TRACKED}), reports AS (
    SELECT battle.id, battle.attacker_player_id AS attacker,
           battle.defender_player_id AS defender, evidence.perspective, evidence.stars,
           evidence.destruction_percentage, evidence.army_share_code,
           CASE WHEN evidence.perspective = 'attacker' THEN evidence.reporter_trophies
                ELSE evidence.opponent_trophies END AS attacker_trophies,
           CASE WHEN evidence.perspective = 'attacker' THEN evidence.opponent_trophies
                ELSE evidence.reporter_trophies END AS defender_trophies,
           evidence.attacker_gain, evidence.defender_loss
    FROM legend_battles AS battle
    LEFT JOIN battle_perspectives AS shown ON shown.battle_id = battle.id
    LEFT JOIN battle_evidence AS evidence ON evidence.id = shown.evidence_id
    WHERE battle.ranked_day_start = %(day)s
      AND (battle.attacker_player_id = ANY(%(players)s::bigint[])
           OR battle.defender_player_id = ANY(%(players)s::bigint[]))
), paired AS (
    SELECT id, attacker, defender, count(perspective) AS saved,
           array_remove(ARRAY[
               CASE WHEN count(perspective) = 2 AND count(DISTINCT stars) > 1 THEN 'stars' END,
               CASE WHEN count(perspective) = 2
                         AND count(DISTINCT destruction_percentage) > 1
                    THEN 'destruction_percentage' END,
               CASE WHEN count(perspective) = 2
                         AND (count(DISTINCT army_share_code) > 1
                              OR count(army_share_code) = 1)
                    THEN 'army_share_code' END,
               CASE WHEN count(attacker_trophies) = 2
                         AND count(DISTINCT attacker_trophies) > 1
                    THEN 'attacker_trophies' END,
               CASE WHEN count(defender_trophies) = 2
                         AND count(DISTINCT defender_trophies) > 1
                    THEN 'defender_trophies' END,
               CASE WHEN count(perspective) = 2 AND count(DISTINCT attacker_gain) > 1
                    THEN 'attacker_gain' END,
               CASE WHEN count(perspective) = 2 AND count(DISTINCT defender_loss) > 1
                    THEN 'defender_loss' END
           ], NULL) AS fields
    FROM reports GROUP BY id, attacker, defender
)
SELECT CASE WHEN attacker = ANY(%(players)s::bigint[]) THEN attacker ELSE defender END,
       id, saved, fields
FROM paired
WHERE (attacker = ANY(%(players)s::bigint[]) OR attacker NOT IN (SELECT member FROM tracked))
  AND (cardinality(fields) > 0
       OR saved < 2 AND attacker IN (SELECT member FROM tracked)
                    AND defender IN (SELECT member FROM tracked))
"""
# The confirmed rows of the newest Daily board of the Reset ending the day.
_BOARD = """
SELECT entry.player_id, board.id, entry.trophies
FROM (SELECT id FROM leaderboard_snapshots
      WHERE snapshot_kind = 'frozen' AND state = 'published' AND boundary_at = %(reset)s
      ORDER BY version DESC, id DESC LIMIT 1) AS board
JOIN leaderboard_snapshot_entries AS entry ON entry.snapshot_id = board.id
WHERE entry.player_id = ANY(%(players)s::bigint[]) AND entry.confidence = 'confirmed'
"""
# The trophies of a day's battles that ended at or after its Reset, which a
# board's total leaves out (``boundary_manifest.reset_trophies``): an attack
# ends at its report time, a defense when its attacker's report is stamped.
_AFTER_RESET = """
SELECT day.id, COALESCE(sum(
           CASE WHEN contribution.value ->> 'lens' = 'offense' THEN 1 ELSE -1 END
           * (contribution.value ->> 'amount_used')::integer), 0)
FROM ranked_day_versions AS day
CROSS JOIN LATERAL jsonb_array_elements(day.input_evidence -> 'contributions')
    AS contribution
LEFT JOIN LATERAL (
    SELECT evidence.battle_timestamp FROM battle_perspectives AS side
    JOIN battle_evidence AS evidence ON evidence.id = side.evidence_id
    WHERE side.battle_id = (contribution.value ->> 'battle_identity')::bigint
      AND side.perspective = 'attacker'
) AS attack (reported_at) ON contribution.value ->> 'lens' = 'defense'
WHERE day.id = ANY(%(versions)s)
  AND contribution.value ->> 'included' = 'true'
  AND CASE WHEN contribution.value ->> 'lens' = 'offense'
           THEN (contribution.value ->> 'battle_timestamp')::timestamptz
           ELSE attack.reported_at END >= day.ranked_day_end
GROUP BY day.id
"""


@dataclass(frozen=True)
class Finding:
    check: str
    player_id: int
    subject_id: int = 0
    version_id: int | None = None
    expected: dict[str, Any] = field(default_factory=dict)
    saved: dict[str, Any] = field(default_factory=dict)


def _params(day: datetime, players: list[int]) -> dict[str, Any]:
    params = {"players": players, "day": day, "reset": day + domain.RANKED_DAY_DURATION}
    params["window_start"], params["window_end"] = domain.battle_window(day)
    return params


def judge(connection: Any, day: datetime, players: list[int]) -> list[Finding]:
    """Every problem the six checks find in ``players``' day from ``day``,
    reading only, in the caller's transaction."""
    params = _params(day, players)
    days: dict[tuple[int, datetime], Any] = {
        (int(row[0]), row[1]): row for row in connection.execute(_DAYS, params)}
    findings = [Finding("missing_day", int(row[0]))
                for row in connection.execute(_EXPECTED, params)
                if (int(row[0]), day) not in days]
    recounts = {int(row[0]): tuple(int(value) for value in row[1:])
                for row in connection.execute(_RECOUNT, params)}
    ends = {}
    for (player, start), row in days.items():
        if start != day or row[2] is None:
            continue
        (_, _, version, season_id, start_trophies, end, _, automatic,
         attacks, gain, defenses, loss) = row
        ends[player] = (version, end, automatic)
        recount = recounts.get(player, (0, 0, 0, 0))
        if recount != (attacks, gain, defenses, loss):
            findings.append(Finding("recount", player, 0, version, _counts(*recount),
                                    _counts(attacks, gain, defenses, loss)))
        before = days.get((player, day - domain.RANKED_DAY_DURATION))
        if (before is not None and before[2] is not None and before[3] == season_id
                and None not in (before[6], start_trophies) and before[6] != start_trophies):
            findings.append(Finding("chain", player, 0, version, {"start": before[6]},
                                    {"start": start_trophies}))
        if None not in (start_trophies, end) and (
                start_trophies + gain - loss - (automatic or 0) != end):
            findings.append(Finding("ledger", player, 0, version,
                                    {"end": start_trophies + gain - loss - (automatic or 0)},
                                    {"end": end}))
        elif attacks > 8 or defenses > 8:
            findings.append(Finding("ledger", player, 0, version,
                                    {"attacks": min(attacks, 8), "defenses": min(defenses, 8)},
                                    {"attacks": attacks, "defenses": defenses}))
    for player, battle, saved, fields in connection.execute(_COPIES, params):
        findings.append(Finding("copies", int(player), int(battle), None,
                                {"reports": 2, "fields": []},
                                {"reports": int(saved), "fields": list(fields)}))
    rows = connection.execute(_BOARD, params).fetchall()
    versions = [ends[int(row[0])][0] for row in rows
                if ends.get(int(row[0]), (None, None))[1] is not None]
    after_reset = dict(connection.execute(
        _AFTER_RESET, {"versions": versions}).fetchall()) if versions else {}
    for player, board, trophies in rows:
        version, end, automatic = ends.get(int(player), (None, None, None))
        expected = None if end is None else (
            end + (automatic or 0) - after_reset.get(version, 0))
        if expected != trophies:
            findings.append(Finding("board", int(player), int(board), version,
                                    {"trophies": expected}, {"trophies": trophies}))
    return findings


def _counts(attacks: int, gain: int, defenses: int, loss: int) -> dict[str, int]:
    return {"attacks": attacks, "gain": gain, "defenses": defenses, "loss": loss}


def record(connection: Any, day: datetime, players: list[int], findings: list[Finding],
           *, read_at: datetime, fix: bool, fix_room: int) -> Counter[str]:
    """Save ``findings`` of ``players``' day from ``day``, judged from a read
    at ``read_at``, resolve those players' other open findings of the day,
    mark escalated each one whose recalculation finished before that read,
    and queue up to ``fix_room`` recalculations when ``fix``; the counts."""
    counts = Counter[str]()
    seen = []
    for finding in findings:
        finding_id, found_at, fix_queued_at, escalated_at = connection.execute(
            """
            INSERT INTO reconcile_findings AS finding (check_name, player_id,
                ranked_day_start, subject_id, ranked_day_version_id, expected, saved)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ranked_day_start, player_id, check_name, subject_id)
                WHERE resolved_at IS NULL
            DO UPDATE SET last_seen_at = clock_timestamp(),
                ranked_day_version_id = EXCLUDED.ranked_day_version_id,
                expected = EXCLUDED.expected, saved = EXCLUDED.saved
            RETURNING id, found_at, fix_queued_at, escalated_at
            """,
            (finding.check, finding.player_id, day, finding.subject_id, finding.version_id,
             Jsonb(finding.expected), Jsonb(finding.saved)),
        ).fetchone()
        seen.append(finding_id)
        counts["found" if found_at > read_at else "open"] += 1
        key = f"reconcile:pass:{finding_id}"
        if fix_queued_at is None:
            if fix and finding.check in FIXED_CHECKS and counts["queued"] < fix_room:
                # Due now at backfill priority; once, under the finding's own key.
                first_battle_log._queue(
                    connection, finding.player_id, day, None, key=key,
                    trigger="reconcile_pass", later_days=False,
                    priority=db.PYTHON_BACKFILL_PRIORITY)
                connection.execute(
                    "UPDATE reconcile_findings SET fix_queued_at = clock_timestamp()"
                    " WHERE id = %s", (finding_id,))
                counts["queued"] += 1
        elif escalated_at is None and connection.execute(
            """
            SELECT EXISTS (SELECT 1 FROM python_processing_jobs_worker
                WHERE deduplication_key = %s AND state = 'complete' AND completed_at < %s)
            """,
            (key, read_at),
        ).fetchone()[0]:
            connection.execute(
                "UPDATE reconcile_findings SET escalated_at = clock_timestamp() WHERE id = %s",
                (finding_id,))
            counts["escalated"] += 1
    counts["resolved"] = connection.execute(
        """
        UPDATE reconcile_findings SET resolved_at = clock_timestamp()
        WHERE resolved_at IS NULL AND ranked_day_start = %s
          AND player_id = ANY(%s::bigint[]) AND id <> ALL(%s)
        """,
        (day, players, seen),
    ).rowcount
    return counts


def _newest_day(slot: str) -> domain.RankedDay:
    """The newest day ended by ``slot``'s Reset: its Season is the pass's."""
    reset = datetime.strptime(slot[:10], "%Y-%m-%d").replace(hour=5, tzinfo=UTC)
    return domain.ranked_day_for(reset - timedelta(days=1))


def _queue_step(connection: Any, slot: str, day: datetime, after: int, queued: int) -> bool:
    """Queue a pass step, once; whether it was new."""
    day_text = day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json, state, due_at,
            parser_version, processing_version, domain_rule_version,
            analytics_rule_version, priority
        ) VALUES (NULL, %s, %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s, %s)
        ON CONFLICT (deduplication_key) DO NOTHING
        RETURNING id
        """,
        (WORK_TYPE, f"{WORK_TYPE}:{slot}:{day_text}:{after}",
         Jsonb({"slot": slot, "ranked_day_start": day_text, "after_player_id": after,
                "queued": queued}),
         db.DEFAULT_PARSER_VERSION, db.PROCESSING_VERSION, db.DOMAIN_RULE_VERSION,
         db.ANALYTICS_RULE_VERSION, db.PYTHON_BACKFILL_PRIORITY),
    ).fetchone() is not None


def queue_pass(database: db.Database, slot: str) -> bool:
    """Queue ``slot``'s pass, a twice-daily recheck slot such as
    ``2026-10-13:night``, from its Season's first day, once; whether it was
    new. None starts from 04:00 to 07:00 UTC."""
    if _now().hour in QUIET_HOURS:
        return False
    with database.pool.connection() as connection, connection.transaction():
        return _queue_step(connection, slot, _newest_day(slot).season_start, 0, 0)


def run_step(database: db.Database, claim: db.Claim) -> str:
    """Run the claimed pass step and queue the next one; its outcome."""
    step = claim.input_json
    slot, after, queued = str(step["slot"]), int(step["after_player_id"]), int(step["queued"])
    day = datetime.strptime(str(step["ranked_day_start"]), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC)
    newest = _newest_day(slot)
    detail: dict[str, Any] = {}
    try:
        with database.pool.connection() as connection:
            stopped = _now().hour in QUIET_HOURS
            if not stopped:
                with connection.transaction():
                    connection.execute("SET TRANSACTION READ ONLY")
                    connection.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                    read_at = connection.execute("SELECT transaction_timestamp()").fetchone()[0]
                    players = [int(row[0]) for row in connection.execute(
                        _STEP_PLAYERS, {**_params(day, []), "after": after, "limit": BATCH_DAYS})]
                    findings = judge(connection, day, players) if players else []
            with connection.transaction():
                job = database._lock_live_claim(connection, claim)
                if not stopped:
                    # Locked, a Season repair cannot start its days phase
                    # before this step's recalculations are queued.
                    repairing = any(row[0] > 0 and row[1] is None for row in connection.execute(
                        "SELECT queued_through_player_id, boards_queued_at FROM season_repairs"
                        " WHERE official_season_id = %s FOR SHARE", (newest.official_season_id,)))
                    counts = record(connection, day, players, findings, read_at=read_at,
                                    fix=not repairing, fix_room=PASS_FIX_LIMIT - queued)
                    detail = {**counts, "days": len(players), "repairing": repairing,
                              **Counter(f"seen:{finding.check}" for finding in findings)}
                    queued += counts["queued"]
                    if len(players) == BATCH_DAYS:
                        _queue_step(connection, slot, day, players[-1], queued)
                    elif day < newest.start:
                        _queue_step(connection, slot, day + domain.RANKED_DAY_DURATION, 0, queued)
                    else:
                        detail["finished"] = True
                database._finish_claim(connection, claim, job, state="complete",
                                       outcome="processed")
    except db.LeaseLost:
        return "lease_lost"
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        _log(slot, day, "failed", queued, error=repr(error)[:300])
        try:
            job_outcomes.fail_claim(database, claim, category="calculation_error",
                                    detail=type(error).__name__, retryable=True)
        except db.LeaseLost:
            return "lease_lost"
        return "failed"
    if stopped or detail.get("finished"):
        _log(slot, day, "stopped" if stopped else "finished", queued, **detail)
    return "processed"


def _log(slot: str, day: datetime, status: str, queued: int, **detail: Any) -> None:
    print(json.dumps({"event": "reconcile_pass", "slot": slot, "status": status,
                      "day": f"{day:%Y-%m-%d}", "queued": queued, **detail},
                     default=str), flush=True)
