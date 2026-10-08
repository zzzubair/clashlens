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
conservative guards: it becomes ``settled``, with the accepted trophies and
their proof, or ``unresolved`` with the reasons it could not be proved. New
``settled`` verdicts are only admitted while
``CLASHLENS_ENABLE_NEW_RESET_PROOFS`` is on; otherwise a passing check stays
``provisional`` with its candidate proof kept for assessment. The Season
summary accepts a day's end on a settled check (``season_summaries``).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import boundary, domain, ranked_day_inputs
from .collector_reset import COLLECTION_WINDOW, SETTLEMENT_DELAY
from .db import PROCESSING_VERSION, Database
from .domain import (
    BATTLE_DAY_GRACE,
    TROPHY_ALLOCATION_RULE_VERSION,
    battle_window,
    is_season_boundary,
    ranked_day_for,
)
from .ranked_day_inputs import Reading
from .reconciliation import (
    MAX_DAILY_DEFENSES,
    BattleContribution,
    CoverageObservation,
    ReconciliationInput,
    _deduplicate_contributions,
    _zero_defense_loss,
    automatic_defense_loss,
)

PROOF_RULE_VERSION = "reset-settlement-observed-adjustment-v1"
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
# When a battle's trophies reach the profile: an attack about 4 minutes after
# its report time, a defense about 2 minutes after it ends (report time plus
# length), measured on 7 October 2026.
ATTACK_LANDING, DEFENSE_LANDING = timedelta(minutes=4), timedelta(minutes=2)
# A battle reported from this long before a reading to this long after it
# may or may not be in it.
NEAR_BEFORE, NEAR_AFTER = timedelta(minutes=6), timedelta(minutes=2)


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
    before the automatic loss, makes it ``contradicted``: both Reset
    readings can miss the same delayed credit. Anything else is
    ``unproven``.
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
    later: int | None = None

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
        """Whether the later reading, if any, is ``total`` before the
        automatic loss, or after the day's known loss."""
        return self.later is None or self.later in {total, total - self.known_loss}

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
    connection: Any, version_ids: list[int], *, later_readings: bool
) -> dict[int, DayEnd]:
    """Each saved day's ``DayEnd``, keyed by its version. With
    ``later_readings`` each also gets the latest accepted Legend I profile
    naming the day's Season read after its end Reset reading and before
    the player's first battle of the next day, by either player's report,
    within a day; ``balanced`` and ``contradicted`` need it to tell apart.
    Each step reads by an index: the 8 October 2026 board's 11,769 days
    took about 3 seconds this way, against 33 in one statement.
    """
    if not version_ids:
        return {}
    rows = connection.execute(
        """
        SELECT ranked.id, ranked.state, ranked.final_trophies_before_reset,
               ranked.automatic_defense_loss,
               ranked.automatic_defense_evidence_state,
               ranked.input_evidence ->> 'boundary_kind',
               CASE WHEN NOT ranked.failure_reasons
                             ?| ARRAY['missing_start_baseline',
                                      'start_baseline_incomplete']
                    THEN ranked.start_trophies END,
               CASE WHEN NOT ranked.failure_reasons
                             ?| ARRAY['missing_end_baseline', 'end_baseline_incomplete']
                    THEN (ranked.input_evidence ->> 'next_start_trophies')::integer
               END,
               ranked.next_start_trophies,
               ranked.input_evidence -> 'end_baseline_evidence'
                   ? 'official_final_trophies',
               settlement.selected_trophies, ranked.ranked_day_end,
               ranked.player_id, ranked.official_season_id,
               (ranked.input_evidence -> 'end_baseline_evidence'
                   -> 'profile' ->> 'observed_at')::timestamptz
        FROM ranked_day_versions AS ranked
        LEFT JOIN reset_boundary_settlements AS settlement
          ON settlement.player_id = ranked.player_id
         AND settlement.boundary_at = ranked.ranked_day_end
         AND settlement.state = 'settled'
        WHERE ranked.id = ANY(%s)
        """,
        (version_ids,),
    ).fetchall()
    later = _later_readings(connection, rows) if later_readings else {}
    return {
        int(row[0]): DayEnd(
            state=str(row[1]), final=row[2], automatic_loss=row[3],
            automatic_state=str(row[4]), boundary_kind=row[5], start=row[6],
            end_reading=row[7], next_start=row[8], official_end=bool(row[9]),
            settled=row[10], boundary_at=row[11], later=later.get(int(row[0])),
        )
        for row in rows
    }


def _later_readings(connection: Any, rows: list[Any]) -> dict[int, int]:
    """For ``day_ends``: each day's latest profile read after its end Reset
    reading and before its player's first battle of the next day."""
    days = [row for row in rows if row[14] is not None]
    if not days:
        return {}
    first_battles = {
        (int(player), boundary_at): first_at
        for player, boundary_at, first_at in connection.execute(
            """
            SELECT side.player_id, battle.ranked_day_start,
                   min(evidence.battle_timestamp)
            FROM legend_battles AS battle
            JOIN battle_evidence AS evidence ON evidence.battle_id = battle.id
            CROSS JOIN LATERAL (
                VALUES (battle.attacker_player_id), (battle.defender_player_id)
            ) AS side (player_id)
            WHERE battle.ranked_day_start = ANY(%s::timestamptz[])
            GROUP BY side.player_id, battle.ranked_day_start
            """,
            # Every player's, as the day's battles are read whole anyway:
            # filtering by thousands of players made it 8 times slower.
            (sorted({row[11] for row in days}),),
        ).fetchall()
    }
    return {
        int(version_id): int(trophies)
        for version_id, trophies in connection.execute(
            """
            SELECT day.version_id, later.trophies
            FROM unnest(
                %s::bigint[], %s::bigint[], %s::text[], %s::timestamptz[],
                %s::timestamptz[]
            ) AS day (version_id, player_id, season_id, read_at, until)
            CROSS JOIN LATERAL (
                SELECT profile.trophies
                FROM collector_observations AS observation
                JOIN player_profile_effects AS effect
                  ON effect.observation_id = observation.id
                JOIN player_profile_versions AS profile
                  ON profile.id = effect.profile_version_id
                WHERE observation.player_id = day.player_id
                  AND observation.endpoint = 'profile'
                  AND observation.response_completed_at > day.read_at
                  AND observation.response_completed_at < day.until
                  AND effect.observed_at > day.read_at
                  AND profile.source_contract_state = 'accepted'
                  AND profile.eligibility_state = 'eligible'
                  AND profile.current_league_season_id = day.season_id
                ORDER BY observation.response_completed_at DESC, observation.id DESC
                LIMIT 1
            ) AS later
            """,
            (
                [int(row[0]) for row in days],
                [int(row[12]) for row in days],
                [str(row[13]) for row in days],
                [row[14] for row in days],
                [
                    min(
                        row[11] + DAY,
                        first_battles.get((int(row[12]), row[11]), row[11] + DAY),
                    )
                    for row in days
                ],
            ),
        ).fetchall()
    }


def start_proven(data: ReconciliationInput) -> bool:
    """Whether the day's start is proven apart from its own Reset reading:
    a Season's first day starts at 5,000, and a Complete day before ending
    on that same reading proves it."""
    previous = data.previous_day
    return data.season_first_day or (
        previous is not None
        and previous.complete
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
    """
    previous = data.previous_day
    if (
        data.start_trophies is None
        and previous is not None
        and previous.complete
        and previous.late_reading_start is not None
        and previous.end_baseline_id == data.start_baseline_id
        and domain.LATE_RESET_READING == "verify"
    ):
        # A late reading the day before proved is its own start too.
        return previous.late_reading_start, 0, 0
    if (
        data.start_trophies is None
        or previous is None
        or not previous.complete
        or not (previous.unsettled_automatic_loss or previous.reset_reading_correction)
        or previous.end_baseline_id is None
        or previous.end_baseline_id != data.start_baseline_id
    ):
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
    return EndReading(
        automatic_loss, automatic_state, charged, expected_next, next_start,
        residual, unsettled_loss, reading_correction, battles_after_reading,
    )


@dataclass(frozen=True, slots=True)
class LateEnd:
    """What a late end Reset reading proves (see ``settle_late_reading``):
    ``end`` as an on-time reading would, or ``None`` when the day ends on its
    calculated end; ``evidence`` is saved with the day."""

    end: EndReading | None
    evidence: dict[str, Any]


def new_day_change(
    battles: tuple[BattleContribution, ...], read_at: datetime, *, by_landing: bool
) -> int:
    """The trophy change of ``battles`` already in a reading taken at
    ``read_at``: those landed before it, or reported before it."""

    def counted(battle: BattleContribution) -> bool:
        assert battle.battle_timestamp is not None
        at = battle.battle_timestamp
        if by_landing:
            at += ATTACK_LANDING if battle.lens == "offense" else (
                timedelta(seconds=battle.battle_seconds or 0) + DEFENSE_LANDING)
        return at < read_at

    return sum((battle.amount or 0) * (1 if battle.lens == "offense" else -1)
               for battle in battles if counted(battle))


def settle_late_reading(
    data: ReconciliationInput,
    contributions: tuple[BattleContribution, ...],
    **settle: Any,
) -> LateEnd | None:
    """The ended day's end from a Reset reading taken after the new day's
    first battle, or ``None`` to leave the day without it.

    The reading holds the new day's battles that had landed. Less those, by
    landing time or by report time, it is read as an on-time reading
    (``settle_end_reading``): matching the day's calculated next start, it
    verifies the end and starts the next day. Otherwise a battle of either
    day reported within ``NEAR_BEFORE`` before it or ``NEAR_AFTER`` after
    it, or new-day battles that cannot be judged, leave the reading unable
    to judge the day, which ends on its calculated end; anything else
    contradicts it. On 6 October 2026, 3,623 ended days lacked only this
    reading; landing verified about 92.8%. A day with no used defense slot,
    whose automatic loss only a reading shows, never ends on a calculated
    end. ``settle`` is ``settle_end_reading``'s keywords; ``clean`` ignores
    the missing reading.
    """
    late = data.late_end_reading
    if late is None or domain.LATE_RESET_READING != "verify" or not settle["clean"]:
        return None
    if settle["end_hidden_by_reset"] or "official_final_trophies" in data.end_baseline_evidence:
        return None
    # Battles after the reading cannot be in it, disputed or not.
    relevant = tuple(
        battle for battle in late.new_day_contributions
        if battle.battle_timestamp is None or battle.battle_timestamp < late.read_at + NEAR_AFTER
    )
    new_day, _, reasons, malformed, inconsistent = _deduplicate_contributions(relevant)
    unclear = bool(
        reasons or malformed or inconsistent or not late.log_after_reading
        or any(battle.battle_timestamp is None for battle in new_day)
    )
    near = any(
        battle.battle_timestamp is None
        or late.read_at - NEAR_BEFORE <= battle.battle_timestamp <= late.read_at + NEAR_AFTER
        for battle in (*contributions, *new_day)
    )
    evidence: dict[str, Any] = {
        "reading_trophies": late.trophies,
        "read_at": late.read_at.isoformat(),
        "new_day_battles": [
            {"battle_identity": battle.battle_identity, "lens": battle.lens,
             "trophies": battle.amount,
             "battle_timestamp": _iso(battle.battle_timestamp),
             "battle_seconds": battle.battle_seconds}
            for battle in new_day
        ],
    }
    first = None
    for basis, by_landing in (("landing", True), ("report_time", False)):
        if unclear:
            break
        change = new_day_change(new_day, late.read_at, by_landing=by_landing)
        end = settle_end_reading(
            replace(data, next_start_trophies=late.trophies - change), contributions, **settle
        )
        evidence[f"{basis}_change"] = change
        if end.residual == 0:
            return LateEnd(end, {**evidence, "outcome": "verified", "basis": basis})
        first = first or end
    if not (unclear or near):
        assert first is not None
        return LateEnd(first, {**evidence, "outcome": "contradicted"})
    if not settle["defense_count"] + data.zero_result_defense_slots:
        return None
    return LateEnd(None, {**evidence, "outcome": "calculated_not_reset_verified",
                          "unclear": unclear})


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
    # Every saved own-side report of the ended day and the day before.
    battles: tuple[BattleContribution, ...] = ()
    # Report times of unreadable rows in battle logs saved after the named one.
    late_unreadable: tuple[datetime | None, ...] = ()
    # Earliest report time, from either player, at or after the early reading.
    first_report_after_early: datetime | None = None
    first_new_day_report: datetime | None = None
    # Every saved own-side report of the new day.
    new_day_battles: tuple[BattleContribution, ...] = ()
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
    calculated automatic loss, on top of an independently settled previous
    Reset plus every battle of the ended day, can be ``settled``. Anything
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

    # 3. Complete battle evidence for the ended day and the day before,
    # from the named battle log, unchanged by every report saved since.
    ended_from, ended_until = battle_window(boundary - DAY)
    prior_from = battle_window(boundary - 2 * DAY)[0]
    coverage = inputs.log_coverage
    if (coverage is None or not coverage.valid or coverage.has_row_gap
            or coverage.malformed_row_count or coverage.unclassified_row_count
            or len(set(coverage.battle_identities)) != len(coverage.battle_identities)):
        reasons.append("battle_log_unreadable")
    if not inputs.log_reports or min(r[2] for r in inputs.log_reports) >= prior_from:
        reasons.append("battle_log_too_short")
    pinned = {
        identity: (lens, amount)
        for identity, lens, at, amount in inputs.log_reports
        if prior_from <= at < ended_until
    }
    for battle in inputs.battles:
        if (not battle.valid or battle.failure_reason or battle.disagreement
                or battle.opponent_tag is None or battle.amount is None):
            reasons.append("battle_report_unusable")
        if battle.source_rule_version != TROPHY_ALLOCATION_RULE_VERSION:
            reasons.append("rule_correction_pending")
    if {b.battle_identity: (b.lens, b.amount) for b in inputs.battles} != pinned:
        reasons.append("battle_reports_changed_after_log")
    if any(at is None or prior_from <= at < ended_until for at in inputs.late_unreadable):
        reasons.append("late_battle_log_unreadable")
    def amounts(lens: str, since: datetime, until: datetime) -> list[int]:
        return [a for _, kind, at, a in inputs.log_reports if kind == lens and since <= at < until]

    attacks = amounts("offense", ended_from, ended_until)
    defenses = amounts("defense", ended_from, ended_until)
    prior_defenses = amounts("defense", prior_from, ended_from)
    if max(len(attacks), len(defenses), len(prior_defenses)) > MAX_DAILY_DEFENSES:
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
    # A new-day battle before a reading is in it once landed, and comes off
    # it as in ``settle_late_reading``; only one that cannot be judged
    # refuses the check.
    subtract = domain.LATE_RESET_READING == "verify"
    new_day_from = battle_window(boundary)[0]
    if subtract:
        battles, _, unclear, malformed, disputed = _deduplicate_contributions(
            inputs.new_day_battles)
        if unclear or malformed or disputed or any(
            battle.battle_timestamp is None for battle in battles
        ):
            reasons.append("new_day_battles_unclear")
    for first, reason in (
        (None if subtract else inputs.first_new_day_report, "new_day_battle_before_profile"),
        (inputs.first_report_after_early, "battle_between_readings"),
    ):
        if first is not None and first <= profile.response_completed_at and not (
            subtract and first >= new_day_from
        ):
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

    # 5 and 6. An independently settled previous Reset, plus the ended
    # day's battles and its positive automatic loss, gives the target; the
    # early reading must sit exactly that loss above it, and the named
    # profile exactly on it.
    zero_attacks, zero_defenses = ranked_day_inputs.slot_counts(
        inputs.zero_result_slots, ended_from, ended_until)
    zero_prior_defenses = ranked_day_inputs.slot_counts(
        inputs.zero_result_slots, prior_from, ended_from)[1]
    automatic = None
    if 1 <= len(defenses) + zero_defenses < MAX_DAILY_DEFENSES:
        automatic = automatic_defense_loss(
            attacks=len(attacks) + zero_attacks,
            defenses=len(defenses) + zero_defenses,
            defense_loss=sum(defenses),
            previous_defenses=len(prior_defenses) + zero_prior_defenses,
            previous_defense_loss=sum(prior_defenses),
            season_first_day=is_season_boundary(boundary - DAY),
        )
    proof["automatic_loss_basis"] = {
        "prior_defenses": len(prior_defenses), "prior_defense_loss": sum(prior_defenses),
        "defenses": len(defenses), "defense_loss": sum(defenses), "automatic_loss": automatic,
        # Only present when the log holds such rows, so other proofs are unchanged.
        **{name: count for name, count in (
            ("zero_result_attacks", zero_attacks), ("zero_result_defenses", zero_defenses),
            ("zero_result_prior_defenses", zero_prior_defenses)) if count},
    }
    root = inputs.root
    if root is None:
        reasons.append("independent_root_missing")
    elif root.boundary_at >= boundary or set(root.observations) & set(proof["observations"]):
        reasons.append("independent_root_circular")
    if not automatic:
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
    early_shown = _shown(early, inputs, target + automatic)
    profile_shown = _shown(profile, inputs, target)
    if early_shown != target + automatic:
        reasons.append("early_reading_mismatch")
    if early_shown - profile_shown != automatic:
        reasons.append("observed_drop_mismatch")
    if profile_shown != target:
        reasons.append("profile_catchup_unknown")
    if reasons:
        return verdict(UNRESOLVED, reasons)
    return verdict(SETTLED, [], target)


def _shown(reading: Reading, inputs: ProofInputs, expected: int) -> int:
    """The reading's trophies less the new-day battles already in it: by
    landing time, or by report time when only that gives ``expected``."""
    assert reading.trophies is not None
    if domain.LATE_RESET_READING != "verify" or not inputs.new_day_battles:
        return reading.trophies
    battles = _deduplicate_contributions(inputs.new_day_battles)[0]
    shown = [
        reading.trophies - new_day_change(
            battles, reading.response_completed_at, by_landing=by_landing)
        for by_landing in (True, False)
    ]
    return expected if expected in shown else shown[0]


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
    return replace(
        inputs,
        log_coverage=coverage,
        log_reports=ranked_day_inputs.load_log_reports(
            database, connection, log.observation_id, log.parser_version),
        zero_result_slots=ranked_day_inputs.load_zero_result_slots(
            connection, (coverage,) if coverage else ()),
        battles=tuple(battle for day in (boundary_at - 2 * DAY, boundary_at - DAY)
                      for battle in ranked_day_inputs.load_contributions(
                          connection, player_id, ranked_day_for(day))),
        late_unreadable=tuple(ranked_day_inputs.load_unreadable_report_times(
            database, connection, player_id, log_observed_at, boundary_at + DAY
        )),
        first_report_after_early=reports[0],
        first_new_day_report=reports[1],
        new_day_battles=ranked_day_inputs.load_contributions(
            connection, player_id, ranked_day_for(boundary_at)),
        later_profiles=later_profiles,
        root=Root(root[0], int(root[1]), str(root[2]), int(root[3]),
                  tuple(int(value) for value in root[4])) if root else None,
    )


def refresh_boundary(
    database: Database, connection: Any, player_id: int, boundary_at: datetime, *, depth: int = 0
) -> None:
    """Re-judge one Reset and record a changed verdict (guard 7).

    Runs in the caller's transaction, under the Reset's own lock, so the
    inputs are re-read after any concurrent writer finished. A finalized
    Season keeps its verdict. Admitting a new ``settled`` verdict needs the
    switch; losing one never does. A change to a settled verdict re-judges
    the next Reset, whose target it roots.
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
    if SETTLED in (current[0], verdict.state) and depth < MAX_CASCADE:
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
    """Re-judge the Resets a newly processed response can change.

    A named check's own responses always count. Any other response of the
    player, or of an opponent in its battles, from up to three days after a
    Reset, re-judges only a finished check that is settled, passed every
    guard, or met a later profile that disagreed or was not processed. A
    battle log also re-judges a finished check with an unusable report at
    any Reset that can read one of its battles. Later evidence can only
    take proof away from the rest; they are re-judged in full when the
    previous Reset's verdict changes. Every Reset it may re-judge is locked
    before choosing, so a concurrent judgment is chosen from once committed.
    """
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
    """Judge up to ``batch`` finished checks that no processed response will.

    A check that failed or expired before saving both responses has nothing
    for the worker to process, so the maintenance timer judges it here.
    Resets in a finalized Season keep their verdict and are skipped. Returns
    how many it judged.
    """
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
