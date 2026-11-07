"""Which worker process runs the shared publication maintenance.

Only one worker process at a time runs the Reset publication checks, the
correction sweep and the army rank-band count, so two processes neither
repeat that work nor race on it. The process whose own connection holds
this lock runs them until it stops. Queue maintenance is cheap and safe to
repeat, so every process runs it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import psycopg
from psycopg.errors import Error

MAINTENANCE_PERMIT_KEY = "worker-publication-maintenance"


class PermitState(StrEnum):
    """Where one worker process stands with the maintenance permit.

    ``DISCONNECTED``: no connection of its own, at start and after any
    database error or ``close``. ``STANDBY``: connected, and another process
    holds the lock. ``HELD``: this process's connection holds the lock.

    ``MaintenancePermit.acquire`` is the only event that moves it forward,
    each time maintenance is due: it connects when ``DISCONNECTED``, tries
    the lock when ``STANDBY``, and checks the connection is alive when
    ``HELD``, since the lock lasts as long as its connection does. Any
    database error, a lost connection among them, closes the connection
    and moves it to ``DISCONNECTED``; the next ``acquire`` starts over. On a
    stop or crash the server ends the session and frees the lock, a moment
    later, for another process's next ``acquire``.
    """

    DISCONNECTED = "disconnected"
    STANDBY = "standby"
    HELD = "held"


class MaintenancePermit:
    """A session lock the one maintaining process holds on its own connection."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        # Open exactly while not ``DISCONNECTED``.
        self.connection: psycopg.Connection[Any] | None = None
        self.state = PermitState.DISCONNECTED

    @property
    def held(self) -> bool:
        return self.state is PermitState.HELD

    def acquire(self) -> bool:
        """Whether this process holds the permit, taking it when it is free."""
        try:
            if self.connection is None:
                self.connection = psycopg.connect(
                    self.conninfo, autocommit=True, connect_timeout=10
                )
                self.state = PermitState.STANDBY
            if self.state is PermitState.HELD:
                self.connection.execute("SELECT 1")
            elif self.connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                (MAINTENANCE_PERMIT_KEY,),
            ).fetchone()[0]:
                self.state = PermitState.HELD
        except Error:
            self.close()
        return self.held

    def close(self) -> None:
        connection, self.connection = self.connection, None
        self.state = PermitState.DISCONNECTED
        if connection is not None:
            connection.close()
