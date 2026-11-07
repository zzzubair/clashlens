"""Ordinary collection handing over to each Reset, and each Reset pair's proof.

Both states are derived from the clock and saved rows, never stored, so a
restarted collector reads the same state the stopped one left.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from . import collector_reset

# Regular checks stop this long before each Reset, at 04:55 UTC.
_CLOSE_BEFORE_RESET = timedelta(minutes=5)


class AdmissionPhase(Enum):
    """Whether regular checks may start, derived by `admission_phase`.

    OPEN: regular checks start. CLOSING: from 04:55 to the 05:00 UTC Reset no
    new one starts; those already running finish (the collector counts them in
    ``Collector.regular_inflight``). AWAITING_SWEEP: the Reset passed but its
    sweep row is not written yet; the collector writes it with
    ``collector_reset.begin_reset`` once no regular check is running.
    RESET_RUNNING: a sweep, this Reset's or an older one, still has a Reset
    pair pending or waiting to retry.

    The clock moves OPEN to CLOSING at 04:55 and CLOSING to AWAITING_SWEEP at
    05:00; the sweep row moves AWAITING_SWEEP to RESET_RUNNING; the last Reset
    pair finishing, complete or failed, moves RESET_RUNNING to OPEN. A failed
    pair request waits to retry for up to 23h55m after its Reset and holds
    regular checks meanwhile; an older sweep with an unfinished pair also
    stops ``begin_reset`` writing the next one. After a restart nothing is
    running, and ``begin_reset`` returns the sweep already written. The
    ``collector_boundary_admission`` table's phases are not used.
    """

    OPEN = "open"
    CLOSING = "closing"
    AWAITING_SWEEP = "awaiting_sweep"
    RESET_RUNNING = "reset_running"


def admission_phase(connection: Any, now: datetime) -> AdmissionPhase:
    utc_now = now.astimezone(UTC)
    boundary = utc_now.replace(hour=5, minute=0, second=0, microsecond=0)
    if boundary - _CLOSE_BEFORE_RESET <= utc_now < boundary:
        return AdmissionPhase.CLOSING
    completed_boundary = boundary if utc_now >= boundary else boundary - timedelta(days=1)
    if (
        utc_now >= boundary
        and connection.execute(
            "SELECT 1 FROM collector_reset_sweeps WHERE boundary_at = %s",
            (completed_boundary,),
        ).fetchone()
        is None
    ):
        return AdmissionPhase.AWAITING_SWEEP
    # Asked per sweep so each probe reads only that sweep's unfinished
    # rows from collector_work_sweep_order, not the whole work table.
    running = connection.execute(
        """
        SELECT 1
        FROM collector_reset_sweeps AS sweep
        WHERE sweep.boundary_at <= %s
          AND EXISTS (
              SELECT 1 FROM collector_work AS work
              WHERE work.sweep_id = sweep.id
                AND work.kind = 'reset_baseline'
                AND work.status IN ('pending', 'waiting_retry')
          )
        LIMIT 1
        """,
        (completed_boundary,),
    ).fetchone()
    return AdmissionPhase.OPEN if running is None else AdmissionPhase.RESET_RUNNING


class PairProof(Enum):
    """What a Reset pair or settlement check still needs, derived by
    `pair_proof` from the observations its work row points at.

    PROFILE_OWED: no usable profile yet (none, or not a 2xx or 404 answer);
    the profile is fetched, and the battle log with it outside the Reset
    pair's profile pass (``collector_reset.profile_pass``). LOG_OWED: a usable
    profile but no usable battle log after it: for a Reset pair, a log that
    completed before its profile did; for a settlement check, one requested
    before its profile arrived. Only the battle log is fetched. PROVEN: a
    usable profile and a usable log after it; nothing is fetched and
    ``CollectorDatabase.complete_intent`` finishes the work.

    Saving a response (``CollectorDatabase._record_intent_endpoint``, the only
    writer) moves it: a Reset pair points at its newest answers, so a newer
    profile can move PROVEN back to LOG_OWED; a settlement check keeps its
    first usable profile and the first usable log after it. A failed request
    moves nothing and the work waits to retry until 23h55m after its Reset;
    a restart reads the same rows. The worker then judges the saved pair as
    complete, partial or failed evidence (``reset_baseline_evidence.state``),
    and a later saved response records a new version of that judgement.
    """

    PROFILE_OWED = "profile_owed"
    LOG_OWED = "log_owed"
    PROVEN = "proven"


def answered(status: int | None) -> bool:
    """A usable answer: any 2xx, or 404 for a player that no longer exists."""
    return status is not None and (200 <= status < 300 or status == 404)


def pair_proof(
    kind: str,
    profile_status: int | None,
    battle_log_status: int | None,
    log_completed_before_profile: bool | None,
    log_started_before_profile: bool | None,
) -> PairProof:
    if not answered(profile_status):
        return PairProof.PROFILE_OWED
    misordered = (
        log_started_before_profile
        if kind == "reset_settlement"
        else log_completed_before_profile
    )
    if not answered(battle_log_status) or misordered:
        return PairProof.LOG_OWED
    return PairProof.PROVEN


def required_endpoints(
    kind: str,
    league_history_status: str,
    profile_status: int | None,
    battle_log_status: int | None,
    league_history_status_code: int | None,
    log_completed_before_profile: bool | None,
    log_started_before_profile: bool | None,
    boundary_at: datetime | None,
    now: datetime,
) -> tuple[bool, bool, bool]:
    """Whether one pending work row fetches its profile, battle log and
    league history now."""
    profile_pass = collector_reset.profile_pass(kind, boundary_at, now)
    if kind in {"reset_baseline", "reset_settlement"}:
        proof = pair_proof(
            kind,
            profile_status,
            battle_log_status,
            log_completed_before_profile,
            log_started_before_profile,
        )
        profile = proof is PairProof.PROFILE_OWED
        battle_log = not profile_pass and proof is not PairProof.PROVEN
    else:
        profile = kind != "league_history_refresh" and (
            kind != "discovery_profile" or not answered(profile_status)
        )
        battle_log = kind != "league_history_refresh" and not profile_pass
    league_history = (
        league_history_status != "not_applicable"
        and not answered(league_history_status_code)
        and not profile_pass
        if kind in {"reset_baseline", "discovery_profile", "league_history_refresh"}
        else league_history_status == "pending"
    )
    return profile, battle_log, league_history
