"""Saved army totals per rank band for the newest frozen leaderboard.

The Armies page's Top N and rank-band views count every attack, on every
selected Legend day, by the players in that part of the newest selected day's
frozen leaderboard. Reading those facts grows with each stored day, so the
worker saves each Legend day's totals for the 14 rank bands covering ranks
1-1000 of the newest leaderboard, and the page adds up at most 28 x 14 rows.

Each Season keeps only its newest leaderboard's totals: about 8,600 rows at
day 28, replaced at each Reset and deleted with their Legend day when season
retirement deletes its completed-day marker. A row holds the day's marker
hash it was counted from; once a day is rebuilt the row no longer matches,
the page reads facts instead, and the next check here counts again.
"""

from __future__ import annotations

import hashlib
import json
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
)

BANDS = tuple(sorted(RANK_BANDS))
_LAST_POSITION = BANDS[-1][1]
# A count reads the top 1000's facts for the Season so far, about a minute
# late in a Season, so a failed one waits before trying again.
RETRY_SECONDS = 600
_retry_at = float("-inf")


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def refresh_rank_band_totals(database: Any) -> None:
    """Count the newest leaderboard's totals when they are missing or stale.

    Runs on the worker's maintenance timer. A failure is reported and tried
    again after RETRY_SECONDS; the page reads facts meanwhile.
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
                    "snapshot_id": counted,
                    "seconds": round(monotonic() - started, 1),
                }
            ),
            flush=True,
        )


def _refresh(database: Any) -> int | None:
    with database.pool.connection() as connection, connection.transaction():
        # One snapshot for the day markers and the facts they describe.
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if not connection.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            ("army-rank-band-totals",),
        ).fetchone()[0]:
            return None
        newest = connection.execute(
            """
            SELECT snapshot.id, day.official_season_id, day.season_day_number
            FROM leaderboard_snapshots AS snapshot
            JOIN army_analytics_completed_days AS day
              ON day.ranked_day_start = snapshot.boundary_at - interval '1 day'
            WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
            ORDER BY snapshot.boundary_at DESC, snapshot.version DESC
            LIMIT 1
            """
        ).fetchone()
        if newest is None:
            return None
        snapshot_id, season_id, last_day = int(newest[0]), _text(newest[1]), int(newest[2])
        days = {
            int(row[0]): (row[1], _text(row[2]))
            for row in connection.execute(
                """
                SELECT season_day_number, ranked_day_start, fact_input_hash
                FROM army_analytics_completed_days
                WHERE official_season_id = %s AND season_day_number <= %s
                """,
                (season_id, last_day),
            ).fetchall()
        }
        saved = {
            (int(row[0]), _text(row[1]))
            for row in connection.execute(
                """
                SELECT DISTINCT season_day_number, fact_input_hash
                FROM army_analytics_rank_band_totals
                WHERE snapshot_id = %s
                """,
                (snapshot_id,),
            ).fetchall()
        }
        if saved == {(day, marker) for day, (_start, marker) in days.items()}:
            return None
        _count(connection, snapshot_id, season_id, days)
        return snapshot_id


def _count(
    connection: Any,
    snapshot_id: int,
    season_id: str,
    days: dict[int, tuple[Any, str]],
) -> None:
    band_by_player: dict[int, int] = {}
    for player_id, position in connection.execute(
        """
        SELECT player_id, position FROM leaderboard_snapshot_entries
        WHERE snapshot_id = %s AND position <= %s
        """,
        (snapshot_id, _LAST_POSITION),
    ).fetchall():
        band_by_player[int(player_id)] = next(
            first for first, last in BANDS if first <= int(position) <= last
        )
    players = sorted(band_by_player)
    fact_filter = """
        fact.is_current AND fact.official_season_id = %s
          AND fact.season_day_number = ANY(%s::integer[])
          AND fact.population_player_id = ANY(%s::bigint[])
    """
    fact_params = (season_id, sorted(days), players)
    # The ordered fact list each band was counted from, as the page's
    # evidence hash: it changes only when that band's own facts change.
    digests = {
        (int(row[0]), _text(row[1]), int(row[2])): _text(row[3])
        for row in connection.execute(
            f"""
            SELECT fact.season_day_number, fact.lens, band.first_position,
                   encode(sha256(convert_to(string_agg(
                       fact.id::text || ':' || fact.input_hash,
                       ',' ORDER BY fact.battle_id
                   ), 'UTF8')), 'hex')
            FROM army_analytics_battle_facts AS fact
            JOIN unnest(%s::bigint[], %s::integer[])
                AS band(player_id, first_position)
              ON band.player_id = fact.population_player_id
            WHERE {fact_filter}
            GROUP BY 1, 2, 3
            """,
            (players, [band_by_player[player] for player in players], *fact_params),
        ).fetchall()
    }
    totals = {
        (day, lens, first, category): new_army_totals()
        for day in days
        for lens in LENSES
        for first, _last in BANDS
        for category in CATEGORIES
    }
    with connection.cursor(name="army_rank_band_facts") as cursor:
        cursor.itersize = 2000
        cursor.execute(
            f"""
            SELECT fact.season_day_number, fact.lens, fact.population_player_id,
                   fact.stars, fact.destruction_percentage, fact.army_state,
                   fact.home_troops, fact.spells, fact.siege, fact.cc_troops,
                   fact.heroes, fact.unresolved_components,
                   fact.perspective_disagreement
            FROM army_analytics_battle_facts_with_armies AS fact
            WHERE {fact_filter}
            """,
            fact_params,
        )
        for row in cursor:
            fact = {
                "stars": row[3],
                "destruction_percentage": row[4],
                "army_state": _text(row[5]),
                "home_troops": row[6],
                "spells": row[7],
                "siege": row[8],
                "cc_troops": row[9],
                "heroes": row[10],
                "unresolved_components": row[11],
                "perspective_disagreement": row[12],
            }
            key = (int(row[0]), _text(row[1]), band_by_player[int(row[2])])
            for category in CATEGORIES:
                add_army_fact(totals[(*key, category)], fact, category)
    connection.execute(
        "DELETE FROM army_analytics_rank_band_totals WHERE official_season_id = %s",
        (season_id,),
    )
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
                    days[day][0],
                    days[day][1],
                    digests.get((day, lens, first)),
                    Jsonb(day_totals),
                )
                for (day, lens, first, category), day_totals in totals.items()
            ],
        )


def read_rank_band_totals(
    connection: Any,
    snapshot_id: int,
    *,
    lens: str,
    category: str,
    population: str,
    day_markers: dict[int, str],
) -> tuple[dict[str, Any], str] | None:
    """Add up saved totals for a Top N or rank-band view, if all are current.

    Returns the totals and a digest of the fact lists they were counted
    from, or None when any selected day's totals are missing or were counted
    from an older build of that day.
    """
    if population.startswith("top-"):
        low, high = 1, int(population.removeprefix("top-"))
    elif population.startswith("band-"):
        low, high = map(int, population.removeprefix("band-").split("-"))
    else:
        return None
    covered = [(first, last) for first, last in BANDS if low <= first and last <= high]
    if sum(last - first + 1 for first, last in covered) != high - low + 1:
        return None
    bands = [first for first, _last in covered]
    rows = connection.execute(
        """
        SELECT season_day_number, first_position, fact_input_hash,
               source_digest, totals
        FROM army_analytics_rank_band_totals
        WHERE snapshot_id = %s AND lens = %s AND category = %s
          AND season_day_number = ANY(%s::integer[])
          AND first_position = ANY(%s::integer[])
        ORDER BY season_day_number, first_position
        """,
        (snapshot_id, lens, category, sorted(day_markers), bands),
    ).fetchall()
    if len(rows) != len(day_markers) * len(bands) or any(
        _text(row[2]) != day_markers[int(row[0])] for row in rows
    ):
        return None
    totals = new_army_totals()
    for row in rows:
        merge_army_totals(totals, row[4])
    digest = hashlib.sha256(
        json.dumps(
            [[int(row[0]), int(row[1]), row[3]] for row in rows],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return totals, digest
