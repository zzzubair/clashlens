"""Own durable response publication, ordered commits and restart recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from functools import partial
from typing import Any

import psycopg

from .async_runtime import drain_awaitable, drain_to_thread
from .collector_db import ResponseHandoff
from .spool import SpoolError

_HANDOFF_LOCK_STRIPES = 4096
_HANDOFF_PROTOCOL = 2


class HandoffRecoveryRequired(RuntimeError):
    pass


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
    recovery then queues those a lock held up during recovery as
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
    """The newest turn per player and request type."""

    def __init__(self) -> None:
        # Kept only while a response waits; see forget.
        self._turns: dict[Identity, Turn] = {}

    def state(self, identity: Identity) -> TurnState:
        return turn_state(self._turns.get(identity))

    def take(self, identity: Identity, turn: Turn) -> Turn | None:
        """Make ``turn`` the newest; return the one it now waits behind."""
        previous = self._turns.get(identity)
        self._turns[identity] = turn
        return previous

    def forget(self, identity: Identity, result: asyncio.Task[None] | None) -> None:
        """Drop a settled newest turn, so turns are kept only while responses wait."""
        turn = self._turns.get(identity)
        if (
            turn is not None
            and turn_state(turn) is TurnState.SETTLED
            and turn.result() is result
        ):
            del self._turns[identity]


def serialize_handoff(handoff: ResponseHandoff) -> tuple[str, bytes]:
    payload = asdict(handoff)
    payload["handoff_protocol"] = _HANDOFF_PROTOCOL
    payload["request_started_at"] = handoff.request_started_at.isoformat()
    payload["response_completed_at"] = handoff.response_completed_at.isoformat()
    return handoff.occurrence_key, json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def deserialize_handoff(payload: bytes) -> tuple[ResponseHandoff, bool]:
    value = json.loads(payload)
    protocol = value.pop("handoff_protocol", None)
    if protocol not in {None, _HANDOFF_PROTOCOL}:
        raise ValueError("unsupported response handoff protocol")
    value["request_started_at"] = datetime.fromisoformat(value["request_started_at"])
    value["response_completed_at"] = datetime.fromisoformat(
        value["response_completed_at"]
    )
    # Sidecars written before field compaction replay with the raw digest:
    # they count as changed once, which stores rather than loses them.
    value.setdefault("content_fingerprint", value["response_hash"])
    return ResponseHandoff(**value), protocol == _HANDOFF_PROTOCOL


class SavedResponses:
    def __init__(
        self,
        *,
        database: Any,
        spool: Any,
        spool_health: Any,
        database_call: Any,
        count: Any,
    ) -> None:
        self.database = database
        self.spool = spool
        self.spool_health = spool_health
        self.database_call = database_call
        self._count = count
        self._handoff_locks = tuple(
            asyncio.Lock() for _ in range(_HANDOFF_LOCK_STRIPES)
        )
        self._turns = CommitTurns()
        self._later: dict[asyncio.Task[None], int | None] = {}
        self._unrecovered: list[tuple[str, ResponseHandoff, bool]] = []
        # Newest committed time and field fingerprint, from this process only;
        # empty after restart so the first new sighting always reaches the spool.
        self._committed: dict[Identity, tuple[datetime, str]] = {}
        self._stopping = asyncio.Event()

    def held_work(self) -> list[int]:
        return [work for work in self._later.values() if work is not None]

    async def stop(self) -> None:
        self._stopping.set()
        await asyncio.gather(*self._later, return_exceptions=True)

    async def recover(self) -> int:
        recovered = await drain_to_thread(self._recover)
        self._resume()
        return recovered

    async def save(
        self,
        handoff: ResponseHandoff,
        body: bytes,
        reservation: Any,
        *,
        allow_compaction: bool,
    ) -> bool:
        name, payload = serialize_handoff(handoff)
        # Only a known-unchanged sighting may skip the spool: an
        # ordinary, work-free response whose used fields match the ones
        # this process last committed for the same endpoint, checked
        # while no response for its lock stripe is in flight. A crash
        # before its database commit loses only that sighting; the next
        # poll records it again. The check holds no lock, so no other
        # response waits behind it. Every other response (first since
        # restart, changed, reset or work-bound) is saved to the spool
        # before its own database work, as is a known-unchanged one
        # the check does not compact.
        identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
        lock = self._handoff_lock(handoff)
        committed = self._committed.get(identity)
        cancelled = False
        compacted = False
        if (
            allow_compaction
            and handoff.collector_work_id is None
            and committed is not None
            and committed[1] == handoff.content_fingerprint
            and not lock.locked()
            and self._turns.state(identity) is TurnState.SETTLED
            and not self.spool_health.recovery_required
        ):
            check = asyncio.ensure_future(
                asyncio.to_thread(self.database.record_unchanged_response, handoff)
            )
            try:
                await drain_awaitable(check)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:  # noqa: BLE001, S110 - the spool handoff keeps it.
                pass
            compacted = (
                not check.cancelled()
                and check.exception() is None
                and check.result() is True
            )
        seen = (handoff.response_completed_at, handoff.content_fingerprint)
        if compacted:
            self._committed[identity] = max(seen, self._committed.get(identity) or seen)
        else:
            published = False
            later: asyncio.Task[None] | None = None
            turn = asyncio.get_running_loop().create_future()
            try:
                async with lock:
                    # A predecessor may have failed after its durable
                    # publish while this response was waiting for the
                    # same stripe. Recovery must run before any
                    # successor can become current.
                    if self.spool_health.recovery_required:
                        if cancelled:
                            raise asyncio.CancelledError
                        return False
                    await drain_to_thread(
                        self.spool.publish_handoff,
                        body,
                        handoff.response_hash,
                        name,
                        payload,
                        reservation,
                    )
                    published = True
                    previous = self._turns.take(identity, turn)
                # Saved responses commit in publish order, but no
                # database wait holds the lock, so a later response
                # always reaches the spool first.
                behind = None if previous is None else await asyncio.shield(previous)
                if self.spool_health.recovery_required:
                    if cancelled:
                        raise asyncio.CancelledError
                    return False
                committed_now = False
                if behind is None or behind.done():
                    with suppress(psycopg.errors.LockNotAvailable):
                        await self._commit_saved(handoff, name)
                        committed_now = True
                if not committed_now:
                    later = self._commit_later(behind, handoff, name)
            except BaseException as error:
                if published or self._sidecar_exists(name):
                    self.spool_health.require_recovery()
                    if isinstance(error, (OSError, SpoolError)):
                        raise HandoffRecoveryRequired(
                            "durable response handoff requires restart recovery"
                        ) from error
                raise
            finally:
                turn.set_result(later)
                self._turns.forget(identity, later)
            if later is not None and handoff.collector_work_id is not None:
                await asyncio.wait({later})
                if not cancelled and (later.cancelled() or later.exception()):
                    return False
        if cancelled:
            raise asyncio.CancelledError
        return True

    async def _commit_saved(
        self, handoff: ResponseHandoff, name: str, serialized: bool | None = None
    ) -> None:
        record = (
            self.database.record_response
            if serialized is None
            else partial(self.database.record_recovered_response, serialized=serialized)
        )
        await drain_awaitable(self.database_call(record, handoff))
        identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
        seen = (handoff.response_completed_at, handoff.content_fingerprint)
        self._committed[identity] = max(seen, self._committed.get(identity) or seen)
        await drain_to_thread(self.spool.remove_handoff, name)

    def _handoff_lock(self, handoff: ResponseHandoff) -> asyncio.Lock:
        identity = (
            f"{handoff.scope}\0{handoff.identity_key}\0{handoff.endpoint}"
        ).encode()
        stripe = int.from_bytes(hashlib.sha256(identity).digest()[:4], "big")
        return self._handoff_locks[stripe % len(self._handoff_locks)]

    def _sidecar_exists(self, name: str) -> bool:
        try:
            return any(item_name == name for item_name, _ in self.spool.iter_handoffs())
        except BaseException as error:
            self.spool_health.require_recovery()
            raise HandoffRecoveryRequired(
                "response handoff state cannot be inspected safely"
            ) from error

    def _recover(self) -> int:
        records = [
            (name, *deserialize_handoff(payload))
            for name, payload in self.spool.iter_handoffs()
        ]
        # Responses already applied go first, then the rest in the order they
        # were received, so a later one never compacts away an earlier change
        # or replaces the commit marker of one already applied.
        applied = (
            self.database.applied_occurrence_keys(
                [handoff.occurrence_key for _name, handoff, _serialized in records]
            )
            if records
            else set()
        )
        records.sort(
            key=lambda record: (
                record[1].occurrence_key not in applied,
                record[1].response_completed_at,
            )
        )
        for _name, handoff, _serialized in records:
            if self.spool.verify(handoff.response_hash, handoff.byte_size) is None:
                raise SpoolError("handoff raw response is missing or corrupt")
        recovered = 0
        for index, (name, handoff, serialized) in enumerate(records):
            try:
                self.database.record_recovered_response(handoff, serialized=serialized)
            except psycopg.errors.LockNotAvailable:
                self._unrecovered = records[index:]
                break
            self.spool.remove_handoff(name)
            recovered += 1
        self.sweep_unreferenced()
        return recovered

    def sweep_unreferenced(self) -> None:
        try:
            self.spool.remove_unreferenced(self.database.referenced_spool_hashes)
        except psycopg.errors.QueryCanceled:  # over its time limit; next sweep
            self._count("spool_sweep_timeout")

    def _commit_later(
        self,
        behind: asyncio.Task[None] | None,
        handoff: ResponseHandoff,
        name: str,
        serialized: bool | None = None,
    ) -> asyncio.Task[None]:
        self._count("commit_deferred")
        task = asyncio.create_task(self._retry(behind, handoff, name, serialized))
        self._later[task] = handoff.collector_work_id
        identity = (handoff.scope, handoff.identity_key, handoff.endpoint)

        def finished(done: asyncio.Task[None]) -> None:
            self._later.pop(done, None)
            self._turns.forget(identity, done)

        task.add_done_callback(finished)
        return task

    def _resume(self) -> None:
        """Commit the saved responses restart recovery left, in recovery order."""
        loop = asyncio.get_running_loop()
        records, self._unrecovered = self._unrecovered, []
        for name, handoff, serialized in records:
            identity = (handoff.scope, handoff.identity_key, handoff.endpoint)
            turn: Turn = loop.create_future()
            previous = self._turns.take(identity, turn)
            behind = None if previous is None else previous.result()
            turn.set_result(self._commit_later(behind, handoff, name, serialized))

    async def _retry(
        self,
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
            while not (self.spool_health.recovery_required or self._stopping.is_set()):
                try:
                    await self._commit_saved(handoff, name, serialized)
                    return
                except psycopg.errors.LockNotAvailable:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), _RETRY_SECONDS)
            raise RuntimeError("saved response left for restart recovery")
        except BaseException:
            self.spool_health.require_recovery()
            raise
