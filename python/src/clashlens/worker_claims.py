"""Which claims one worker process holds, and whether a lane has started each.

A job leased in the database is not always running: a batch transaction
leases several at once, and each waits here until a lane frees up.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection
from enum import StrEnum
from threading import Lock
from time import monotonic
from typing import Any

from .db import Claim, LeaseLost


class ClaimStage(StrEnum):
    """Where one claim this worker process holds stands.

    ``BUFFERED``: a batch transaction leased it; it waits for a lane and
    counts against no lane yet. ``RUNNING``: a lane holds it, counting
    against its kind's lanes until the job ends.

    Events, all owned by ``ClaimLedger`` (one per worker process):

    - ``fill``: a lane finds its kind's batch empty and leases a new one,
      every claim ``BUFFERED``.
    - ``take``: a lane takes the oldest ``BUFFERED`` claim, now ``RUNNING``.
      One that waited half its lease is renewed first; if the lease was lost
      meanwhile it is dropped, as another worker or queue maintenance owns
      the job. A failed renewal for any other reason leaves it ``BUFFERED``.
    - ``start_alone``: a claim made on its own, unbatched, is ``RUNNING`` from
      before it is made; it is refused while its kind has ``BUFFERED`` claims,
      so those start first.
    - ``finish``: the job's outcome, retry or failure is now in the database;
      the process no longer holds it.
    - ``release``: on stop, lanes finish what is ``RUNNING`` and claim nothing
      more, then every ``BUFFERED`` claim is given back and is claimable at once.

    Nothing here survives a crash or restart: the database still shows the
    jobs leased, their leases run out and queue maintenance retries them.
    The same happens to ``BUFFERED`` claims whose release fails.
    """

    BUFFERED = "buffered"
    RUNNING = "running"


Kind = tuple[str, ...]


class ClaimLedger:
    """Each kind's ``BUFFERED`` claims, oldest first, and ``RUNNING`` count.

    ``lanes`` is how many lanes of each kind may run a job; a batch never
    leases more than those not ``RUNNING`` one.
    """

    def __init__(self, kinds: Collection[Kind]) -> None:
        self._buffered: dict[Kind, deque[tuple[float, Claim]]] = {
            kind: deque() for kind in kinds
        }
        self._locks = {kind: Lock() for kind in kinds}
        self._running = dict.fromkeys(kinds, 0)
        self._count_lock = Lock()
        self.lanes = dict.fromkeys(kinds, 1)

    def __contains__(self, kind: object) -> bool:
        return kind in self._buffered

    def held(self, kind: Kind) -> dict[ClaimStage, int]:
        with self._count_lock:
            running = self._running[kind]
        return {ClaimStage.BUFFERED: len(self._buffered[kind]), ClaimStage.RUNNING: running}

    def free_lanes(self, kind: Kind) -> int:
        return self.lanes[kind] - self.held(kind)[ClaimStage.RUNNING]

    def start_alone(self, kind: Kind) -> bool:
        """Count an unbatched claim ``RUNNING``, unless buffered ones wait."""
        with self._locks[kind]:
            if self.held(kind)[ClaimStage.BUFFERED]:
                return False
            self._count(kind, 1)
            return True

    def take(
        self,
        kind: Kind,
        *,
        lease_seconds: int,
        fill: Callable[[], list[Claim]],
        renew: Callable[[Claim], Any],
    ) -> Claim | None:
        """The oldest ``BUFFERED`` claim, now ``RUNNING``; None if none."""
        with self._locks[kind]:
            batch = self._buffered[kind]
            if not batch:
                claimed_at = monotonic()
                batch.extend((claimed_at, claim) for claim in fill())
            while batch:
                claimed_at, claim = batch[0]
                if monotonic() - claimed_at >= lease_seconds / 2:
                    # Slow jobs ahead of it, such as army redecodes, used
                    # half its lease: renew it before it starts, unless it
                    # is lost. It stays buffered until renewed.
                    try:
                        renew(claim)
                    except LeaseLost:
                        batch.popleft()
                        continue
                batch.popleft()
                self._count(kind, 1)
                return claim
            return None

    def finish(self, kind: Kind) -> None:
        self._count(kind, -1)

    def release(self) -> list[Claim]:
        """Every ``BUFFERED`` claim, no longer held, to give back."""
        claims: list[Claim] = []
        for kind, batch in self._buffered.items():
            with self._locks[kind]:
                claims.extend(claim for _claimed_at, claim in batch)
                batch.clear()
        return claims

    def _count(self, kind: Kind, change: int) -> None:
        with self._count_lock:
            self._running[kind] += change
