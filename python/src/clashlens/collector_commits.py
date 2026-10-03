"""Commit saved responses whose database update a worker's lock held up.

Saved responses on one lock stripe commit in the order they were saved. Once a
lock holds one up, it and every later one on its stripe commit in the
background, so no check or collection slot waits for the worker.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from .collector import Collector
    from .collector_db import ResponseHandoff

# Pause between attempts; each attempt waits at most 3 s for a lock.
_RETRY_SECONDS = 2.0
Turn = asyncio.Future["asyncio.Task[None] | None"]


def settled(turn: Turn | None) -> bool:
    """Whether every saved response up to this stripe turn has committed or failed."""
    return turn is None or (
        turn.done() and (turn.result() is None or turn.result().done())
    )


def commit_later(
    collector: Collector,
    behind: asyncio.Task[None] | None,
    handoff: ResponseHandoff,
    name: str,
    serialized: bool | None = None,
) -> asyncio.Task[None]:
    collector._count("commit_deferred")
    task = asyncio.create_task(_retry(collector, behind, handoff, name, serialized))
    collector._later_commits[task] = handoff.collector_work_id
    task.add_done_callback(lambda done: collector._later_commits.pop(done, None))
    return task


def commit_unrecovered(
    collector: Collector, records: list[tuple[str, ResponseHandoff, bool]]
) -> None:
    """Commit the saved responses restart recovery left, in recovery order."""
    loop = asyncio.get_running_loop()
    for name, handoff, serialized in records:
        lock = collector._handoff_lock(handoff)
        previous = collector._handoff_turns.get(lock)
        behind = None if previous is None else previous.result()
        turn: Turn = loop.create_future()
        turn.set_result(commit_later(collector, behind, handoff, name, serialized))
        collector._handoff_turns[lock] = turn


async def _retry(
    collector: Collector,
    behind: asyncio.Task[None] | None,
    handoff: ResponseHandoff,
    name: str,
    serialized: bool | None,
) -> None:
    if behind is not None:
        await asyncio.wait({behind})
        if behind.cancelled() or behind.exception() is not None:
            raise RuntimeError("an earlier saved response did not commit")
    while not (collector._handoff_recovery_required or collector._stopping.is_set()):
        try:
            await collector._commit_saved(handoff, name, serialized)
            return
        except psycopg.errors.LockNotAvailable:
            with suppress(TimeoutError):
                await asyncio.wait_for(collector._stopping.wait(), _RETRY_SECONDS)
        except BaseException:
            collector._handoff_recovery_required = True
            raise
    raise RuntimeError("saved response left for restart recovery")
