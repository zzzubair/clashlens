"""Commit saved responses whose database update a worker's lock held up.

Saved responses for one player and request type commit in the order they were
saved. Once a lock holds one up, it and every later one for that player and
request type commit in the background, so no check or collection slot waits
for the worker, and no other player's responses wait behind it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import Enum
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from .collector import Collector
    from .collector_db import ResponseHandoff

# Pause between attempts; each attempt waits at most 3 s for a lock.
_RETRY_SECONDS = 2.0
# The newest saved response's turn: it resolves to None once that response
# committed or failed in its own check, or to the task committing it later.
Turn = asyncio.Future["asyncio.Task[None] | None"]
Identity = tuple[str, str, str]


class TurnState(Enum):
    """Where the saved responses for one player and request type stand.

    - ``SETTLED``: none waits. Each one saved has committed, or failed and set
      the spool's recovery required (see ``spool_health.SpoolState``).
    - ``SAVING``: the check that fetched the newest one holds its turn while it
      saves it to the spool and then commits it, or waits behind the one
      before it.
    - ``DEFERRED``: a worker's lock held up a commit, so the newest one commits
      in a background task behind the ones before it, trying every
      _RETRY_SECONDS.

    Events: a check saving a response takes the turn (``SAVING``); it commits
    at once (``SETTLED``) or hands over to a background task (``DEFERRED``);
    that task commits (``SETTLED``). A failure, a cancelled earlier commit or a
    collector stop ends the task with recovery required, which closes
    collection until a restart replays the saved responses in order;
    ``commit_unrecovered`` then queues those a lock held up during recovery as
    ``DEFERRED``. Only ``SETTLED`` lets an unchanged response skip the spool.
    ``CommitTurns`` owns the turns; they live only in this process.
    """

    SETTLED = "settled"
    SAVING = "saving"
    DEFERRED = "deferred"


def turn_state(turn: Turn | None) -> TurnState:
    if turn is None:
        return TurnState.SETTLED
    if not turn.done():
        return TurnState.SAVING
    later = turn.result()
    return TurnState.SETTLED if later is None or later.done() else TurnState.DEFERRED


class CommitTurns:
    """The newest turn per player and request type, and the background commits."""

    def __init__(self) -> None:
        # Kept only while a response waits; see forget.
        self._turns: dict[Identity, Turn] = {}
        self._later: dict[asyncio.Task[None], int | None] = {}

    def state(self, identity: Identity) -> TurnState:
        return turn_state(self._turns.get(identity))

    def take(self, identity: Identity, turn: Turn) -> Turn | None:
        """Make ``turn`` the newest; return the one it now waits behind."""
        previous = self._turns.get(identity)
        self._turns[identity] = turn
        return previous

    def held_work(self) -> list[int]:
        """Work whose saved responses still wait to commit; it is not fetched again."""
        return [work for work in self._later.values() if work is not None]

    def forget(self, identity: Identity, result: asyncio.Task[None] | None) -> None:
        """Drop a settled newest turn, so turns are kept only while responses wait."""
        turn = self._turns.get(identity)
        if (
            turn is not None
            and turn_state(turn) is TurnState.SETTLED
            and turn.result() is result
        ):
            del self._turns[identity]


def commit_later(
    collector: Collector,
    behind: asyncio.Task[None] | None,
    handoff: ResponseHandoff,
    name: str,
    serialized: bool | None = None,
) -> asyncio.Task[None]:
    collector._count("commit_deferred")
    turns = collector.commits
    task = asyncio.create_task(_retry(collector, behind, handoff, name, serialized))
    turns._later[task] = handoff.collector_work_id
    identity = (handoff.scope, handoff.identity_key, handoff.endpoint)

    def finished(done: asyncio.Task[None]) -> None:
        turns._later.pop(done, None)
        turns.forget(identity, done)

    task.add_done_callback(finished)
    return task


def commit_unrecovered(
    collector: Collector, records: list[tuple[str, ResponseHandoff, bool]]
) -> None:
    """Commit the saved responses restart recovery left, in recovery order."""
    loop = asyncio.get_running_loop()
    for name, handoff, serialized in records:
        identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
        turn: Turn = loop.create_future()
        previous = collector.commits.take(identity, turn)
        behind = None if previous is None else previous.result()
        turn.set_result(commit_later(collector, behind, handoff, name, serialized))


async def _retry(
    collector: Collector,
    behind: asyncio.Task[None] | None,
    handoff: ResponseHandoff,
    name: str,
    serialized: bool | None,
) -> None:
    try:
        if behind is not None:
            await asyncio.wait({behind})
            if behind.cancelled() or behind.exception() is not None:
                raise RuntimeError("an earlier saved response did not commit")
        while not (
            collector.spool_health.recovery_required or collector._stopping.is_set()
        ):
            try:
                await collector._commit_saved(handoff, name, serialized)
                return
            except psycopg.errors.LockNotAvailable:
                with suppress(TimeoutError):
                    await asyncio.wait_for(collector._stopping.wait(), _RETRY_SECONDS)
        raise RuntimeError("saved response left for restart recovery")
    except BaseException:
        collector.spool_health.require_recovery()
        raise
