"""Cancellation-safe thread ownership and bounded database retries."""

from __future__ import annotations

import asyncio
import random
from contextlib import nullcontext
from typing import Any

import psycopg
from psycopg_pool import PoolTimeout


async def retry_database(
    operation: Any,
    *args: Any,
    on_failure: Any,
    wait_context: Any = nullcontext,
    **kwargs: Any,
) -> Any:
    for attempt in range(3):
        try:
            with wait_context():
                return await asyncio.to_thread(operation, *args, **kwargs)
        except psycopg.errors.LockNotAvailable:
            raise
        except (psycopg.Error, PoolTimeout):
            on_failure("database_failure")
            if attempt == 2:
                raise
            await asyncio.sleep(retry_delay(attempt))
    raise AssertionError("unreachable database retry loop")


def retry_delay(attempt: int) -> float:
    return min(5.0, 0.25 * 2**attempt) + random.uniform(0.0, 0.1)


async def drain_to_thread(operation: Any, *args: Any, **kwargs: Any) -> Any:
    return await drain_awaitable(asyncio.to_thread(operation, *args, **kwargs))


async def drain_awaitable(awaitable: Any) -> Any:
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation cannot stop a running thread or synchronous database
            # call. Keep ownership until it reaches a safe endpoint, even if
            # the owner receives a second cancellation while draining.
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def stop_task(stop_requested: asyncio.Event, task: asyncio.Task[None]) -> None:
    stop_requested.set()
    await task


async def cancel_task(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def wait_or_stop(stop_requested: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_requested.wait(), seconds)
    except TimeoutError:
        pass
