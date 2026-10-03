"""Whether a player's Reset trophies are known to have settled.

The game can apply the previous day's automatic defense loss minutes after
the 05:00 UTC Reset, so the profile read at the Reset is only provisional.
Each Reset starts as ``provisional``. Once its delayed settlement check has
finished, the check is judged against seven conservative guards: it becomes
``settled``, with the accepted trophies and their proof, or ``unresolved``
with the reasons it could not be proved. New ``settled`` verdicts are only
admitted while ``CLASHLENS_ENABLE_NEW_RESET_PROOFS`` is on; otherwise a
passing check stays ``provisional`` with its candidate proof kept for
assessment. Nothing reads this state yet, so day results are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb

from . import ranked_day_inputs
from .collector_reset import COLLECTION_WINDOW, SETTLEMENT_DELAY
from .db import PROCESSING_VERSION, Database
from .domain import (
    TROPHY_ALLOCATION_RULE_VERSION,
    battle_window,
    is_season_boundary,
    ranked_day_for,
)
from .ranked_day_inputs import Reading
from .reconciliation import MAX_DAILY_DEFENSES, BattleContribution, CoverageObservation

PROOF_RULE_VERSION = "reset-settlement-observed-adjustment-v1"
NEW_PROOFS_SWITCH = "CLASHLENS_ENABLE_NEW_RESET_PROOFS"
PROVISIONAL, SETTLED, UNRESOLVED = "provisional", "settled", "unresolved"
TERMINAL_WORK = frozenset({"complete", "failed", "cancelled"})
DAY = timedelta(days=1)
# The longest a chain of settled Resets is rechecked after one changes.
MAX_CASCADE = 28


def record_provisional_boundary(
    connection: Any,
    *,
    player_id: int,
    boundary_at: datetime,
    sweep_id: int,
    early_baseline_id: int,
    early_state: str,
    reasons: list[str],
    observation_id: int | None,
) -> None:
    """Record the Reset pair evidence of a still-provisional boundary.

    The pair's state and failure reasons are kept as they are: a complete
    pair proves the responses were processed, not that trophies settled. A
    repeat with the same evidence changes nothing, and a boundary already
    settled or unresolved keeps that verdict. A boundary with a settlement
    check keeps the check's reasons; the pair's own are in its proof. It
    first locks this Reset and every one ``observation_id`` can re-judge.
    """
    early = {"baseline_id": early_baseline_id, "state": early_state, "reasons": reasons}
    _lock_resets(connection, [(player_id, boundary_at),
                              *_observation_resets(connection, observation_id)])
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
    # Every saved own-side report of the ended day and the day before.
    battles: tuple[BattleContribution, ...] = ()
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

    # 5 and 6. An independently settled previous Reset, plus the ended
    # day's battles and its positive automatic loss, gives the target; the
    # early reading must sit exactly that loss above it, and the named
    # profile exactly on it.
    automatic = None
    if 1 <= len(defenses) < MAX_DAILY_DEFENSES:
        automatic = (sum(prior_defenses) + sum(defenses)) // (
            len(prior_defenses) + len(defenses)
        ) * (MAX_DAILY_DEFENSES - len(defenses))
    proof["automatic_loss_basis"] = {
        "prior_defenses": len(prior_defenses), "prior_defense_loss": sum(prior_defenses),
        "defenses": len(defenses), "defense_loss": sum(defenses), "automatic_loss": automatic,
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
        battle_window(boundary_at)[0], boundary_at + DAY,
    )
    quiet_until = min(reports[1] or boundary_at + DAY, boundary_at + DAY)
    later_profiles = ranked_day_inputs.load_profile_trophies(
        database, connection, player_id, profile.response_completed_at, quiet_until
    )
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
            database, connection, log.observation_id, log.parser_version
        ),
        battles=tuple(
            battle
            for day in (boundary_at - 2 * DAY, boundary_at - DAY)
            for battle in ranked_day_inputs.load_contributions(
                connection, player_id, ranked_day_for(day)
            )
        ),
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
    database: Database,
    connection: Any,
    player_id: int,
    boundary_at: datetime,
    *,
    depth: int = 0,
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


def _lock_resets(connection: Any, resets: list[tuple[int, datetime]]) -> None:
    """Lock Resets oldest first, after any publication lock, so jobs never
    wait on each other's Resets in opposite orders."""
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


def refresh_for_observation(
    database: Database, connection: Any, observation_id: int
) -> None:
    """Re-judge the Resets a newly processed response can change.

    A named check's own responses always count. Any other response of the
    player, or of an opponent in its battles, from up to three days after a
    Reset, re-judges only a Reset whose check finished and that is settled,
    passed every guard, or met a later profile that disagreed or was not
    processed: later evidence can
    only take proof away from the rest, and they are re-judged in full when
    the previous Reset's verdict changes.
    """
    if not _has_settlements(database, connection):
        return
    rows = _observation_resets(connection, observation_id)
    _lock_resets(connection, rows)
    for player_id, boundary_at in rows:
        refresh_boundary(database, connection, player_id, boundary_at)


def _observation_resets(connection: Any, observation_id: int | None) -> list[tuple[int, datetime]]:
    if observation_id is None:
        return []
    rows = connection.execute(
        """
        WITH observed AS (
            SELECT id, player_id, response_completed_at AS at
            FROM collector_observations WHERE id = %s
        ), affected AS (
            SELECT player_id FROM observed WHERE player_id IS NOT NULL
            UNION
            SELECT CASE WHEN battle.attacker_player_id = observed.player_id
                        THEN battle.defender_player_id
                        ELSE battle.attacker_player_id END
            FROM observed
            JOIN battle_evidence AS evidence ON evidence.observation_id = observed.id
            JOIN legend_battles AS battle ON battle.id = evidence.battle_id
        )
        SELECT settlement.player_id, settlement.boundary_at
        FROM observed
        CROSS JOIN affected
        JOIN reset_boundary_settlements AS settlement
          ON settlement.player_id = affected.player_id
         AND settlement.boundary_at <= observed.at
         AND settlement.boundary_at > observed.at - interval '3 days'
        JOIN collector_work AS work ON work.id = settlement.delayed_work_id
        WHERE observed.id IN (work.profile_observation_id, work.battle_log_observation_id)
           OR (work.status IN ('complete', 'failed', 'cancelled') AND (
                  settlement.state = 'settled'
                  OR settlement.reasons <@ '["new_reset_proofs_disabled"]'::jsonb
                  OR settlement.reasons ?| array['later_profile_contradicts', 'later_profile_unprocessed']))
        ORDER BY settlement.boundary_at, settlement.player_id
        """,
        (observation_id,),
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
