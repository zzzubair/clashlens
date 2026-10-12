"""Saved army totals per rank band, one completed Legend day at a time.

The Armies page's Top N and rank-band views count each selected Legend day's
attacks by the players in that part of the day's own frozen leaderboard, the
one saved just before the Reset that ends the day. Reading those facts grows
with each stored day, so the worker saves each completed day's totals for the
17 rank bands covering ranks 1-10,000, and the page adds up at most 28 x 17
rows. Army types read the same leaderboards, so both views count the same
players.

A Reset's leaderboard keeps being corrected for a day or more, a handful of
players at a time. Each day is counted once, from the version published when
it is counted, and corrections never count it again. Only a change to the
day's own facts does: a row holds the day's marker hash it was counted from,
and a rebuilt day no longer matches. Until that recount the page serves the
saved totals.

Each Season keeps about 10,500 rows and 16 MB at day 28, deleted with their
Legend day when season retirement deletes its completed-day marker.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from time import monotonic
from typing import Any

from psycopg.types.json import Jsonb

from .army_analytics import (
    CATEGORIES,
    LENSES,
    RANK_BANDS,
    add_army_fact,
    merge_army_totals,
    new_army_totals,
    rank_band_firsts,
)

BANDS = tuple(sorted(RANK_BANDS))
_LAST_POSITION = BANDS[-1][1]
# A counted day has one row per lens, category and band.
DAY_ROWS = len(LENSES) * len(CATEGORIES) * len(BANDS)
# A count reads one day's facts for the top 10,000, about a minute, so a
# failed one waits before trying again.
RETRY_SECONDS = 600
_retry_at = float("-inf")
# Totals saved before unnamed ids counted like any other unit carry these
# tallies and leave those ids out; they are counted again, never served.
_CURRENT_FORMAT = (
    "NOT totals ?| ARRAY['cc_unknown', 'hero_unknown', 'unknown', 'unknown_present']"
)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def refresh_rank_band_totals(database: Any) -> None:
    """Count the newest Season's newest day whose totals are missing or stale.

    Runs on the worker's maintenance timer and counts at most one day, so
    other upkeep waits a minute at most. A failure is reported and tried
    again after RETRY_SECONDS.
    """
    global _retry_at
    started = monotonic()
    if started < _retry_at:
        return
    try:
        counted = _refresh(database)
    except Exception as error:  # noqa: BLE001 - retried after RETRY_SECONDS
        _retry_at = monotonic() + RETRY_SECONDS
        print(
            json.dumps(
                {
                    "event": "army_rank_band_totals",
                    "status": "failed",
                    "error": type(error).__name__,
                }
            ),
            flush=True,
        )
        return
    if counted is not None:
        print(
            json.dumps(
                {
                    "event": "army_rank_band_totals",
                    "status": "complete",
                    **counted,
                    "seconds": round(monotonic() - started, 1),
                }
            ),
            flush=True,
        )


def _refresh(database: Any) -> dict[str, Any] | None:
    with database.pool.connection() as connection, connection.transaction():
        # One snapshot for the day marker and the facts it describes.
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if not connection.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            ("army-rank-band-totals",),
        ).fetchone()[0]:
            return None
        # The newest Season's days, as for army types. Rows counted from
        # another Reset's board, before each day used its own, are counted
        # again.
        due = connection.execute(
            f"""
            SELECT day.ranked_day_start, day.official_season_id,
                   day.season_day_number, day.fact_input_hash, board.id
            FROM army_analytics_completed_days AS day
            CROSS JOIN LATERAL (
                SELECT id FROM leaderboard_snapshots
                WHERE snapshot_kind = 'frozen' AND state = 'published'
                  AND boundary_at = day.ranked_day_start + interval '1 day'
                ORDER BY version DESC LIMIT 1
            ) AS board
            WHERE day.official_season_id = (
                SELECT official_season_id FROM army_analytics_completed_days
                ORDER BY ranked_day_start DESC LIMIT 1
            ) AND (
                SELECT count(*)
                FROM army_analytics_rank_band_totals AS total
                JOIN leaderboard_snapshots AS saved ON saved.id = total.snapshot_id
                WHERE total.ranked_day_start = day.ranked_day_start
                  AND total.fact_input_hash = day.fact_input_hash
                  AND saved.boundary_at = day.ranked_day_start + interval '1 day'
                  AND {_CURRENT_FORMAT}
            ) <> %s
            ORDER BY day.ranked_day_start DESC
            LIMIT 1
            """,
            (DAY_ROWS,),
        ).fetchone()
        if due is None:
            return None
        start, season_id, day, marker, snapshot_id = due
        connection.execute(
            "DELETE FROM army_analytics_rank_band_totals WHERE ranked_day_start = %s",
            (start,),
        )
        _count(
            connection, int(snapshot_id), _text(season_id), int(day), start, _text(marker)
        )
        return {
            "official_season_id": _text(season_id),
            "season_day_number": int(day),
            "snapshot_id": int(snapshot_id),
        }


def _count(
    connection: Any,
    snapshot_id: int,
    season_id: str,
    day: int,
    ranked_day_start: Any,
    marker: str,
) -> None:
    """Save one day's totals for every band of ``snapshot_id``."""
    band_by_player = band_players(connection, snapshot_id, 1, _LAST_POSITION)
    digests = _fact_digests(connection, season_id, day, band_by_player)
    totals = {
        (lens, first, category): new_army_totals()
        for lens in LENSES
        for first, _last in BANDS
        for category in CATEGORIES
    }
    with connection.cursor(name="army_rank_band_facts") as cursor:
        cursor.itersize = 2000
        cursor.execute(
            """
            SELECT fact.lens, fact.population_player_id,
                   fact.stars, fact.destruction_percentage, fact.army_state,
                   fact.home_troops, fact.spells, fact.siege, fact.cc_troops,
                   fact.heroes, fact.unresolved_components,
                   fact.perspective_disagreement
            FROM army_analytics_battle_facts_with_armies AS fact
            WHERE fact.is_current AND fact.official_season_id = %s
              AND fact.season_day_number = %s
              AND fact.population_player_id = ANY(%s::bigint[])
            """,
            (season_id, day, sorted(band_by_player)),
        )
        for row in cursor:
            fact = {
                "stars": row[2],
                "destruction_percentage": row[3],
                "army_state": _text(row[4]),
                "home_troops": row[5],
                "spells": row[6],
                "siege": row[7],
                "cc_troops": row[8],
                "heroes": row[9],
                "unresolved_components": row[10],
                "perspective_disagreement": row[11],
            }
            key = (_text(row[0]), band_by_player[int(row[1])])
            for category in CATEGORIES:
                add_army_fact(totals[(*key, category)], fact, category)
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO army_analytics_rank_band_totals (
                snapshot_id, lens, category, season_day_number, first_position,
                official_season_id, ranked_day_start, fact_input_hash,
                source_digest, totals
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    snapshot_id,
                    lens,
                    category,
                    day,
                    first,
                    season_id,
                    ranked_day_start,
                    marker,
                    digests.get((lens, first)),
                    Jsonb(day_totals),
                )
                for (lens, first, category), day_totals in totals.items()
            ],
        )


def counted_boards(connection: Any, season_id: str) -> dict[int, tuple[int, int]]:
    """Each counted day of a Season: the board its totals used, and its version."""
    return {
        int(day): (int(snapshot_id), int(version))
        for day, snapshot_id, version in connection.execute(
            """
            SELECT DISTINCT total.season_day_number, total.snapshot_id,
                   snapshot.version
            FROM army_analytics_rank_band_totals AS total
            JOIN leaderboard_snapshots AS snapshot
              ON snapshot.id = total.snapshot_id
             AND snapshot.boundary_at = total.ranked_day_start + interval '1 day'
            WHERE total.official_season_id = %s
            """,
            (season_id,),
        ).fetchall()
    }


def read_rank_band_totals(
    connection: Any,
    boards: dict[int, int],
    *,
    lens: str,
    category: str,
    population: str,
) -> tuple[dict[str, Any], str] | None:
    """Add up saved totals for a Top N or rank-band view.

    ``boards`` maps each selected day to the board ``counted_boards`` gives
    it. Returns the totals and their ``rank_band_digest``, or None when any
    selected day's totals were saved in the format before unnamed ids
    counted.
    """
    bands = _bands(population)
    if bands is None:
        return None
    rows = connection.execute(
        f"""
        SELECT total.season_day_number, total.first_position,
               total.source_digest, total.totals
        FROM army_analytics_rank_band_totals AS total
        JOIN unnest(%s::integer[], %s::bigint[]) AS board(day, snapshot_id)
          ON board.day = total.season_day_number
         AND board.snapshot_id = total.snapshot_id
        WHERE total.lens = %s AND total.category = %s
          AND total.first_position = ANY(%s::integer[])
          AND {_CURRENT_FORMAT}
        """,
        (list(boards), list(boards.values()), lens, category, bands),
    ).fetchall()
    if len(rows) != len(boards) * len(bands):
        return None
    totals = new_army_totals()
    for row in rows:
        merge_army_totals(totals, row[3])
    digests = {(int(row[0]), int(row[1])): row[2] for row in rows}
    return totals, rank_band_digest(population, boards, digests)


def band_players(
    connection: Any, snapshot_id: int, low: int, high: int
) -> dict[int, int]:
    """The players at ranks low-high of a board, each with its band's first rank."""
    return {
        int(player_id): band_of(int(position))
        for player_id, position in connection.execute(
            """
            SELECT player_id, position FROM leaderboard_snapshot_entries
            WHERE snapshot_id = %s AND position BETWEEN %s AND %s
            """,
            (snapshot_id, low, high),
        ).fetchall()
    }


def band_of(position: int) -> int:
    """The first position of the rank band holding ``position``."""
    return next(first for first, last in BANDS if first <= position <= last)


def _bands(population: str) -> list[int] | None:
    if population.startswith("top-"):
        return rank_band_firsts(1, int(population.removeprefix("top-")))
    if population.startswith("band-"):
        low, high = map(int, population.removeprefix("band-").split("-"))
        return rank_band_firsts(low, high)
    return None


def rank_band_digest(
    population: str,
    days: Iterable[int],
    digests: Mapping[tuple[int, int], str | None],
) -> str:
    """Digest of a Top N or rank-band view's fact lists, one per day and band.

    ``digests`` holds the SHA-256 of each (day, band first position)'s
    ``id:input_hash`` facts in battle order, joined by commas, as saved with
    the totals.
    """
    bands = _bands(population)
    assert bands is not None
    return hashlib.sha256(
        json.dumps(
            [
                [day, first, digests.get((day, first))]
                for day in sorted(days)
                for first in bands
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _fact_digests(
    connection: Any, season_id: str, day: int, band_by_player: dict[int, int]
) -> dict[tuple[str, int], str]:
    # The ordered fact list of each lens and band: it changes only when that
    # band's own facts change.
    players = sorted(band_by_player)
    return {
        (_text(row[0]), int(row[1])): _text(row[2])
        for row in connection.execute(
            """
            SELECT fact.lens, band.first_position,
                   encode(sha256(convert_to(string_agg(
                       fact.id::text || ':' || fact.input_hash,
                       ',' ORDER BY fact.battle_id
                   ), 'UTF8')), 'hex')
            FROM army_analytics_battle_facts AS fact
            JOIN unnest(%s::bigint[], %s::integer[])
                AS band(player_id, first_position)
              ON band.player_id = fact.population_player_id
            WHERE fact.is_current AND fact.official_season_id = %s
              AND fact.season_day_number = %s
              AND fact.population_player_id = ANY(%s::bigint[])
            GROUP BY 1, 2
            """,
            (
                players,
                [band_by_player[player] for player in players],
                season_id,
                day,
                players,
            ),
        ).fetchall()
    }
