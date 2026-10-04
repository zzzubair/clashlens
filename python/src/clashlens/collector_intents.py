"""Run one durable collector work row: Reset, settlement, interactive, ranking or discovery."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psycopg

from .collector_db import CollectorIntent, CollectorWork

if TYPE_CHECKING:
    from .collector import Collector

# Player work kept for retry when every failure was transient.
_RETRIED_INTENTS = frozenset(
    {"reset_baseline", "reset_settlement", "initial_collection", "live_refresh"}
)
# Failed runs allowed outside a provider-outage pause before such work settles
# as missing, so a few failing players cannot hold ordinary collection.
_RETRIES_WHILE_ANSWERING = 3


async def collect_intent(collector: Collector, intent: CollectorIntent) -> str:
    if intent.work_id is None:
        raise ValueError("collector intent has no durable work row")
    if intent.kind == "global_player_rankings":
        work = CollectorWork(
            None,
            "global",
            intent.due_at or intent.cycle_at,
            collector_work_id=intent.work_id,
        )
        endpoints = ("global_player_rankings",)
        lane = "ordinary"
    else:
        if intent.player_id is None or intent.normalized_tag is None:
            raise ValueError("player collector intent has no player identity")
        work = CollectorWork(
            intent.player_id,
            intent.normalized_tag,
            intent.due_at or intent.cycle_at,
            collector_work_id=intent.work_id,
            eligibility_recheck=intent.eligibility_recheck,
            collect_before=intent.collect_before,
        )
        endpoints = tuple(
            endpoint
            for endpoint, required in (
                ("profile", intent.profile_required),
                ("battle_log", intent.battle_log_required and intent.kind != "discovery_profile"),
                ("league_history", intent.league_history_required),
            )
            if required
        )
        # A settlement check takes the Reset path: a fresh profile, saved,
        # then the battle log, never a reused profile or a skipped log. It
        # still waits behind Reset work in the ordinary intent slots.
        lane = (
            "reset"
            if intent.kind in {"reset_baseline", "reset_settlement"}
            else (
                "interactive"
                if intent.kind in {"initial_collection", "live_refresh"}
                else "ordinary"
            )
        )
    outcomes = await collector.collect_player(work, lane=lane, endpoints=endpoints)
    if "capacity_paused" in outcomes:
        return "capacity_paused"
    if "window_closed" in outcomes:
        return "window_closed"
    if outcomes != ["recorded"] * len(endpoints):
        # A provider outage must not become a permanent player failure,
        # but once the API answers again a few retries are enough.
        retryable = "failed" not in outcomes and intent.kind in _RETRIED_INTENTS
        outage = getattr(collector.client, "provider_outage", None)
        if retryable and not getattr(outage, "active", False):
            used = collector._retries_while_answering.get(intent.work_id, 0) + 1
            collector._retries_while_answering[intent.work_id] = used
            retryable = used <= _RETRIES_WHILE_ANSWERING
        status = await _finish(
            collector,
            collector.database.fail_intent,
            intent.work_id,
            category="provider_failure",
            detail="one or more required endpoint requests failed",
            retryable=retryable,
        )
        if status != "waiting_retry":
            collector._retries_while_answering.pop(intent.work_id, None)
            return "failed"
        return "retrying"
    collector._retries_while_answering.pop(intent.work_id, None)
    completed = await _finish(
        collector, collector.database.complete_intent, intent.work_id
    )
    if completed and intent.kind == "live_refresh":
        collector.refresh_latency_seconds += max(
            0.0, (datetime.now(UTC) - intent.cycle_at).total_seconds()
        )
        collector.refresh_count += 1
    return "complete" if completed else "incomplete"


async def _finish(
    collector: Collector, operation: Any, *args: Any, **kwargs: Any
) -> Any:
    """Update the work row, retrying while a worker's lock holds it, until stopping."""
    while not collector._stopping.is_set():
        try:
            return await collector._database_call(operation, *args, **kwargs)
        except psycopg.errors.LockNotAvailable:
            with suppress(TimeoutError):
                await asyncio.wait_for(collector._stopping.wait(), 2.0)
    return None
