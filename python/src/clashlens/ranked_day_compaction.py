"""Delete the extra saved copies of each player's ended Legend days.

Every battle saves a complete new copy of the player's day result, and players
only see the newest. Once a Legend day has ended and its Reset work is done,
the same readiness the late-battle sweep waits for, this deletes in small
batches every copy nothing needs, keeping each day's newest copy and every copy
a publication, the following day or a queued correction points at (see
migration 0052). Nothing that runs later needs the deleted copies: late
corrections and the next day's recalculation read only the newest copy, and an
earlier result that becomes current again is saved as a new copy.

`./ops` runs this on a timer inside the worker container. Each run works for at
most RUN_SECONDS and pauses between batches, so a backlog of many days is
worked through over several runs.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import psycopg

from .domain import ranked_day_for
from .late_battle_sweep import SWEEP_DELAY, reset_work_finished

# About 25 players' copies of one day and the day before: 50 player-day
# locks and a few hundred deletions per batch.
PLAYERS_PER_BATCH = 25
# A batch holds its player-day locks until it ends, and on 2026-10-03 one ran
# for 407 seconds, so it is cancelled and rolled back after this long.
BATCH_TIMEOUT = "5s"
PAUSE_SECONDS = 1.0
RUN_SECONDS = 120.0


def ready_through(connection: Any, now: datetime) -> datetime:
    """The latest Reset whose ended Legend days may be cleaned."""
    boundary = ranked_day_for(now).start
    if now >= boundary + SWEEP_DELAY and reset_work_finished(connection, boundary):
        return boundary
    return boundary - timedelta(days=1)


def compact(
    connection: Any,
    *,
    now: datetime | None = None,
    run_seconds: float = RUN_SECONDS,
    pause_seconds: float = PAUSE_SECONDS,
    players_per_batch: int = PLAYERS_PER_BATCH,
) -> dict[str, Any]:
    """Run batches, each in its own transaction, until done or out of time."""
    deadline = monotonic() + run_seconds
    totals = {"batches": 0, "deleted_versions": 0, "deleted_logs": 0}
    finished_days: list[str] = []
    with connection.transaction():
        through = ready_through(connection, now or datetime.now(UTC))
    while True:
        if monotonic() >= deadline:
            return {**totals, "finished_days": finished_days, "status": "paused"}
        try:
            with connection.transaction():
                # The function's own lock_timeout limits only its waits.
                connection.execute(f"SET LOCAL statement_timeout = '{BATCH_TIMEOUT}'")
                row = connection.execute(
                    "SELECT * FROM clashlens_compact_ranked_days(%s, %s)",
                    (through, players_per_batch),
                ).fetchone()
        except psycopg.errors.LockNotAvailable:
            # A live recalculation held a player-day; the next run retries the batch.
            return {**totals, "finished_days": finished_days, "status": "busy"}
        except psycopg.errors.QueryCanceled:
            # The batch ran out of time and rolled back, progress included;
            # the next run retries it.
            return {**totals, "finished_days": finished_days, "status": "timed_out"}
        if row is None:
            return {**totals, "finished_days": finished_days, "status": "idle"}
        totals["batches"] += 1
        totals["deleted_versions"] += int(row[2])
        totals["deleted_logs"] += int(row[3])
        if row[4]:
            finished_days.append(row[0].astimezone(UTC).isoformat())
        if monotonic() + pause_seconds >= deadline:
            return {**totals, "finished_days": finished_days, "status": "paused"}
        sleep(pause_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--database-url-file",
        default=os.environ.get("CLASHLENS_DATABASE_URL_FILE", ""),
    )
    arguments = parser.parse_args(argv)
    if not arguments.database_url_file:
        parser.error("--database-url-file or CLASHLENS_DATABASE_URL_FILE is required")
    url = Path(arguments.database_url_file).read_text(encoding="utf-8").strip()
    with psycopg.connect(url, autocommit=True) as connection:
        result = compact(connection)
    print(json.dumps({"event": "ranked_day_compaction", **result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
