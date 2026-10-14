"""Helpers that `development/season-replay` runs on the replay machine.

``relay`` forwards a Unix socket to PostgreSQL's TCP port, so the worker can
run in a network namespace with no network at all. ``cli`` runs the Clash
Lens command line with board rebuilds unpaced, the way the Reset stories run
it (``tests/test_reset_scenarios_postgres.py``). ``score`` scores each Reset's
newest board against the game's own ranking lists saved in the database.
``season`` names the Season of the newest ended Legend day.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, time, timedelta


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(1 << 16):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def _relay(socket_path: str, port: int) -> None:
    async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))

    server = await asyncio.start_unix_server(forward, socket_path)
    await server.serve_forever()


def _unpaced_cli(argv: list[str]) -> int:
    from clashlens import cli, past_reset_pacing as pacing

    # Every correction starts as soon as the one before it publishes: no
    # 6-hour or hourly wait, no 04:30-07:00 or 04:00-07:00 quiet window.
    pacing.PAST_RESET_CORRECTION_INTERVAL = timedelta(0)
    pacing.NEWEST_RESET_CORRECTION_INTERVAL = timedelta(0)
    pacing._in_quiet_window = lambda _now: False
    pacing.OPERATOR_QUIET_START = pacing.PAST_RESET_QUIET_END = time(0)
    return cli.main(argv)


def _score(database_url: str, season_id: str) -> None:
    from clashlens.collector_db import CollectorDatabase
    from clashlens.game_rankings import score_day

    season_start = datetime.fromtimestamp(int(season_id), UTC)
    database = CollectorDatabase(database_url, max_size=2)
    try:
        with database._connection() as connection:
            resets = [row[0] for row in connection.execute(
                "SELECT boundary_at FROM collector_reset_sweeps WHERE boundary_at > %s"
                " AND boundary_at <= %s ORDER BY 1",
                (season_start, season_start + timedelta(days=28)),
            )]
        for reset in resets:
            print(reset.isoformat(), score_day(database, reset), flush=True)
    finally:
        database.close()


def _season(database_url: str) -> None:
    import psycopg
    from clashlens.domain import ranked_day_for

    with psycopg.connect(database_url) as connection:
        newest = connection.execute("SELECT max(boundary_at) FROM collector_reset_sweeps").fetchone()[0]
    # The Season of the newest ended Legend day.
    day = ranked_day_for(newest - timedelta(days=1))
    print(day.official_season_id, day.season_start.isoformat())


def main(argv: list[str]) -> int:
    command, arguments = argv[0], argv[1:]
    if command == "relay":
        asyncio.run(_relay(arguments[0], int(arguments[1])))
        return 0
    if command == "cli":
        return _unpaced_cli(arguments)
    if command == "season":
        _season(*arguments)
        return 0
    if command == "score":
        _score(*arguments)
        return 0
    raise SystemExit(f"unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
