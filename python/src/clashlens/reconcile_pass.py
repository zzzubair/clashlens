"""The nightly re-check of finished Legend days (the reconcile pass).

After each twice-daily recheck (``queue_refresh.DailyRecheck``) is queued,
once after the morning one and once after the 23:00 one, the worker's
maintenance timer re-checks every finished day of the Season of the newest
ended day, 500 player-days a turn, each turn one Legend day and the next 500
tracked players. It compares things produced separately, in plain SQL over
saved battles, days and boards, never the worker's own calculation:

1. ``recount``: the day's attacks, defenses, gain and loss counted again
   from the saved battles against the saved day;
2. ``chain``: the day's start against the day before's saved next start;
3. ``ledger``: start plus gain less losses against the saved end, and at
   most 8 attacks and 8 defenses;
4. ``copies``: a battle the two players' battle logs disagree on, or that
   only one of two tracked players' logs shows;
5. ``missing_day``: a tracked player with saved days this Season but none
   for this day;
6. ``board``: a confirmed Daily board row against its day's end plus its
   automatic loss, less the battles that ended after the Reset.

Each problem is a row in ``reconcile_findings`` (migration 0110); one not
found again is resolved, and resolved rows go 48 hours later. Checks 1 and 5
are fixed only by queueing the normal recalculation of the day, once per
finding, at backfill priority, at most ``PASS_FIX_LIMIT`` a pass; a finding
still there in a later pass after its recalculation finished is marked
escalated and never queued again. The other checks only list. Nothing else
is written, and nothing while a Season repair recalculates the Season's days.

The checks read in READ ONLY transactions with a statement timeout and take
no row locks. They run on the maintenance timer's derived turn, in the
connection pool the worker already has, never from 04:00 to 07:00 UTC; a
pass still running at 04:00 stops there. On 9 October 2026 the same checks
found 7 recount differences, 0 chain breaks, 0 ledger breaks, 5 one-sided
battles, 1,019 missing days and 850 board rows over the Season's first 4
days. The alert check posts one line a day with the counts
(``alerts.reconcile_probe``).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import db, domain, first_battle_log

BATCH_DAYS = 500
PASS_FIX_LIMIT = 2000
STATEMENT_TIMEOUT = "60s"
QUIET_HOURS = range(4, 7)  # UTC: the Reset and its catching up.
FINDINGS_KEPT = timedelta(hours=48)
FIXED_CHECKS = ("recount", "missing_day")

# Each tracked player's saved day of %(day)s and the day before, as their
# pages show it: the newest published log and the saved result it shows.
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
# The players with any saved day of the Season.
_SEASON_PLAYERS = """
SELECT player.id FROM unnest(%(players)s::bigint[]) AS player (id)
WHERE EXISTS (SELECT 1 FROM api_player_daily_logs AS log
              WHERE log.player_id = player.id
                AND log.ranked_day_start >= %(season_start)s
                AND log.ranked_day_start < %(season_end)s)
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
  AND (battle.attacker_player_id = ANY(%(players)s::bigint[]) OR battle.defender_player_id = ANY(%(players)s::bigint[]))
  AND side.player_id = ANY(%(players)s::bigint[])
  AND evidence.battle_timestamp >= %(window_start)s
  AND evidence.battle_timestamp < %(window_end)s
  AND source.outcome = 'valid_legend'
GROUP BY side.player_id
"""
# The day's battles whose two copies disagree, or that only one of two
# players saved for the day shows. A battle is judged with its attacker
# when the attacker is tracked, else with its defender.
_COPIES = """
SELECT CASE WHEN battle.attacker_player_id = ANY(%(players)s::bigint[])
            THEN battle.attacker_player_id ELSE battle.defender_player_id END,
       battle.id, battle.disagreement_state, battle.disagreement_fields
FROM legend_battles AS battle
JOIN players AS attacker ON attacker.id = battle.attacker_player_id
WHERE battle.ranked_day_start = %(day)s
  AND (battle.attacker_player_id = ANY(%(players)s::bigint[])
       OR battle.defender_player_id = ANY(%(players)s::bigint[]) AND NOT attacker.active)
  AND (battle.disagreement_state = 'disagreement'
       OR battle.disagreement_state = 'single_perspective'
          AND attacker.active
          AND EXISTS (SELECT 1 FROM players AS defender
                      WHERE defender.id = battle.defender_player_id AND defender.active)
          AND (SELECT count(DISTINCT log.player_id) FROM api_player_daily_logs AS log
               WHERE log.player_id IN (battle.attacker_player_id, battle.defender_player_id)
                 AND log.ranked_day_start = %(day)s) = 2)
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


def judge(connection: Any, season: domain.RankedDay, day: datetime,
          players: list[int]) -> list[Finding]:
    """Every problem the six checks find in ``players``' day from ``day``,
    reading only, in the caller's transaction."""
    params = {"players": players, "day": day, "reset": day + domain.RANKED_DAY_DURATION,
              "season_start": season.season_start, "season_end": season.season_end}
    params["window_start"], params["window_end"] = domain.battle_window(day)
    days: dict[tuple[int, datetime], Any] = {
        (int(row[0]), row[1]): row for row in connection.execute(_DAYS, params)}
    findings = []
    for player in (int(row[0]) for row in connection.execute(_SEASON_PLAYERS, params)):
        if (player, day) not in days:
            findings.append(Finding("missing_day", player))
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
    for player, battle, state, fields in connection.execute(_COPIES, params):
        findings.append(Finding("copies", int(player), int(battle), None,
                                {"copies": "agreed"},
                                {"copies": str(state), "fields": list(fields)}))
    differing = []
    for player, board, trophies in connection.execute(_BOARD, params):
        version, end, automatic = ends.get(int(player), (None, None, None))
        if end is None:
            findings.append(Finding("board", int(player), int(board), version,
                                    {"trophies": None}, {"trophies": trophies}))
        elif end + (automatic or 0) != trophies:
            differing.append((int(player), int(board), version, end + (automatic or 0), trophies))
    after_reset = dict(connection.execute(
        _AFTER_RESET, {"versions": [row[2] for row in differing]}).fetchall()
    ) if differing else {}
    for player, board, version, settled, trophies in differing:
        if settled - after_reset.get(version, 0) != trophies:
            findings.append(Finding("board", player, board, version,
                                    {"trophies": settled - after_reset.get(version, 0)},
                                    {"trophies": trophies}))
    return findings


def _counts(attacks: int, gain: int, defenses: int, loss: int) -> dict[str, int]:
    return {"attacks": attacks, "gain": gain, "defenses": defenses, "loss": loss}


def record(connection: Any, day: datetime, after: int, last: int | None,
           findings: list[Finding], *, pass_started: datetime, fix: bool,
           fix_room: int) -> dict[str, int]:
    """Save ``findings`` for players after ``after`` through ``last`` (or
    every later one) on ``day``, resolve the scope's other open findings,
    mark escalated each one seen again after its recalculation finished, and
    queue up to ``fix_room`` recalculations when ``fix``; the counts."""
    counts = Counter[str]()
    seen = []
    for finding in findings:
        row = connection.execute(
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
        finding_id, found_at, fix_queued_at, escalated_at = row
        seen.append(finding_id)
        counts["found" if found_at >= pass_started else "open"] += 1
        if fix_queued_at is None:
            if fix and finding.check in FIXED_CHECKS and counts["queued"] < fix_room:
                # Due now at backfill priority; once, under the finding's own key.
                first_battle_log._queue(
                    connection, finding.player_id, day, None,
                    key=f"reconcile:pass:{finding_id}", trigger="reconcile_pass",
                    later_days=False, priority=db.PYTHON_BACKFILL_PRIORITY)
                connection.execute(
                    "UPDATE reconcile_findings SET fix_queued_at = clock_timestamp()"
                    " WHERE id = %s", (finding_id,))
                counts["queued"] += 1
        elif escalated_at is None and fix_queued_at < pass_started and not connection.execute(
            """
            SELECT EXISTS (SELECT 1 FROM python_processing_jobs_worker
                WHERE deduplication_key = %s
                  AND state IN ('pending', 'leased', 'waiting_retry', 'waiting_dependency'))
            """,
            (f"reconcile:pass:{finding_id}",),
        ).fetchone()[0]:
            connection.execute(
                "UPDATE reconcile_findings SET escalated_at = clock_timestamp() WHERE id = %s",
                (finding_id,))
            counts["escalated"] += 1
    counts["resolved"] = connection.execute(
        """
        UPDATE reconcile_findings SET resolved_at = clock_timestamp()
        WHERE resolved_at IS NULL AND ranked_day_start = %s
          AND player_id > %s AND (%s::bigint IS NULL OR player_id <= %s)
          AND id <> ALL(%s)
        """,
        (day, after, last, last, seen),
    ).rowcount
    return counts


@dataclass
class _Pass:
    slot: str
    season: domain.RankedDay
    last_day: datetime
    day: datetime
    after: int = 0
    # The database's time once the pass has started.
    started_at: datetime | None = None
    counts: Counter[str] = field(default_factory=Counter)


class ReconcilePass:
    """One pass per recheck slot, a batch each time the timer calls."""

    def __init__(self, database: db.Database) -> None:
        self.database, self.current, self.done = database, None, set()

    def run_when_due(self, recheck_slots: set[str], now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        if now.astimezone(UTC).hour in QUIET_HOURS:
            if self.current is not None:
                self._log("stopped", self.current)
                self.current = None
            return
        if self.current is None:
            latest = domain.ranked_day_for(now).start
            due = sorted(slot for slot in recheck_slots - self.done
                         if slot.startswith(f"{latest:%Y-%m-%d}:"))
            if not due:
                return
            self.done.update(due)
            newest = domain.ranked_day_for(latest - timedelta(days=1))
            self.current = _Pass(due[-1], newest, newest.start, newest.season_start)
        try:
            if self.current.started_at is None:
                self._start(self.current)
            if not self.run_batch(self.current):
                self._log("finished", self.current)
                self.current = None
        except Exception as error:  # noqa: BLE001 - the next slot tries again
            self._log("failed", self.current, error=repr(error)[:300])
            self.current = None

    def _start(self, current: _Pass) -> None:
        """Delete findings resolved ``FINDINGS_KEPT`` ago and note the
        database's time the pass starts."""
        with self.database.pool.connection() as connection, connection.transaction():
            deleted = connection.execute(
                "DELETE FROM reconcile_findings WHERE resolved_at < clock_timestamp() - %s",
                (FINDINGS_KEPT,)).rowcount
            current.started_at = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        self._log("started", current, deleted=deleted)

    def run_batch(self, current: _Pass) -> bool:
        """Judge the next ``BATCH_DAYS`` player-days and record them; whether
        any are left."""
        with self.database.pool.connection() as connection:
            with connection.transaction():
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
                players = [int(row[0]) for row in connection.execute(
                    "SELECT id FROM players WHERE active AND id > %s ORDER BY id LIMIT %s",
                    (current.after, BATCH_DAYS))]
                findings = judge(connection, current.season, current.day, players)
                repairing = connection.execute(
                    """
                    SELECT EXISTS (SELECT 1 FROM season_repairs
                        WHERE official_season_id = %s AND queued_through_player_id > 0
                          AND boards_queued_at IS NULL)
                    """,
                    (current.season.official_season_id,),
                ).fetchone()[0]
            last = players[-1] if len(players) == BATCH_DAYS else None
            assert current.started_at is not None
            with connection.transaction():
                current.counts.update(record(
                    connection, current.day, current.after, last, findings,
                    pass_started=current.started_at, fix=not repairing,
                    fix_room=PASS_FIX_LIMIT - current.counts["queued"]))
                current.counts.update(f"seen:{finding.check}" for finding in findings)
        current.counts["days"] += len(players)
        if last is not None:
            current.after = last
            return True
        current.after = 0
        current.day += domain.RANKED_DAY_DURATION
        return current.day <= current.last_day

    @staticmethod
    def _log(status: str, current: _Pass, **detail: Any) -> None:
        print(json.dumps({
            "event": "reconcile_pass", "slot": current.slot, "status": status,
            "season": current.season.official_season_id, **dict(current.counts), **detail,
        }), flush=True)
