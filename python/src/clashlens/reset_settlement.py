"""What a player's Reset reading proves, for both days it bounds.

A Reset reading ends one Legend day and starts the next. ``settle_end_reading``
decides, once, what the ended day's end reading proves: read before the game
applied the automatic defense loss, before it finished crediting the day's
last battles, or exactly the day's end. The ended day saves that as its next
start, and ``settled_start`` starts the next day from it, so both days use
the same trophies.

Separately, the game can apply the previous day's automatic defense loss
minutes after the 05:00 UTC Reset, so the profile read at the Reset is only
provisional. Each Reset starts as ``provisional``. Once its delayed
settlement check has finished, the check is judged against seven
conservative guards: ``settled``, with the accepted trophies and their
proof, or ``unresolved`` with its reasons. New ``settled`` verdicts need
``CLASHLENS_ENABLE_NEW_RESET_PROOFS``; otherwise a passing check stays
``provisional`` with its candidate proof. The Season summary accepts a day's
end on a settled check (``season_summaries``).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary, ranked_day_inputs
from .boundary_manifest import reset_proof_facts
from .collector_reset import COLLECTION_WINDOW, SETTLEMENT_DELAY
from .db import (
    ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
    Database,
)
from .domain import (
    BATTLE_DAY_GRACE,
    SEASON_START_TROPHIES,
    TROPHY_ALLOCATION_RULE_VERSION,
    battle_day_for,
    battle_window,
    is_season_boundary,
    ranked_day_for,
)
from .ranked_day_inputs import Reading
from .reconciliation import (
    DISPUTED_BATTLE_REASONS,
    MAX_DAILY_DEFENSES,
    RECONCILIATION_RULE_VERSION,
    BattleContribution,
    CoverageObservation,
    ReconciliationInput,
    _zero_defense_loss,
    automatic_defense_loss,
)

PROOF_RULE_VERSION = "reset-settlement-observed-adjustment-v2"
NEW_PROOFS_SWITCH = "CLASHLENS_ENABLE_NEW_RESET_PROOFS"
PROVISIONAL, SETTLED, UNRESOLVED = "provisional", "settled", "unresolved"
TERMINAL_WORK = frozenset({"complete", "failed", "cancelled"})
DAY = timedelta(days=1)
# The longest a chain of settled Resets is rechecked after one changes.
MAX_CASCADE = 28
# A defense reaches the profile about 2 minutes after it starts (90% within
# 3.4) and an attacker's own attack about 4 minutes after its report time,
# measured on 7 October 2026, so a Reset reading can miss a battle that
# landed shortly before it.
RESET_READING_BATTLE_LAG = timedelta(minutes=10)
# Saved battles keep no length, so a defense is taken to land this long
# after its report time, the longest a battle lasts.
LONGEST_BATTLE = timedelta(minutes=3)
# Two readings minutes apart can both miss the same delayed credit, so a
# later reading proves an end only this long after the end Reset reading,
# and after the Reset.
LATER_READING_GAP, LATER_READING_AFTER_RESET = timedelta(minutes=15), timedelta(minutes=20)


# How far a saved day's end is proven, weakest last.
VERIFIED, BALANCED, CONTRADICTED, UNPROVEN = (
    "verified", "balanced", "contradicted", "unproven",
)


@dataclass(frozen=True, slots=True)
class DayEnd:
    """What a saved day proves about the Reset it ends: one answer for the
    Daily board and the Season summary, read by ``day_ends``.

    ``proof`` is ``verified`` when something besides the day's own
    calculation proves its end: the game's official Season-end total, or a
    settled Reset check landing on the next day's start, never a
    Season-opening one, which only shows 5,000. It is ``balanced`` when the
    day is Complete, so its Reset readings at both ends and every battle
    between agree, and the end reading was not reset by the game. A later
    reading, taken after the end Reset reading and before the player's
    first battle of the next day, showing neither the day's end nor the end
    before an automatic loss its end Reset reading had not applied yet,
    makes it ``contradicted``: both Reset readings can miss the same delayed
    credit. Anything else is ``unproven``.

    ``proven_end`` is what every reader of a day's end takes from it.
    """

    state: str
    final: int | None
    automatic_loss: int | None
    automatic_state: str
    boundary_kind: str | None
    # The day's start, without a missing or incomplete start reading.
    start: int | None
    # The end Reset reading as read, without a missing or incomplete one.
    end_reading: int | None
    next_start: int | None
    official_end: bool
    settled: int | None
    boundary_at: datetime
    # The automatic loss the end Reset reading had not applied yet.
    unsettled_loss: int = 0
    end_read_at: datetime | None = None
    later: int | None = None
    later_at: datetime | None = None
    # Defense slots the day used, counting "no opponent, no battle" rows.
    defense_slots: int = 0
    coverage_complete: bool = False
    # When the day's last counted battle reached the profile at the latest.
    last_landed: datetime | None = None
    # Its start is 5,000 on a Season's Day 1 or the day before's proven end.
    start_proven: bool = False

    @property
    def known_loss(self) -> int:
        """The day's automatic loss when known, 0 otherwise."""
        return int(self.automatic_loss or 0) if self.automatic_state in {
            "calculated", "confirmed",
        } else 0

    @property
    def end_reset(self) -> bool:
        """The game resets trophies at a Season's end, and raises a total
        at or below 5,000 at a weekly one, so that Reset's reading proves
        nothing."""
        if self.boundary_kind == "season":
            return True
        total = self.final if self.final is not None else self.end_reading
        return self.boundary_kind == "weekly" and (total or 0) <= 5000

    @property
    def before_loss(self) -> int | None:
        """The Complete day's trophies at the Reset before the automatic
        loss: its end plus that loss."""
        if self.state != "Complete" or self.final is None:
            return None
        return self.final + int(self.automatic_loss or 0)

    def agrees(self, total: int) -> bool:
        """Whether the later reading, if any, is ``total`` after the day's
        known automatic loss, or before only the part of it the end Reset
        reading had not applied yet."""
        return not later_reading_contradicts(
            self.later, total - self.known_loss, self.unsettled_loss
        )

    @property
    def proven_end(self) -> tuple[int, int] | None:
        """The proven end of the day ending at this Reset, after its
        automatic loss, and that loss, or None: the Daily board shows their
        sum, the next day of a day not Complete starts from the end
        (``settled_start``), and the next Reset's settlement check roots on
        it. A verified or balanced day proves its own. Any other is proven
        by two readings: the later reading, when the end Reset reading is it
        plus the day's automatic loss (calculated, confirmed, or none on a
        day that used defense slots), read ``RESET_READING_BATTLE_LAG`` after
        the day's last battle landed, ``LATER_READING_GAP`` after the end
        Reset reading and ``LATER_READING_AFTER_RESET`` after the Reset, with
        continuous battle logs; never at a Season's end, nor at a weekly
        Reset at 5,000 or below, which can be the game's raise. On 7 October
        2026 one reading changed with no battle between (#R988P2Y9: 5,017 at
        05:08:33, 4,977 at 05:22:14), and two minutes apart can both be out
        of date (#8RRYVCYQU read 4,814 at 05:01:16, missing 176 trophies of
        attacks from before 04:31). A day whose start is proven
        (``start_proven``) and whose calculated end, that start plus its
        battles less its automatic loss, is not the later reading proves
        nothing so: both readings can miss the same credit."""
        if self.proof in {VERIFIED, BALANCED}:
            return int(self.final or 0), int(self.automatic_loss or 0)
        if (
            self.end_reading is None
            or self.boundary_kind == "season"
            or self.boundary_kind == "weekly" and self.end_reading <= 5000
            or not self.coverage_complete
            or self.later is None
            or self.later_at is None
            or self.end_read_at is None
            or self.later_at < self.end_read_at + LATER_READING_GAP
            or self.later_at < self.boundary_at + LATER_READING_AFTER_RESET
            or self.last_landed is not None
            and self.later_at < self.last_landed + RESET_READING_BATTLE_LAG
        ):
            return None
        if self.automatic_state in {"calculated", "confirmed"}:
            loss = int(self.automatic_loss or 0)
        elif self.automatic_state == "not_applicable" and self.defense_slots > 0:
            loss = 0
        else:
            return None
        if self.end_reading != self.later + loss or (
            self.start_proven and self.final is not None and self.final != self.later
        ):
            return None
        return self.later, loss

    @property
    def proof(self) -> str:
        if self.before_loss is None:
            return UNPROVEN
        if self.official_end or (
            self.settled is not None
            and self.settled == self.next_start
            and not is_season_boundary(self.boundary_at)
        ):
            return VERIFIED
        if self.end_reset:
            return UNPROVEN
        return BALANCED if self.agrees(self.before_loss) else CONTRADICTED


def day_ends(
    connection: Any, version_ids: list[int],
    facts: Mapping[int, Mapping[str, Any] | None] | None = None,
) -> dict[int, DayEnd]:
    """Each saved day's ``DayEnd``, keyed by its version (``day_end``). Its
    proof reads what the day stored when it was calculated, its later reading
    and settled Reset check (``boundary_manifest.stored_proof``), so every
    reader of one saved version gets its stored proof and end, or ``facts``,
    what the evidence saved now gives (``boundary_manifest.reset_proof_facts``),
    checked against it.
    """
    if not version_ids:
        return {}
    rows = connection.execute(
        """
        SELECT ranked.id, ranked.state, ranked.final_trophies_before_reset,
               ranked.automatic_defense_loss, ranked.automatic_defense_evidence_state,
               ranked.failure_reasons, ranked.start_trophies, ranked.next_start_trophies,
               jsonb_build_object(
                   'boundary_kind', ranked.input_evidence -> 'boundary_kind',
                   'next_start_trophies', ranked.input_evidence -> 'next_start_trophies',
                   'end_baseline_evidence', jsonb_build_object(
                       'official_final_trophies',
                       ranked.input_evidence -> 'end_baseline_evidence'
                           -> 'official_final_trophies',
                       'profile', ranked.input_evidence -> 'end_baseline_evidence'
                           -> 'profile'
                   ),
                   'zero_result_defense_slots',
                   ranked.input_evidence -> 'zero_result_defense_slots',
                   'previous_day', ranked.input_evidence -> 'previous_day',
                   'contributions', ranked.input_evidence -> 'contributions'
               ),
               ranked.formula_components, ranked.defense_count, ranked.coverage_complete,
               ranked.season_day_number, ranked.ranked_day_end
        FROM ranked_day_versions AS ranked
        WHERE ranked.id = ANY(%s)
        """,
        (version_ids,),
    ).fetchall()
    columns = (
        "state", "final_trophies_before_reset", "automatic_defense_loss",
        "automatic_defense_evidence_state", "failure_reasons", "start_trophies",
        "next_start_trophies", "input_evidence", "formula_components",
        "defense_count", "coverage_complete", "season_day_number", "ranked_day_end",
    )
    ends = {}
    for row in rows:
        saved = dict(zip(columns, row[1:14], strict=True))
        frozen = (saved["formula_components"] or {}).get("reset_proof") or {}
        if facts is not None:
            frozen = facts.get(int(row[0])) or {}
        ends[int(row[0])] = day_end(saved, frozen.get("settled"), frozen)
    return ends


def day_end(
    saved: Mapping[str, Any], settled: int | None, facts: Mapping[str, Any]
) -> DayEnd:
    """A day's ``DayEnd`` from its saved result, as ``day_ends`` reads it or
    its calculation is about to save it, its settled Reset check's trophies
    and what its proof reads besides the day (``facts``). Its start is
    proven by 5,000 on a Season's Day 1 or by the proven end of the day
    before that it saved when calculated (``previous_day.proven_end``)."""
    evidence = saved["input_evidence"] or {}
    reasons = set(saved["failure_reasons"] or ())
    end_evidence = evidence.get("end_baseline_evidence") or {}
    read_at = (end_evidence.get("profile") or {}).get("observed_at")
    later = facts.get("later")
    start = None if reasons & {
        "missing_start_baseline", "start_baseline_incomplete",
    } else saved["start_trophies"]
    landed = [
        datetime.fromisoformat(str(battle["battle_timestamp"]))
        + (LONGEST_BATTLE if battle.get("lens") == "defense" else timedelta(0))
        for battle in evidence.get("contributions") or ()
        if battle.get("included") is True and battle.get("battle_timestamp")
    ]
    return DayEnd(
        state=str(saved["state"]), final=saved["final_trophies_before_reset"],
        automatic_loss=saved["automatic_defense_loss"],
        automatic_state=str(saved["automatic_defense_evidence_state"]),
        boundary_kind=evidence.get("boundary_kind"), start=start,
        end_reading=None if reasons & {
            "missing_end_baseline", "end_baseline_incomplete",
        } else evidence.get("next_start_trophies"),
        next_start=saved["next_start_trophies"],
        official_end=end_evidence.get("official_final_trophies") is not None,
        settled=settled, boundary_at=saved["ranked_day_end"],
        end_read_at=datetime.fromisoformat(str(read_at)) if read_at else None,
        unsettled_loss=int(
            (saved["formula_components"] or {}).get("unsettled_automatic_loss") or 0
        ),
        later=int(later["trophies"]) if later else None,
        later_at=datetime.fromisoformat(later["read_at"]) if later else None,
        defense_slots=int(saved["defense_count"] or 0)
        + int(evidence.get("zero_result_defense_slots") or 0),
        coverage_complete=bool(saved["coverage_complete"]),
        last_landed=max(landed, default=None),
        start_proven=start is not None and (
            saved["season_day_number"] == 1 and start == SEASON_START_TROPHIES
            or (evidence.get("previous_day") or {}).get("proven_end") == start
        ),
    )


def profile_rechecks(
    connection: Any, player_id: int, observation_id: int
) -> tuple[list[tuple[int, datetime]], datetime]:
    """The (player, Reset) day whose later reading a profile just saved can
    change, none when it was read after the player's first battle of the
    next day, with its day lock taken (``lock_rechecked_days``), and when
    the profile was read."""
    read_at = connection.execute(
        "SELECT response_completed_at FROM collector_observations WHERE id = %s",
        (observation_id,),
    ).fetchone()[0]
    boundary_at = ranked_day_for(read_at).start
    first_new_day = ranked_day_inputs.load_first_reports(
        connection, player_id, read_at, battle_window(boundary_at)[0],
        boundary_at + DAY,
    )[1]
    if first_new_day is not None and read_at >= first_new_day:
        return [], read_at
    return lock_rechecked_days(connection, [(player_id, boundary_at)]), read_at


def battle_log_rechecks(
    connection: Any, observation_id: int, reporter_id: int,
    observed_at: datetime, has_row_gap: bool,
) -> list[tuple[int, datetime]]:
    """The (player, Reset) days whose later reading a battle log just saved
    can move, with their day locks taken (``lock_rechecked_days``): each
    player's whose first battle of a Legend day it brings first, ending the
    time a later reading of the day before can come from, and its own
    player's when it holds a row whose battle cannot be read."""
    firsts: dict[tuple[int, datetime], datetime] = {}
    for player_id, stamped_at in connection.execute(
        """
        SELECT side.player_id, evidence.battle_timestamp
        FROM battle_evidence AS evidence
        JOIN legend_battles AS battle ON battle.id = evidence.battle_id
        CROSS JOIN LATERAL (
            VALUES (battle.attacker_player_id), (battle.defender_player_id)
        ) AS side (player_id)
        WHERE evidence.observation_id = %s AND evidence.battle_timestamp IS NOT NULL
        """,
        (observation_id,),
    ).fetchall():
        key = (int(player_id), battle_day_for(stamped_at).start)
        firsts[key] = min(firsts.get(key, stamped_at), stamped_at)
    days = [
        (player_id, boundary_at)
        for (player_id, boundary_at), stamped_at in firsts.items()
        if ranked_day_inputs.load_first_reports(
            connection, player_id, stamped_at, battle_window(boundary_at)[0],
            boundary_at + DAY,
        )[1] == stamped_at
    ]
    if has_row_gap:
        days.append((reporter_id, ranked_day_for(observed_at).start))
    return lock_rechecked_days(connection, days)


def lock_rechecked_days(
    connection: Any, days: list[tuple[int, datetime]]
) -> list[tuple[int, datetime]]:
    """Take the calculation lock of each (player, Reset) day, by player and
    day, and return them so. The saving job takes them before its army
    battle locks, its Reset pair's work lock and any Reset's publication
    lock, the day calculation's order; ``recheck_later_readings`` then runs
    after that work."""
    days = sorted(set(days))
    for player_id, boundary_at in days:
        ranked_day_inputs.lock_ranked_day(
            connection, player_id, ranked_day_for(boundary_at - DAY)
        )
    return days


def lock_publications(
    connection: Any, observation_id: int, days: list[tuple[int, datetime]]
) -> None:
    """For a profile just saved, before its Reset proof locks: its Reset
    pair's work lock, then the publication locks of that pair's Reset and
    of ``days``, oldest first, as a battle log's army decodes take them."""
    from .boundary import lock_boundary_members
    from .reset_baselines import _load_reset_baseline_context

    boundaries = {boundary_at for _, boundary_at in days}
    context = _load_reset_baseline_context(connection, observation_id)
    if context is not None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"reset-baseline:{context[0]}",),
        )
        boundaries.add(context[4])
    for boundary_at in sorted(at.astimezone(UTC) for at in boundaries):
        lock_boundary_members(connection, boundary_at)


def recheck_later_readings(
    database: Database, connection: Any, days: list[tuple[int, datetime]],
    cause: str, *, read_at: datetime | None = None,
) -> None:
    """``queue_later_reading_recheck`` for each day ``profile_rechecks`` or
    ``battle_log_rechecks`` locked, taking the Resets' publication locks
    oldest first."""
    from .boundary import lock_boundary_members

    for boundary_at in sorted({boundary_at for _, boundary_at in days}):
        lock_boundary_members(connection, boundary_at)
    for player_id, boundary_at in days:
        queue_later_reading_recheck(
            database, connection, player_id, boundary_at, cause, read_at=read_at
        )


def queue_later_reading_recheck(
    database: Database, connection: Any, player_id: int, boundary_at: datetime,
    cause: str, *, read_at: datetime | None = None,
) -> None:
    """Queue one recalculation of the player's Legend day ending at
    ``boundary_at`` when evidence just saved, named by ``cause``, changes its
    later reading (``ranked_day_inputs.load_later_reading``): a profile read
    at ``read_at``, or a battle or unreadable row moving where that reading
    can come from: a day whose result a later reading settles or disproves
    (``ranked_day_inputs.LATER_READING_DAY_SQL``), a Complete one saved
    without a later reading only when the new one disproves it, and, whatever
    its state, a day whose proven end a reader used is no longer the one it
    proves (``reconciliation_db.proven_end_moved``), whose recalculation
    judges again the checks it roots. The day's calculation lock makes the
    two meet. This covers evidence saved after the day-end recheck, such as
    a response recovered late, once per day and cause, at backfill priority.
    The Reset's board is checked too (``_queue_board_correction``), and a
    day whose entry it changes is calculated again, which stores the
    evidence the entry reads."""
    ended = ranked_day_for(boundary_at - DAY)
    ranked_day_inputs.lock_ranked_day(connection, player_id, ended)
    board_moved = _queue_board_correction(database, connection, player_id, boundary_at)
    day = connection.execute(
        f"""
        SELECT {ranked_day_inputs.LATER_READING_DAY_SQL}, state,
               next_start_trophies,
               COALESCE((formula_components ->> 'unsettled_automatic_loss')::int, 0),
               (input_evidence -> 'end_baseline_evidence'
                   -> 'profile' ->> 'observed_at')::timestamptz,
               input_evidence -> 'later_next_start_reading'
        FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s
        ORDER BY version DESC LIMIT 1
        """,
        (player_id, ended.start, RECONCILIATION_RULE_VERSION),
    ).fetchone()
    if day is None or day[4] is None or read_at is not None and read_at <= day[4]:
        return
    current = ranked_day_inputs.load_later_reading(
        database, connection, player_id, ended, day[4]
    ) if day[0] else None
    saved = (
        (datetime.fromisoformat(day[5]["read_at"]), int(day[5]["trophies"]))
        if day[5] else None
    )
    if day[1] == "Complete" and saved is None:
        moved = current is not None and day[2] is not None and later_reading_contradicts(
            current[1], int(day[2]), int(day[3])
        )
    else:
        moved = current != saved
    if not (day[0] and moved) and not board_moved:
        from .reconciliation_db import proven_end_moved

        if not proven_end_moved(database, connection, player_id, ended.start):
            return
    _queue_recalculation(connection, player_id, ended, cause)


def _queue_recalculation(connection: Any, player_id: int, ended: Any, cause: str) -> None:
    """Queue one recalculation of the player's Legend day ``ended``, once
    per day and ``cause``, at backfill priority, unless its day-end
    calculation, which makes the same check, waits already."""
    day_text = ended.start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if connection.execute(
        """
        SELECT 1 FROM python_processing_jobs_worker
        WHERE deduplication_key = %s
          AND state IN ('pending', 'waiting_retry', 'waiting_dependency')
        """,
        (f"reconcile:day-end:{player_id}:{day_text}:{RECONCILIATION_RULE_VERSION}",),
    ).fetchone():
        return
    connection.execute(
        """
        INSERT INTO python_processing_jobs_worker (
            observation_id, work_type, deduplication_key, input_json,
            state, due_at, parser_version, processing_version,
            domain_rule_version, analytics_rule_version, priority
        ) VALUES (
            NULL, 'reconcile_ranked_day', %s, %s, 'pending', clock_timestamp(),
            %s, %s, %s, %s, %s
        )
        ON CONFLICT (deduplication_key) DO NOTHING
        """,
        (
            f"reconcile:later-reading:{player_id}:{day_text}:{cause}",
            Jsonb({
                "player_id": int(player_id),
                "ranked_day_start": day_text,
                "trigger": "later_reading",
            }),
            DEFAULT_PARSER_VERSION,
            PROCESSING_VERSION,
            DOMAIN_RULE_VERSION,
            ANALYTICS_RULE_VERSION,
            PYTHON_BACKFILL_PRIORITY,
        ),
    )


def _queue_board_correction(
    database: Database, connection: Any, player_id: int, boundary_at: datetime
) -> bool:
    """Queue a correction of the Reset's newest board when evidence saved
    since it froze its inputs changes the player's entry: the day's proof
    read with the board's frozen later reading and Reset check, and read
    with them as they are now, give other trophies or another label. The
    correction freezes the board's inputs again. Whatever the day's state;
    one correction is queued per board. The Reset's publication lock is held
    from the start: a board frozen meanwhile is the one compared, and one
    not frozen yet waits and reads this evidence. Whether the entry changes."""
    from .boundary import lock_boundary_members, queue_board_correction
    from .boundary_manifest import reset_trophies

    lock_boundary_members(connection, boundary_at)
    newest = """
        SELECT id FROM boundary_publication_generations
        WHERE boundary_at = %(at)s
          AND snapshot_state <> 'superseded' AND army_state <> 'superseded'
        ORDER BY generation DESC LIMIT 1
    """
    entry = connection.execute(
        f"""
        SELECT generation.id, entry.ranked_day_version_id,
               (entry.input_identity -> 'profile_snapshot' ->> 'observation_id')::bigint,
               (entry.input_identity -> 'profile_snapshot' ->> 'observed_at')::timestamptz,
               (entry.input_identity -> 'profile_snapshot' ->> 'trophies')::integer,
               entry.input_identity -> 'reset_proof'
        FROM boundary_publication_generations AS generation
        CROSS JOIN LATERAL boundary_publication_manifest_entries(
            generation.snapshot_manifest_id
        ) AS entry
        WHERE generation.id = ({newest})
          AND entry.player_id = %(player)s
          AND entry.ranked_day_version_id IS NOT NULL
          AND entry.input_identity ->> 'snapshot_quality' = 'eligible'
          AND entry.input_identity ? 'reset_proof'
        """,
        {"at": boundary_at, "player": player_id},
    ).fetchone()
    if entry is None:
        return False
    version_id = int(entry[1])
    reading = {player_id: (version_id, int(entry[2]), entry[3], int(entry[4]))}
    if reset_trophies(
        connection, boundary_at, reading, {version_id: entry[5]}
    ) == reset_trophies(
        connection, boundary_at, reading, reset_proof_facts(database, connection, [version_id])
    ):
        return False
    queue_board_correction(connection, boundary_at, int(entry[0]), queue=True)
    return True


def later_reading_contradicts(
    later: int | None, next_start: int, pending_loss: int
) -> bool:
    """Whether a profile read after a day's end Reset reading and before the
    player's first battle of the next day disproves the day's settled next
    start: it shows neither that start nor that start plus
    ``pending_loss``, the automatic loss the end reading had not applied
    yet. Both Reset readings can miss the
    same delayed credit, so their agreeing alone proves no day's end."""
    return later is not None and later not in {next_start, next_start + pending_loss}


def start_proven(data: ReconciliationInput) -> bool:
    """Whether the day's start is proven apart from its own Reset reading:
    a Season's first day starts at 5,000, and a day before ending on that
    same reading proves it, Complete or with its end proven (``proven_end``)."""
    previous = data.previous_day
    return data.season_first_day or (
        previous is not None
        and (previous.complete or previous.proven_end is not None)
        and previous.end_baseline_id == data.start_baseline_id
    )


def settled_start(data: ReconciliationInput) -> tuple[int | None, int, int]:
    """The day's starting trophies, what was taken off the Reset reading,
    and what a later reading added to it.

    The previous day's Complete result can say its end reading, this day's
    start reading, came before the game applied its automatic defense loss. The
    day then starts from the reading less that loss, as the player did once
    it landed. On 2 October 2026 that explained 728 of 733 next days whose
    start was too high and a later reading could check. It can instead say a
    later reading settled the Reset reading; the day then starts from that.
    A previous day not Complete whose end two readings prove
    (``DayEnd.proven_end``) starts the day from that end.
    """
    previous = data.previous_day
    if (
        data.start_trophies is None
        or previous is None
        or previous.end_baseline_id is None
        or previous.end_baseline_id != data.start_baseline_id
    ):
        return data.start_trophies, 0, 0
    if not previous.complete:
        start = data.start_trophies if previous.proven_end is None else previous.proven_end
        return start, data.start_trophies - start, 0
    if not (previous.unsettled_automatic_loss or previous.reset_reading_correction):
        return data.start_trophies, 0, 0
    loss = previous.unsettled_automatic_loss
    correction = previous.reset_reading_correction
    return data.start_trophies - loss + correction, loss, correction


@dataclass(frozen=True, slots=True)
class EndReading:
    """What a day's end Reset reading proves (see ``settle_end_reading``)."""

    automatic_loss: int | None
    automatic_state: str
    # The automatic loss for all 8 slots of a day with none used, when the
    # reading shows the game charged it; it comes off the day's end.
    zero_defense_loss: int
    expected_next: int
    next_start_trophies: int
    residual: int
    unsettled_loss: int
    reading_correction: int
    battles_after_reading: tuple[str, ...]
    # A later reading disproves the balanced day's end.
    later_contradicts: bool


def settle_end_reading(
    data: ReconciliationInput,
    contributions: tuple[BattleContribution, ...],
    *,
    expected_next: int,
    final_trophies: int,
    automatic_loss: int | None,
    automatic_state: str,
    defense_count: int,
    end_hidden_by_reset: bool,
    start_proven: bool,
    clean: bool,
) -> EndReading:
    """The ended day's next start from its end Reset reading, which the day
    ends on and the next day starts from.

    ``expected_next`` is the day's calculated next start. Only a ``clean``
    day, ended, with continuous battle logs and nothing failed, malformed or
    disputed, is read below: disputed or missing battles could make any gap
    look like one of these. The game's official Season-end total already
    counts every battle and the automatic loss, so nothing read before it
    settles the day.

    A clean day that balances is still disproved by a later reading
    (``later_reading_contradicts``); the day then cannot be Complete, and the
    next day does not start from its end.
    """
    assert data.next_start_trophies is not None
    reading = data.next_start_trophies
    residual = reading - expected_next
    next_start, unsettled_loss, reading_correction, charged = reading, 0, 0, 0
    battles_after_reading: tuple[str, ...] = ()
    official_end = "official_final_trophies" in data.end_baseline_evidence
    zero_defense_loss = _zero_defense_loss(data, defense_count)
    later = data.later_next_start_reading
    if (
        zero_defense_loss
        and (
            residual == -zero_defense_loss
            or (later is not None and later[1] == expected_next - zero_defense_loss)
        )
        and not end_hidden_by_reset
        and (data.boundary_kind is None or final_trophies - zero_defense_loss > 5000)
        and clean
    ):
        # The game can charge a day with no used defense slots the automatic
        # loss for all 8, or charge it nothing; its battles do not say which.
        # On 6 October 2026, 3 such days lost exactly this (304, 272 and 248)
        # and 86 kept their trophies, including every one read again later
        # that day, with the same battle counts on the days around them. Only
        # a reading after the Reset tells them apart, so only one exactly
        # this loss below the day's end takes it. A Reset reading showing no
        # change, or missing the day's credit, followed by one before any
        # new-day battle showing the loss, was read before the game applied
        # it.
        automatic_loss, automatic_state = zero_defense_loss, "calculated"
        charged = zero_defense_loss
        expected_next = final_trophies - zero_defense_loss
        residual += zero_defense_loss
    if (
        automatic_loss
        and residual == automatic_loss
        and automatic_state == "calculated"
        and not end_hidden_by_reset
        and not official_end
        and clean
    ):
        # The game applies the automatic defense loss about 7 to 13 minutes
        # after the Reset, and the Reset reading usually comes first, so it
        # sits exactly the calculated loss above the day's end: 589 days on
        # 2 October 2026, 998 on 3 October and 2,889 on 5 October. The loss
        # stays calculated, and the next day starts from the reading less it.
        unsettled_loss = automatic_loss
        next_start = reading - unsettled_loss
        residual = 0
    if (
        residual
        and later is not None
        and later[1] == expected_next
        and not end_hidden_by_reset
        and clean
    ):
        # The Reset reading can come before the game finished crediting the
        # ended day: on 6 October 2026, #P20G0CUJY read 4,766 at 05:02:41,
        # all 308 of its last day's attack gains missing, then 5,074 at
        # 05:09:56, before any new-day battle. In 12 of 13 such October 5
        # days, a reading after the Reset one and before any new-day battle
        # was exactly the calculated next start. That later reading settles
        # it.
        reading_correction = later[1] - reading
        next_start = later[1]
        residual = 0
    if (
        residual
        and later is None
        and start_proven
        and not official_end
        and not end_hidden_by_reset
        and clean
    ):
        # The Reset reading can also come before the ended day's last battles
        # landed: #2GL8CJL read 4,839 at 05:00:31 on 7 October 2026, before
        # its attack reported at 05:02:15 added 40. On 5 and 6 October this
        # explained 23 mismatched days, each exactly. Without a later
        # reading, the reading plus the battles that landed last, less any
        # automatic loss the game had not applied yet, settles the day. A
        # start the previous day did not prove could be wrong by just those
        # battles, and a later reading other than the calculated end
        # disproves it.
        battles_after_reading, unsettled_loss = _battles_after_reading(
            contributions, data, expected_next,
            automatic_loss if automatic_state == "calculated" else 0,
        )
        if battles_after_reading:
            reading_correction = expected_next + unsettled_loss - reading
            next_start = expected_next
            residual = 0
    later_contradicts = (
        not residual
        and later is not None
        and not official_end
        and not end_hidden_by_reset
        and clean
        and later_reading_contradicts(
            later[1], next_start, unsettled_loss
        )
    )
    return EndReading(
        automatic_loss, automatic_state, charged, expected_next, next_start,
        residual, unsettled_loss, reading_correction, battles_after_reading,
        later_contradicts,
    )


def _battles_after_reading(
    contributions: tuple[BattleContribution, ...],
    data: ReconciliationInput,
    expected_next: int,
    pending_loss: int,
) -> tuple[tuple[str, ...], int]:
    """The fewest of the day's last landed battles that the end Reset reading
    must have missed for it to equal ``expected_next``, with or without
    ``pending_loss`` still to come off it, and that loss; or none. An attack
    lands at its report time and a defense at its report time plus its
    length; every battle landing after the reading is missed, and so may be
    any landing up to ``RESET_READING_BATTLE_LAG`` before it."""
    observed_at = data.end_baseline_evidence.get("profile", {}).get("observed_at")
    if not isinstance(observed_at, str) or data.next_start_trophies is None:
        return (), 0
    reading_at = datetime.fromisoformat(observed_at)

    def landed_at(battle: BattleContribution) -> datetime:
        assert battle.battle_timestamp is not None
        if battle.lens == "defense":
            return battle.battle_timestamp + timedelta(seconds=battle.battle_seconds or 0)
        return battle.battle_timestamp

    recent = sorted(
        (
            battle for battle in contributions
            if battle.battle_timestamp is not None
            and landed_at(battle) >= reading_at - RESET_READING_BATTLE_LAG
        ),
        key=lambda battle: (landed_at(battle), battle.battle_identity),
    )
    pending = sum(1 for battle in recent if landed_at(battle) > reading_at)
    for count in range(max(pending, 1), len(recent) + 1):
        missed = recent[-count:]
        change = sum(
            (battle.amount or 0) * (1 if battle.lens == "offense" else -1)
            for battle in missed
        )
        for loss in (0, pending_loss):
            if data.next_start_trophies + change - loss == expected_next:
                return tuple(battle.battle_identity for battle in missed), loss
    return (), 0


def record_provisional_boundary(
    connection: Any,
    *,
    player_id: int,
    boundary_at: datetime,
    sweep_id: int,
    early_baseline_id: int,
    early_state: str,
    reasons: list[str],
) -> None:
    """Record the Reset pair evidence of a still-provisional boundary.

    The pair's state and failure reasons are kept as they are: a complete
    pair proves the responses were processed, not that trophies settled. A
    repeat with the same evidence changes nothing, and a boundary already
    settled or unresolved keeps that verdict. A boundary with a settlement
    check keeps the check's reasons; the pair's own are in its proof.
    """
    early = {"baseline_id": early_baseline_id, "state": early_state, "reasons": reasons}
    connection.execute(
        """
        INSERT INTO reset_boundary_settlements (
            player_id, boundary_at, sweep_id, early_baseline_id,
            proof_json, reasons
        ) VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (player_id, boundary_at) DO UPDATE
        SET sweep_id = EXCLUDED.sweep_id,
            early_baseline_id = EXCLUDED.early_baseline_id,
            proof_json = reset_boundary_settlements.proof_json || EXCLUDED.proof_json,
            reasons = CASE WHEN reset_boundary_settlements.delayed_work_id IS NULL
                           THEN EXCLUDED.reasons
                           ELSE reset_boundary_settlements.reasons END,
            change_number = reset_boundary_settlements.change_number + 1,
            updated_at = clock_timestamp()
        WHERE reset_boundary_settlements.state = 'provisional'
          AND (
              reset_boundary_settlements.early_baseline_id,
              reset_boundary_settlements.proof_json -> 'early',
              reset_boundary_settlements.reasons
          ) IS DISTINCT FROM (
              EXCLUDED.early_baseline_id,
              EXCLUDED.proof_json -> 'early',
              CASE WHEN reset_boundary_settlements.delayed_work_id IS NULL
                   THEN EXCLUDED.reasons
                   ELSE reset_boundary_settlements.reasons END
          )
        """,
        (player_id, boundary_at, sweep_id, early_baseline_id,
         Jsonb({"early": early}), Jsonb(reasons)),
    )


@dataclass(frozen=True, slots=True)
class Root:
    """The preceding Reset's settled verdict that the target is built on."""

    boundary_at: datetime
    trophies: int
    fingerprint: str
    change_number: int
    observations: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ProofInputs:
    boundary_at: datetime
    work_status: str | None  # None when the Reset has no named check
    early_recorded: bool = False
    early: Reading | None = None
    profile: Reading | None = None
    battle_log: Reading | None = None
    log_coverage: CoverageObservation | None = None
    # Each battle in the named battle log: (battle ID, lens, report time, trophies).
    log_reports: tuple[tuple[str, str, datetime, int], ...] = ()
    # Each "no opponent, no battle" row in the named battle log, as
    # (report time, is an attack): used slots for the automatic loss only.
    zero_result_slots: frozenset[tuple[datetime, bool]] = frozenset()
    # Every saved own-side report of the ended day.
    battles: tuple[BattleContribution, ...] = ()
    # The saved day before's defense slots, counting "no opponent, no
    # battle" rows, and their losses, when its battle logs were continuous
    # and no battle disputed.
    previous_defenses: tuple[int, int] | None = None
    # The saved day before's shared proof (``DayEnd``).
    previous_end: DayEnd | None = None
    # Report times of unreadable rows in battle logs saved after the named one.
    late_unreadable: tuple[datetime | None, ...] = ()
    # Earliest report time, from either player, at or after the early reading.
    first_report_after_early: datetime | None = None
    first_new_day_report: datetime | None = None
    # Trophies of later profiles before any new-day battle; None if unprocessed.
    later_profiles: tuple[tuple[datetime, int | None], ...] = ()
    root: Root | None = None


@dataclass(frozen=True, slots=True)
class Verdict:
    state: str
    reasons: tuple[str, ...]
    trophies: int | None
    proof: dict[str, Any]


def evaluate_boundary(inputs: ProofInputs) -> Verdict:
    """Judge one Reset's named settlement check against guards 1-6, in order.

    Only an ordinary Reset whose delayed profile shows exactly the
    calculated automatic loss, on top of an independently proven start of
    the ended day plus every battle of it, can be ``settled``. The start is
    the previous Reset's settled check, else 5,000 on a Season's Day 1, else
    the day before's end by its shared proof, verified or balanced. Anything
    not yet known stays ``provisional``; anything unproven is ``unresolved``.
    """
    boundary = inputs.boundary_at
    early, profile, log = inputs.early, inputs.profile, inputs.battle_log
    readings = {"early_reading": early, "settlement_profile": profile,
                "settlement_battle_log": log}
    proof: dict[str, Any] = {
        "rule": PROOF_RULE_VERSION,
        "observations": [r.observation_id for r in readings.values() if r is not None],
        "readings": {
            name: {"observation_id": r.observation_id,
                   "request_started_at": r.request_started_at.isoformat(),
                   "response_completed_at": r.response_completed_at.isoformat(),
                   "outcome": r.outcome, "trophies": r.trophies}
            for name, r in readings.items() if r is not None
        },
    }

    def verdict(state: str, reasons: list[str], trophies: int | None = None) -> Verdict:
        return Verdict(state, tuple(dict.fromkeys(reasons)), trophies, proof)

    # 1. Named attempt and interval: a fresh profile from 05:20, then a
    # battle log requested only after that profile arrived, both by 04:55.
    if inputs.work_status is None:
        return verdict(UNRESOLVED, ["settlement_check_missing"])
    if inputs.work_status not in TERMINAL_WORK:
        return verdict(PROVISIONAL, ["settlement_check_pending"])
    if profile is None or log is None:
        missing = "profile" if profile is None else "battle_log"
        return verdict(UNRESOLVED, [f"settlement_{missing}_missing"])
    reasons = []
    if profile.request_started_at < boundary + SETTLEMENT_DELAY:
        reasons.append("settlement_profile_too_early")
    if log.request_started_at < profile.response_completed_at:
        reasons.append("battle_log_requested_before_profile_arrived")
    if max(profile.response_completed_at, log.response_completed_at) >= boundary + COLLECTION_WINDOW:
        reasons.append("settlement_check_after_window")
    if reasons:
        return verdict(UNRESOLVED, reasons)

    # 2. Every reading processed successfully, for this player.
    if not inputs.early_recorded:
        return verdict(PROVISIONAL, ["early_reading_pending"])
    if any(r is not None and r.outcome is None for r in readings.values()):
        return verdict(PROVISIONAL, ["settlement_processing_pending"])
    for name, reading in readings.items():
        if reading is None or not reading.usable:
            reasons.append(f"{name}_unusable")
    if reasons:
        return verdict(UNRESOLVED, reasons)
    assert early is not None and early.trophies is not None
    assert profile.trophies is not None

    # 3. Complete battle evidence for the ended day, from the named battle
    # log, unchanged by every report saved since.
    ended_from, ended_until = battle_window(boundary - DAY)
    coverage = inputs.log_coverage
    if (coverage is None or not coverage.valid or coverage.has_row_gap
            or coverage.malformed_row_count or coverage.unclassified_row_count
            or len(set(coverage.battle_identities)) != len(coverage.battle_identities)):
        reasons.append("battle_log_unreadable")
    if not inputs.log_reports or min(r[2] for r in inputs.log_reports) >= ended_from:
        reasons.append("battle_log_too_short")
    pinned = {
        identity: (lens, amount)
        for identity, lens, at, amount in inputs.log_reports
        if ended_from <= at < ended_until
    }
    for battle in inputs.battles:
        if (not battle.valid or battle.failure_reason or battle.disagreement
                or battle.opponent_tag is None or battle.amount is None):
            reasons.append("battle_report_unusable")
        if battle.source_rule_version != TROPHY_ALLOCATION_RULE_VERSION:
            reasons.append("rule_correction_pending")
    if {b.battle_identity: (b.lens, b.amount) for b in inputs.battles} != pinned:
        reasons.append("battle_reports_changed_after_log")
    if any(at is None or ended_from <= at < ended_until for at in inputs.late_unreadable):
        reasons.append("late_battle_log_unreadable")
    def amounts(lens: str, since: datetime, until: datetime) -> list[int]:
        return [a for _, kind, at, a in inputs.log_reports if kind == lens and since <= at < until]

    attacks = amounts("offense", ended_from, ended_until)
    defenses = amounts("defense", ended_from, ended_until)
    if max(len(attacks), len(defenses)) > MAX_DAILY_DEFENSES:
        reasons.append("battle_count_exceeds_eight")
    proof["coverage"] = {
        "battle_log_observed_at": _iso(coverage.observed_at if coverage else None),
        "earliest_report": _iso(min((r[2] for r in inputs.log_reports), default=None)),
        "battles": len(pinned),
    }
    if reasons:
        return verdict(UNRESOLVED, reasons)

    # 4. No battle between the readings, nor a new-day battle before the
    # named profile, by either player's report; no later quiet profile
    # disagrees with it.
    proof["ordering"] = {"first_report_after_early": _iso(inputs.first_report_after_early),
                         "first_new_day_report": _iso(inputs.first_new_day_report)}
    for first, reason in (
        (inputs.first_new_day_report, "new_day_battle_before_profile"),
        (inputs.first_report_after_early, "battle_between_readings"),
    ):
        if first is not None and first <= profile.response_completed_at:
            reasons.append(reason)
    if early.response_completed_at >= profile.request_started_at:
        reasons.append("early_reading_after_profile")
    if any(trophies is None for _, trophies in inputs.later_profiles):
        reasons.append("later_profile_unprocessed")
    if any(t not in (None, profile.trophies) for _, t in inputs.later_profiles):
        reasons.append("later_profile_contradicts")
    if reasons:
        return verdict(UNRESOLVED, reasons)
    if boundary.weekday() == 0 or is_season_boundary(boundary):
        return verdict(UNRESOLVED, ["special_reset_unsupported"])

    # 5 and 6. The ended day's independently proven start, plus its battles
    # and its positive automatic loss, gives the target; the early reading
    # must sit exactly that loss above it, and the named profile exactly on
    # it. The automatic loss pools the saved day before's defenses.
    zero_attacks, zero_defenses = ranked_day_inputs.slot_counts(
        inputs.zero_result_slots, ended_from, ended_until)
    season_first_day = is_season_boundary(boundary - DAY)
    previous = (0, 0) if season_first_day else inputs.previous_defenses
    automatic = None
    if 1 <= len(defenses) + zero_defenses < MAX_DAILY_DEFENSES:
        if previous is None:
            reasons.append("previous_day_defenses_unknown")
        else:
            automatic = automatic_defense_loss(
                attacks=len(attacks) + zero_attacks,
                defenses=len(defenses) + zero_defenses,
                defense_loss=sum(defenses),
                previous_defenses=previous[0],
                previous_defense_loss=previous[1],
                season_first_day=season_first_day,
            )
    proof["automatic_loss_basis"] = {
        "prior_defenses": previous[0] if previous else None,
        "prior_defense_loss": previous[1] if previous else None,
        "defenses": len(defenses), "defense_loss": sum(defenses), "automatic_loss": automatic,
        # Only present when the log holds such rows, so other proofs are unchanged.
        **{name: count for name, count in (
            ("zero_result_attacks", zero_attacks), ("zero_result_defenses", zero_defenses),
        ) if count},
    }
    root = inputs.root
    if root is None and season_first_day:
        root = Root(boundary - DAY, SEASON_START_TROPHIES, "season-rule", 0, ())
    elif root is None and inputs.previous_end is not None and (
        proven := inputs.previous_end.proven_end
    ):
        kind = inputs.previous_end.proof
        root = Root(boundary - DAY, proven[0], "previous-day-" + (
            kind if kind in {VERIFIED, BALANCED} else "two-readings"), 0, ())
    if root is None:
        reasons.append("independent_root_missing")
    elif root.boundary_at >= boundary or set(root.observations) & set(proof["observations"]):
        reasons.append("independent_root_circular")
    if not automatic and "previous_day_defenses_unknown" not in reasons:
        reasons.append("no_positive_automatic_loss")
    if reasons:
        return verdict(UNRESOLVED, reasons)
    assert root is not None and automatic is not None
    target = root.trophies + sum(attacks) - sum(defenses) - automatic
    proof["catchup"] = {
        "root": {"boundary_at": root.boundary_at.isoformat(), "trophies": root.trophies,
                 "fingerprint": root.fingerprint, "change_number": root.change_number},
        "attack_gain": sum(attacks), "defense_loss": sum(defenses), "target": target,
    }
    if early.trophies != target + automatic:
        reasons.append("early_reading_mismatch")
    if early.trophies - profile.trophies != automatic:
        reasons.append("observed_drop_mismatch")
    if profile.trophies != target:
        reasons.append("profile_catchup_unknown")
    if reasons:
        return verdict(UNRESOLVED, reasons)
    return verdict(SETTLED, [], target)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def new_proofs_enabled() -> bool:
    return os.environ.get(NEW_PROOFS_SWITCH) == "true"


def load_proof_inputs(
    database: Database, connection: Any, player_id: int, boundary_at: datetime
) -> ProofInputs | None:
    """Everything one Reset's verdict depends on, or ``None`` with no check.

    Loading stops as soon as the verdict cannot need more, so the frequent
    still-pending case stays cheap.
    """
    row = connection.execute(
        """
        SELECT work.status, work.profile_observation_id,
               work.battle_log_observation_id, early.id IS NOT NULL,
               early.profile_observation_id
        FROM reset_boundary_settlements AS settlement
        LEFT JOIN collector_work AS work
          ON work.id = settlement.delayed_work_id
         AND work.kind = 'reset_settlement'
        LEFT JOIN LATERAL (
            SELECT evidence.id, evidence.profile_observation_id
            FROM reset_baseline_evidence AS evidence
            WHERE evidence.player_id = settlement.player_id
              AND evidence.boundary_at = settlement.boundary_at
              AND evidence.processing_version = %s
            ORDER BY evidence.version DESC, evidence.id DESC
            LIMIT 1
        ) AS early ON true
        WHERE settlement.player_id = %s AND settlement.boundary_at = %s
          AND settlement.delayed_work_id IS NOT NULL
        """,
        (PROCESSING_VERSION, player_id, boundary_at),
    ).fetchone()
    if row is None:
        return None
    inputs = ProofInputs(boundary_at=boundary_at, work_status=row[0])
    if row[0] not in TERMINAL_WORK:
        return inputs
    inputs = replace(
        inputs,
        early_recorded=bool(row[3]),
        early=ranked_day_inputs.load_reading(database, connection, player_id, row[4]),
        profile=ranked_day_inputs.load_reading(database, connection, player_id, row[1]),
        battle_log=ranked_day_inputs.load_reading(database, connection, player_id, row[2]),
    )
    early, profile, log = inputs.early, inputs.profile, inputs.battle_log
    if not (early and profile and log and early.usable and profile.usable and log.usable):
        return inputs
    assert log.parser_version is not None
    coverage = next((item for item in ranked_day_inputs.load_coverage(
        database, connection, player_id, ranked_day_for(boundary_at),
        log.observation_id, log.observation_id,
    ) if item.observation_id == log.observation_id), None)
    log_observed_at = coverage.observed_at if coverage else log.response_completed_at
    reports = ranked_day_inputs.load_first_reports(
        connection, player_id, early.response_completed_at,
        battle_window(boundary_at)[0], boundary_at + DAY)
    quiet_until = min(reports[1] or boundary_at + DAY, boundary_at + DAY)
    later_profiles = ranked_day_inputs.load_profile_trophies(
        database, connection, player_id, profile.response_completed_at, quiet_until)
    root = connection.execute(
        """
        SELECT boundary_at, selected_trophies, proof_fingerprint, change_number,
               COALESCE(proof_json -> 'observations', '[]'::jsonb)
        FROM reset_boundary_settlements
        WHERE player_id = %s AND boundary_at = %s AND state = 'settled'
        """,
        (player_id, boundary_at - DAY),
    ).fetchone()
    previous = connection.execute(
        """
        SELECT id,
               defense_count
               + COALESCE((input_evidence ->> 'zero_result_defense_slots')::int, 0),
               observed_defense_loss,
               coverage_complete AND NOT failure_reasons ?| %s::text[]
        FROM ranked_day_versions
        WHERE player_id = %s AND ranked_day_start = %s
          AND reconciliation_rule_version = %s
        ORDER BY version DESC, id DESC LIMIT 1
        """,
        (sorted(DISPUTED_BATTLE_REASONS), player_id, boundary_at - 2 * DAY,
         RECONCILIATION_RULE_VERSION),
    ).fetchone()
    return replace(
        inputs,
        log_coverage=coverage,
        log_reports=ranked_day_inputs.load_log_reports(
            database, connection, log.observation_id, log.parser_version),
        zero_result_slots=ranked_day_inputs.load_zero_result_slots(
            connection, (coverage,) if coverage else ()),
        battles=ranked_day_inputs.load_contributions(
            connection, player_id, ranked_day_for(boundary_at - DAY)),
        previous_defenses=(
            (int(previous[1]), int(previous[2])) if previous and previous[3] else None
        ),
        previous_end=day_ends(connection, [int(previous[0])]).get(int(previous[0]))
        if previous else None,
        late_unreadable=tuple(ranked_day_inputs.load_unreadable_report_times(
            database, connection, player_id, log_observed_at, boundary_at + DAY
        )),
        first_report_after_early=reports[0],
        first_new_day_report=reports[1],
        later_profiles=later_profiles,
        root=Root(root[0], int(root[1]), str(root[2]), int(root[3]),
                  tuple(int(value) for value in root[4])) if root else None,
    )


def refresh_boundary(
    database: Database, connection: Any, player_id: int, boundary_at: datetime,
    *, depth: int = 0,
) -> None:
    """Re-judge one Reset and record a changed verdict (guard 7).

    Runs in the caller's transaction, under the Reset's own lock, so the
    inputs are re-read after any concurrent writer finished. A finalized
    Season keeps its verdict. Admitting a new ``settled`` verdict needs the
    switch; losing one never does. A change to a settled verdict re-judges
    the next Reset, whose target it roots, and calculates again the day it
    ends, whose saved version stores the verdict it proves its end by; that
    calculation stores the Season summary again.
    """
    from .season_retirement import is_season_detail_retired

    season_id = _lock_reset(connection, player_id, boundary_at)
    if is_season_detail_retired(connection, season_id):
        return
    inputs = load_proof_inputs(database, connection, player_id, boundary_at)
    if inputs is None:
        return
    current = connection.execute(
        """
        SELECT state, selected_trophies, proof_fingerprint, reasons, proof_json
        FROM reset_boundary_settlements
        WHERE player_id = %s AND boundary_at = %s
        FOR UPDATE
        """,
        (player_id, boundary_at),
    ).fetchone()
    verdict = evaluate_boundary(inputs)
    candidate = {"verdict": verdict.state, **verdict.proof}
    if verdict.state == SETTLED and not new_proofs_enabled() and (
        current[0], current[1]
    ) != (SETTLED, verdict.trophies):
        verdict = replace(verdict, state=PROVISIONAL, trophies=None,
                          reasons=("new_reset_proofs_disabled",))
    fingerprint = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
    if tuple(current[:4]) == (verdict.state, verdict.trophies, fingerprint, list(verdict.reasons)):
        return
    if "early" in current[4]:
        candidate["early"] = current[4]["early"]
    connection.execute(
        """
        UPDATE reset_boundary_settlements
        SET state = %s, selected_trophies = %s, proof_kind = %s,
            proof_rule_version = %s, proof_fingerprint = %s, proof_json = %s,
            reasons = %s, change_number = change_number + 1,
            updated_at = clock_timestamp()
        WHERE player_id = %s AND boundary_at = %s
        """,
        (verdict.state, verdict.trophies,
         "observed_adjustment" if verdict.state == SETTLED else None,
         PROOF_RULE_VERSION, fingerprint, Jsonb(candidate),
         Jsonb(list(verdict.reasons)), player_id, boundary_at),
    )
    if SETTLED in (current[0], verdict.state):
        _queue_recalculation(
            connection, player_id, ranked_day_for(boundary_at - DAY), f"check-{fingerprint[:16]}"
        )
        if depth < MAX_CASCADE:
            refresh_boundary(database, connection, player_id, boundary_at + DAY, depth=depth + 1)


def _lock_reset(connection: Any, player_id: int, boundary_at: datetime) -> str:
    """Hold the Reset's Season and the Reset's own lock; returns the Season."""
    from .season_retirement import acquire_season_lock_shared

    season_id = ranked_day_for(boundary_at - DAY).official_season_id
    acquire_season_lock_shared(connection, season_id)
    connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                       (f"reset-settlement:{player_id}:{boundary_at.astimezone(UTC).isoformat()}",))
    return season_id


def lock_resets(database: Database, connection: Any, observation_id: int | None,
                resets: list[tuple[int, datetime]], publications: tuple[datetime, ...] = ()) -> None:
    """Before any generation row: publication locks for member results, then
    the locks of ``resets`` and of every Reset ``observation_id`` may
    re-judge, each oldest first."""
    for boundary_at in sorted({at.astimezone(UTC) for at in publications}):
        boundary.lock_boundary_members(connection, boundary_at)
    if _has_settlements(database, connection):
        resets = [*resets, *_observation_resets(connection, observation_id, every=True)]
    for player_id, boundary_at in sorted(set(resets), key=lambda r: (r[1], r[0])):
        _lock_reset(connection, player_id, boundary_at)


def _has_settlements(database: Database, connection: Any) -> bool:
    """Whether the schema has boundary settlements; checked once per worker."""
    known = getattr(database, "_supports_reset_settlement", None)
    if known is None:
        known = connection.execute(
            "SELECT to_regclass('reset_boundary_settlements') IS NOT NULL"
        ).fetchone()[0]
        database._supports_reset_settlement = known  # type: ignore[attr-defined]
    return bool(known)


def refresh_for_observation(database: Database, connection: Any, observation_id: int) -> None:
    """Re-judge the Resets a newly processed response can change
    (``refresh_boundary``). A named check's own responses
    always count. Any other response of the player, or of an opponent in its
    battles, from up to three days after a Reset, re-judges only a finished
    check that is settled, passed every guard, or met a later profile that
    disagreed or was not processed; a battle log also one with an unusable
    report at any Reset that can read one of its battles. Every Reset it may
    re-judge is locked before choosing."""
    if not _has_settlements(database, connection):
        return
    lock_resets(database, connection, observation_id, [])
    for player_id, boundary_at in _observation_resets(connection, observation_id):
        refresh_boundary(database, connection, player_id, boundary_at)


def _observation_resets(connection: Any, observation_id: int | None, *,
                        every: bool = False) -> list[tuple[int, datetime]]:
    # Every result joins a settlement, so with none saved yet this skips the
    # battle reads, which took about 137 ms under a hot Reset lock. Checked in
    # each call: a Reset can save the first settlements at any time.
    if observation_id is None or not connection.execute(
        "SELECT EXISTS (SELECT 1 FROM reset_boundary_settlements LIMIT 1)"
    ).fetchone()[0]:
        return []
    rows = connection.execute(
        """
        WITH observed AS (
            SELECT id, player_id, endpoint, response_completed_at AS at
            FROM collector_observations WHERE id = %(id)s
        ), touched AS (
            SELECT battle.attacker_player_id AS attacker, battle.defender_player_id AS defender,
                   (SELECT min(report.battle_timestamp) FROM battle_evidence AS report
                    WHERE report.battle_id = battle.id) AS reported_at
            FROM observed
            JOIN battle_evidence AS evidence ON evidence.observation_id = observed.id
            JOIN legend_battles AS battle ON battle.id = evidence.battle_id
            WHERE observed.endpoint = 'battle_log'
        ), affected AS (
            SELECT player_id FROM observed WHERE player_id IS NOT NULL
            UNION SELECT attacker FROM touched UNION SELECT defender FROM touched
        )
        SELECT settlement.player_id, settlement.boundary_at
        FROM observed
        CROSS JOIN affected
        JOIN reset_boundary_settlements AS settlement
          ON settlement.player_id = affected.player_id
         AND settlement.boundary_at <= observed.at
         AND settlement.boundary_at > observed.at - interval '3 days'
        JOIN collector_work AS work ON work.id = settlement.delayed_work_id
        WHERE %(every)s OR observed.id IN (work.profile_observation_id, work.battle_log_observation_id)
           OR (work.status IN ('complete', 'failed', 'cancelled') AND (
                  settlement.state = 'settled'
                  OR settlement.reasons <@ '["new_reset_proofs_disabled"]'::jsonb
                  OR settlement.reasons ?| array['later_profile_contradicts', 'later_profile_unprocessed']))
        UNION
        SELECT settlement.player_id, settlement.boundary_at
        FROM touched
        JOIN reset_boundary_settlements AS settlement
          ON settlement.player_id IN (touched.attacker, touched.defender)
         AND settlement.boundary_at > touched.reported_at - %(grace)s
         AND settlement.boundary_at <= touched.reported_at + interval '2 days'
        JOIN collector_work AS work ON work.id = settlement.delayed_work_id
        WHERE %(every)s OR (work.status IN ('complete', 'failed', 'cancelled')
          AND settlement.reasons ? 'battle_report_unusable')
        ORDER BY 2, 1
        """,
        {"id": observation_id, "grace": BATTLE_DAY_GRACE, "every": every},
    ).fetchall()
    return [(int(player_id), boundary_at) for player_id, boundary_at in rows]


def refresh_terminal_work(database: Database, *, batch: int = 100) -> int:
    """Judge up to ``batch`` finished checks that no processed response
    will, as one that failed or expired before saving both responses; how
    many it judged. A finalized Season keeps its verdicts."""
    with database.pool.connection() as connection:
        if not _has_settlements(database, connection):
            return 0
        with connection.transaction():
            rows = connection.execute(
                """
            SELECT work.player_id, sweep.boundary_at
            FROM collector_reset_sweeps AS sweep
            JOIN collector_work AS work
              ON work.sweep_id = sweep.id AND work.kind = 'reset_settlement'
            JOIN reset_boundary_settlements AS settlement
              ON settlement.player_id = work.player_id
             AND settlement.boundary_at = sweep.boundary_at
            WHERE NOT EXISTS (
                    SELECT 1 FROM season_detail_retirements AS season
                    WHERE season.status IN ('finalized', 'retired')
                      AND season.season_start <= sweep.boundary_at - interval '1 day'
                      AND season.season_end > sweep.boundary_at - interval '1 day')
              AND work.status IN ('complete', 'failed', 'cancelled')
              AND settlement.state = 'provisional'
              AND (settlement.reasons = '[]'::jsonb
                   OR settlement.reasons ? 'settlement_check_pending')
            ORDER BY sweep.boundary_at, work.player_id
            LIMIT %s
            """,
                (batch,),
            ).fetchall()
        for player_id, boundary_at in rows:
            with connection.transaction():
                refresh_boundary(database, connection, int(player_id), boundary_at)
    return len(rows)
