"""Run one durable collector work row: Reset, settlement, interactive, ranking, discovery or league-history refresh.

Which requests a Reset pair or settlement check makes follows its proof
(``collector_handover.PairProof``), read from its saved answers each time it
is picked.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import psycopg

from .collector_db import CollectorIntent, CollectorWork

if TYPE_CHECKING:
    from .collector import Collector

# How one run of a work row ended. capacity_paused: the spool or keys paused
# it, and window_closed: a settlement check reached 04:55 the next day; both
# leave the row as it was. retrying: a request failed and the row waits to
# retry. failed: it will not retry. profile_saved: a Reset pair's profile pass
# is done and its battle log waits for its own pass. complete: every required
# answer is saved and the row finished. incomplete: the row did not finish:
# an answer is still missing, it finished elsewhere, or the collector stopped.
IntentRun = Literal[
    "capacity_paused",
    "window_closed",
    "retrying",
    "failed",
    "profile_saved",
    "complete",
    "incomplete",
]

# Player work kept for retry when every failure was transient.
_RETRIED_INTENTS = frozenset(
    {
        "reset_baseline",
        "reset_settlement",
        "initial_collection",
        "live_refresh",
        "discovery_profile",
        "league_history_refresh",
    }
)
# Failed runs allowed outside a provider-outage pause before such work settles
# as missing, so a few failing players cannot hold ordinary collection.
_RETRIES_WHILE_ANSWERING = 3


def lane_for(intent: CollectorIntent) -> str:
    # Settlement checks require fresh, ordered answers through the Reset slots.
    if intent.kind in {"reset_baseline", "reset_settlement"}:
        return "reset"
    if intent.kind in {"initial_collection", "live_refresh"}:
        return "interactive"
    return "ordinary"


class IntentExecutor:
    def __init__(self, collector: Collector) -> None:
        self.collector = collector
        self._retries_while_answering: dict[int, int] = {}

    async def collect(self, intent: CollectorIntent) -> IntentRun:
        collector = self.collector
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
                    (
                        "battle_log",
                        intent.battle_log_required
                        and intent.kind != "discovery_profile",
                    ),
                    ("league_history", intent.league_history_required),
                )
                if required
            )
        outcomes = await collector.collect_player(
            work, lane=lane_for(intent), endpoints=endpoints
        )
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
                used = self._retries_while_answering.get(intent.work_id, 0) + 1
                self._retries_while_answering[intent.work_id] = used
                retryable = used <= _RETRIES_WHILE_ANSWERING
            status = await self._finish(
                collector.database.fail_intent,
                intent.work_id,
                category="provider_failure",
                detail="one or more required endpoint requests failed",
                retryable=retryable,
            )
            if status != "waiting_retry":
                self._retries_while_answering.pop(intent.work_id, None)
                return "failed"
            return "retrying"
        self._retries_while_answering.pop(intent.work_id, None)
        if intent.kind == "reset_baseline" and endpoints == ("profile",):
            # The profile pass: the battle log follows from its own pass.
            return "profile_saved"
        completed = await self._finish(
            collector.database.complete_intent, intent.work_id
        )
        if completed and intent.kind == "live_refresh":
            collector.refresh_latency_seconds += max(
                0.0, (datetime.now(UTC) - intent.cycle_at).total_seconds()
            )
            collector.refresh_count += 1
        return "complete" if completed else "incomplete"

    async def _finish(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        """Update the work row, retrying while a worker's lock holds it, until stopping."""
        collector = self.collector
        while not collector.stopping.is_set():
            try:
                return await collector.database_call(operation, *args, **kwargs)
            except psycopg.errors.LockNotAvailable:
                with suppress(TimeoutError):
                    await asyncio.wait_for(collector.stopping.wait(), 2.0)
        return None
