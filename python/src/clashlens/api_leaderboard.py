from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .analytics import (
    tie_order_sql,
)
from .api_db import (
    ApiDatabase,
    _json_array,
    _net_so_far,
    _public_confidence,
    _public_snapshot_confidence,
    _shown_total,
    _text,
)
from .domain import (
    RANKED_DAY_DURATION,
    SEASON_DURATION,
    SEASON_START_TROPHIES,
    ranked_day_for,
)
from .live_board import (
    CURRENT_PROFILE_JOINS,
    UNPLAYED_CANDIDATE_SQL,
    UNPLAYED_SEARCH_SQL,
    season_params,
    unplayed_seen_sql,
)
from .shared_copy import SharedCopy

_LIVE_FRESHNESS_SECONDS = 600
# Rows kept beside a selected player when their page edge would hide them.
_FOCUS_NEIGHBORS = 5
# Finished Legend days each Live row's chart shows; today has its own column.
RECENT_DAYS = 7


def _live_candidates_sql(
    players: str = "players",
    saved: str = "live_unplayed_profiles",
    tallies: str = "live_attack_tallies",
) -> str:
    """Each tracked player the Live board may rank, from ``players``, the
    saved 5,000 profiles ``saved`` and the attack counts ``tallies``: the
    tables, or copies of the rows the board can use."""
    return f"""
SELECT player.normalized_tag, COALESCE(unplayed.name, profile.name) AS name,
       COALESCE(unplayed.trophies, profile.trophies) AS trophies,
       CASE WHEN unplayed.player_id IS NULL
            THEN greatest(player.current_observed_at, player.current_profile_confirmed_at)
            ELSE {unplayed_seen_sql(
                "unplayed.sighted_at", "unplayed.profile_version_id",
                "unplayed.observation_id, unplayed.sighting_observation_id",
            )} END AS observed_at,
       player.eligibility_state,
       CASE WHEN unplayed.player_id IS NULL
            THEN profile.profile_json -> 'clan' ->> 'name'
            ELSE unplayed.clan END AS clan,
       current.season_current OR unplayed.player_id IS NOT NULL AS season_current,
       tally.attacks, tally.destruction
FROM {players} AS player
{CURRENT_PROFILE_JOINS}
-- The same profile as the worker last found it (refresh_live_unplayed), with
-- the parts the board shows: the search above reads about 16 scattered pages
-- per player, 8 s for 1,365 players on 11 October 2026, too slow for every
-- board read. A player whose current profile now ranks them at another total
-- leaves at once; one who starts qualifying, plays or changes profile waits
-- for the next refresh.
LEFT JOIN {saved} AS unplayed
  ON unplayed.player_id = player.id
 AND unplayed.official_season_id = %(season_id)s
 AND {UNPLAYED_CANDIDATE_SQL}
LEFT JOIN {tallies} AS tally
  ON tally.player_id = player.id AND tally.official_season_id = %(season_id)s
WHERE player.active = true
  AND (profile.name IS NOT NULL OR unplayed.name IS NOT NULL)
  -- Still tracked while only unconfirmed profiles prove the drop
  -- (league_days.track), but out of Legend I: on 26 October 2026, 2,047 of
  -- that Reset's 2,079 dropped players stayed on the board at their last
  -- Legend I trophies.
  AND NOT EXISTS (
      SELECT 1 FROM player_league_weeks AS week
      WHERE week.player_id = player.id
        AND week.week_start = %(week_start)s
        AND week.league = 'below_legend_i'
  )
  AND NOT EXISTS (
      SELECT 1 FROM collector_response_state AS checked
      WHERE checked.scope = 'player'
        AND checked.identity_key = player.normalized_tag
        AND checked.endpoint = 'profile'
        AND checked.last_not_found_at IS NOT NULL
        AND (checked.last_success_at IS NULL
             OR checked.last_not_found_at > checked.last_success_at)
  )
"""


_LIVE_CANDIDATES_SQL = _live_candidates_sql()
_LIVE_PLAYERS_SQL = f"""
SELECT * FROM ({_LIVE_CANDIDATES_SQL}) AS candidate WHERE season_current
"""
# The profile that ranks one player at 5,000 when their current one does not.
UNPLAYED_PROFILE_SQL = f"""
SELECT unplayed.id, unplayed.observed_at
FROM players AS player
{CURRENT_PROFILE_JOINS}JOIN LATERAL ({UNPLAYED_SEARCH_SQL}) AS unplayed ON true
WHERE player.normalized_tag = %(tag)s
"""

# Equal trophies in the Daily board's order (analytics.tie_order_key), from
# the counts the worker last saved (analytics.refresh_live_attack_tallies).
# v1 ordered them by MD5 of the tag alone.
_LIVE_ORDER_VERSION = "tracked-trophies-attack-destruction-v2"
_LIVE_ORDER_SQL = (
    f"trophies DESC, {tie_order_sql('attacks', 'destruction', 'normalized_tag')}"
)
_LIVE_RANKED_SQL = f"""
SELECT *, row_number() OVER (ORDER BY {_LIVE_ORDER_SQL}) AS position
FROM ({_LIVE_PLAYERS_SQL}) AS live
"""


def search_live_leaderboard(
    database: ApiDatabase,
    query: str,
    *,
    now: datetime,
    cache: LiveBoardCache | None = None,
) -> dict[str, Any]:
    """Filter the ranked board the focused Live Leaderboard page reads."""
    query = query.strip()
    if not query or len(query) > 80:
        raise ValueError("invalid leaderboard search")
    tag = "#" + query.removeprefix("#").upper()
    ranked = list(enumerate(_live_board(database, now, cache).rows, start=1))
    exact = [
        (rank, row) for rank, row in ranked if query.startswith("#") and row[0] == tag
    ]
    needle = query.casefold()
    found = exact or [
        (rank, row)
        for rank, row in ranked
        if row[0] == tag or needle in _text(row[1]).casefold()
    ]
    return {
        "exact_tag": tag if exact else None,
        "has_more": len(found) > 20,
        "results": [
            {
                "tag": _text(row[0]),
                "name": _text(row[1]),
                "trophies": int(row[2]),
                "rank": rank,
            }
            for rank, row in found[:20]
        ],
    }


def live_positions(connection: Any, tags: list[str], *, now: datetime) -> dict[str, int]:
    """Each tag's Live Leaderboard position; a tag off the board is left out."""
    if not tags:
        return {}
    rows = connection.execute(
        f"""
        SELECT normalized_tag, position FROM ({_LIVE_RANKED_SQL}) AS ranked
        WHERE normalized_tag = ANY(%(tags)s)
        """,
        {"tags": tags, **season_params(now)},
    ).fetchall()
    return {_text(row[0]): int(row[1]) for row in rows}


def live_freshness_metrics(database: ApiDatabase, *, now: datetime) -> dict[str, Any]:
    """Measure the whole Live Leaderboard, without sorting or fetching a page."""
    with database.pool.connection() as connection:
        row = connection.execute(
            f"""
            WITH selected AS ({_LIVE_PLAYERS_SQL}), ages AS (
                SELECT CASE WHEN observed_at IS NOT NULL
                            THEN greatest(0, extract(epoch FROM %(now)s - observed_at))
                       END AS age
                FROM selected
            )
            SELECT count(*), count(*) - count(age),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY age),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY age), max(age),
                   count(*) FILTER (WHERE age > %(fresh)s)
            FROM ages
            """,
            {
                "now": now,
                "fresh": _LIVE_FRESHNESS_SECONDS,
                **season_params(now),
            },
        ).fetchone()
    return {
        "sample_timestamp_seconds": now.timestamp(),
        "entries": int(row[0]),
        "age_missing_entries": int(row[1]),
        "age_p50_seconds": None if row[2] is None else float(row[2]),
        "age_p95_seconds": None if row[3] is None else float(row[3]),
        "age_max_seconds": None if row[4] is None else float(row[4]),
        "older_than_10_minutes": int(row[5]),
    }


@dataclass(frozen=True)
class _LiveBoard:
    tracked_population: int
    season_reset_pending: int
    # (tag, name, trophies, observed_at, eligibility_state, clan) in rank order.
    rows: list[tuple[Any, ...]]

    def tied(self, index: int) -> bool:
        """Whether the row at ``index`` shares its trophies with a neighbor."""
        trophies = self.rows[index][2]
        return any(
            0 <= other < len(self.rows) and self.rows[other][2] == trophies
            for other in (index - 1, index + 1)
        )

    def positions(self, tags: list[str]) -> dict[str, int]:
        """Each tag's position on this board; a tag off it is left out."""
        wanted = set(tags)
        return {
            _text(row[0]): position
            for position, row in enumerate(self.rows, start=1)
            if _text(row[0]) in wanted
        }


def _read_live_board(connection: Any, now: datetime) -> _LiveBoard:
    rows = connection.execute(
        f"""
        -- Each table read once, not once per player: on 26 October 2026
        -- the board read the players twice, and looked up 13,027 players'
        -- saved 5,000 profiles and attack counts one by one, 68,562 pages
        -- for 965 profiles and 13,010 counts.
        WITH tracked AS MATERIALIZED (
            SELECT id, normalized_tag, active, current_profile_version_id,
                   current_observed_at, current_profile_confirmed_at,
                   eligibility_state
            FROM players WHERE active
        ), saved AS MATERIALIZED (
            SELECT * FROM live_unplayed_profiles
            WHERE official_season_id = %(season_id)s
        ), tallies AS MATERIALIZED (
            SELECT * FROM live_attack_tallies
            WHERE official_season_id = %(season_id)s
        ), candidates AS MATERIALIZED (
            {_live_candidates_sql("tracked", "saved", "tallies")}
        ), totals AS (
            SELECT count(*)::bigint AS tracked_population,
                   (SELECT count(*) FROM candidates WHERE NOT season_current)
                       AS season_reset_pending
            FROM tracked
        )
        SELECT totals.tracked_population, totals.season_reset_pending,
               live.normalized_tag, live.name, live.trophies, live.observed_at,
               live.eligibility_state, live.clan
        FROM totals LEFT JOIN (
            SELECT * FROM candidates WHERE season_current
        ) AS live ON true
        ORDER BY {_LIVE_ORDER_SQL}
        """,
        season_params(now),
    ).fetchall()
    return _LiveBoard(
        tracked_population=int(rows[0][0]),
        season_reset_pending=int(rows[0][1]),
        rows=[tuple(row[2:]) for row in rows if row[2] is not None],
    )


def _read_live_board_from(database: ApiDatabase, now: datetime) -> _LiveBoard:
    with database.pool.connection() as connection:
        return _read_live_board(connection, now)


class LiveBoardCache(SharedCopy[_LiveBoard]):
    """The ranked Live Leaderboard shared by every page of it, the home page's
    top players, public profiles' ranks and the dashboard's board for the
    Next Reset range and base comparisons: read again in the background
    once 20 seconds old, never served once 30 seconds old, and read anew for
    each Legend day, so never across a Reset or into a new Season."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        super().__init__(
            _read_live_board_from,
            key=lambda now: ranked_day_for(now).start,
            seconds=20,
            longest=30,
            name="live_board_copy",
            clock=clock,
            start=start,
        )


def _live_board(
    database: ApiDatabase, now: datetime, cache: LiveBoardCache | None
) -> _LiveBoard:
    if cache is not None:
        return cache.current(database, now)
    return _read_live_board_from(database, now)


def get_live_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    now: datetime,
    focus_tag: str | None = None,
    cache: LiveBoardCache | None = None,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    board = _live_board(database, now, cache)
    rows = board.rows
    total_entries = len(rows)
    first, last = offset + 1, offset + limit
    if focus_tag is not None:
        focus_position = next(
            (index + 1 for index, row in enumerate(rows) if row[0] == focus_tag),
            None,
        )
        if focus_position is None:
            return None
        offset = (focus_position - 1) // limit * limit
        first = min(offset + 1, focus_position - _FOCUS_NEIGHBORS)
        last = max(offset + limit, focus_position + _FOCUS_NEIGHBORS)
    if offset and offset >= total_entries:
        return None
    observed = [row[3] for row in rows if row[3] is not None]
    stale_before = now - timedelta(seconds=_LIVE_FRESHNESS_SECONDS)
    stale_count = sum(1 for value in observed if value < stale_before)
    oldest_observed_at = min(observed, default=None)
    newest_observed_at = max(observed, default=None)
    entries = []
    first = max(first, 1)
    for position, row in enumerate(rows[first - 1 : last], start=first):
        observed_at = row[3].astimezone(UTC)
        age = max(0.0, (now.astimezone(UTC) - observed_at).total_seconds())
        age_seconds = int(age)
        freshness = "fresh" if age <= _LIVE_FRESHNESS_SECONDS else "stale"
        entries.append(
            {
                "position": position,
                "tag": _text(row[0]),
                "name": _text(row[1]),
                "trophies": int(row[2]),
                "observed_at": observed_at.isoformat(),
                "age_seconds": age_seconds,
                "freshness": freshness,
                "confidence": _text(row[4]),
                "public_confidence": _public_confidence(True, _text(row[4])),
                "clan": None if row[5] is None else _text(row[5]),
                "official_rank": None,
                "tied": board.tied(position - 1),
            }
        )
    with database.pool.connection() as connection:
        at_reset = _reset_entries(connection, [entry["tag"] for entry in entries], now)
    for entry in entries:
        (
            entry["reset_position"],
            entry["today_change"],
            entry["recent_days"],
        ) = at_reset[entry["tag"]]
    page_count = (total_entries + limit - 1) // limit
    page = offset // limit + 1
    return {
        "kind": "live",
        "ordering_rule_version": _LIVE_ORDER_VERSION,
        "generated_at": now.astimezone(UTC).isoformat(),
        "tracked_population": board.tracked_population,
        "total_entries": total_entries,
        # Tracked players left off until a profile names this Season.
        "season_reset_pending": board.season_reset_pending,
        "page": page,
        "page_size": limit,
        "page_count": page_count,
        "has_previous": page > 1,
        "has_next": page < page_count,
        "coverage": {
            "state": "partial",
            "tracked_players": board.tracked_population,
            "measured_percent": (
                100.0 * total_entries / board.tracked_population
                if board.tracked_population
                else 0.0
            ),
            "note": "Tracked-player publication; complete Legend I coverage is not claimed.",
        },
        "provenance": {
            "source": "current accepted player profiles",
            "observed_at": (
                None
                if newest_observed_at is None
                else newest_observed_at.astimezone(UTC).isoformat()
            ),
            "freshness": "stale" if stale_count else "fresh",
            "confidence": "partial",
            "coverage": "partial",
            "version": _LIVE_ORDER_VERSION,
        },
        "source_observations": {
            "oldest_observed_at": (
                None
                if oldest_observed_at is None
                else oldest_observed_at.astimezone(UTC).isoformat()
            ),
            "newest_observed_at": (
                None
                if newest_observed_at is None
                else newest_observed_at.astimezone(UTC).isoformat()
            ),
            "stale_count": stale_count,
        },
        "quality_states": ["partial"] + (["stale"] if stale_count else []),
        "entries": entries,
    }


def _frozen_publications() -> str:
    """Every published Daily board, legacy and v2, as SQL giving (source, id,
    boundary_at, version, generation, official_season_id, season_day_number)."""
    legacy_publications = """
        SELECT 'legacy'::text AS source, leaderboard.id,
               leaderboard.boundary_at, leaderboard.version, 0 AS generation,
               selector.official_season_id, selector.season_day_number
        FROM api_frozen_leaderboards AS leaderboard
        JOIN LATERAL (
            SELECT day.official_season_id, day.season_day_number
            FROM ranked_day_versions AS day
            WHERE day.ranked_day_end = leaderboard.boundary_at
            ORDER BY day.id DESC LIMIT 1
        ) AS selector ON true
    """
    v2_publications = """
        SELECT 'v2'::text AS source, snapshot.id, snapshot.boundary_at,
               snapshot.version, COALESCE(generation.generation, 0) AS generation,
               COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
               COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number
        FROM leaderboard_snapshots AS snapshot
        LEFT JOIN ranked_day_versions AS source_day
          ON source_day.id = snapshot.source_ranked_day_version_id
        LEFT JOIN boundary_publication_generations AS generation
          ON generation.snapshot_id = snapshot.id
        LEFT JOIN LATERAL (
            -- A proof frozen since 0081 records its newest ranked day;
            -- an older one stores all its rows, so they give it.
            SELECT ranked.official_season_id, ranked.season_day_number
            FROM boundary_publication_manifests AS manifest
            JOIN ranked_day_versions AS ranked
              ON ranked.id = COALESCE(
                  manifest.newest_ranked_day_version_id,
                  (SELECT member.ranked_day_version_id
                   FROM boundary_publication_manifest_rows AS member
                   WHERE member.manifest_id = manifest.id
                     AND member.ranked_day_version_id IS NOT NULL
                   ORDER BY member.ranked_day_version_id DESC LIMIT 1))
            WHERE manifest.id = generation.snapshot_manifest_id
        ) AS generation_day ON true
        WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
          AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
    """
    return v2_publications + " UNION ALL " + legacy_publications


def _reset_place_sql(connection: Any) -> tuple[str, str]:
    """The newest Daily board published at ``%(reset)s`` for Season
    ``%(season_id)s``, as a ``WITH`` clause, and the joins that give each
    ``player`` row its ``place.position`` there, or none off it. A Season's
    first Legend day has no such board: the one at its Reset is the Season
    before's."""
    publications = _frozen_publications()
    v2_entry = (
        """
        SELECT position FROM leaderboard_snapshot_entries
        WHERE board.source = 'v2' AND snapshot_id = board.id
          AND player_id = player.id
        UNION ALL
        """
    )
    return (
        f"""
        WITH board AS (
            SELECT source, id
            FROM ({publications}) AS publication
            WHERE boundary_at = %(reset)s AND official_season_id = %(season_id)s
            ORDER BY (source = 'v2') DESC, version DESC, generation DESC, id DESC
            LIMIT 1
        )
        """,
        f"""
        LEFT JOIN board ON true
        LEFT JOIN LATERAL (
            {v2_entry}
            SELECT position FROM api_frozen_leaderboard_entries
            WHERE board.source = 'legacy' AND leaderboard_id = board.id
              AND player_id = player.id
        ) AS place ON true
        """,
    )


def linked_season_days(
    connection: Any, tags: list[str], now: datetime
) -> list[dict[str, Any]]:
    """What a public profile's player cards add to their numbers, for every
    tag at once in a fixed number of reads: each player's place on the newest
    Daily board published at the last Reset (none on a Season's first Legend
    day, as on the Live board), this Season's weekly league records, and each
    Legend day saved so far this Season, today's included, oldest first.

    Each day is its newest saved log, read as the player page's Daily Legend
    log shows it, and is only what its Season heat map draws, as ``[Season
    day, attacks, defenses, result, unplayed, listed]``, so 500 players' full
    Seasons stay well under the website's 2 MiB response limit. ``unplayed``
    is a day known to be 5,000 to 5,000 with no battles; ``listed`` is false
    for a day outside Legend I that the player page's log leaves out. Its
    battles are never read."""
    if not tags:
        return []
    board, place = _reset_place_sql(connection)
    day = ranked_day_for(now)
    rows = connection.execute(
        f"""
        {board}
        SELECT player.id, player.normalized_tag, place.position,
               daily.ranked_day_start, daily.attack_count, daily.defense_count,
               daily.net_trophy_change, daily.coverage, daily.partial_reasons,
               ranked_day.start_trophies
        FROM players AS player
        {place}
        LEFT JOIN LATERAL (
            SELECT DISTINCT ON (saved.ranked_day_start)
                   saved.ranked_day_start, saved.attack_count, saved.defense_count,
                   saved.net_trophy_change, saved.coverage, saved.partial_reasons,
                   saved.ranked_day_version_id
            FROM api_player_daily_logs AS saved
            WHERE saved.player_id = player.id
              AND saved.ranked_day_start BETWEEN %(first)s AND %(reset)s
            ORDER BY saved.ranked_day_start DESC, saved.version DESC
        ) AS daily ON true
        LEFT JOIN ranked_day_versions AS ranked_day
            ON ranked_day.id = daily.ranked_day_version_id
        WHERE player.normalized_tag = ANY(%(tags)s)
        ORDER BY player.normalized_tag, daily.ranked_day_start
        """,
        {
            "first": day.season_start,
            "reset": day.start,
            "season_id": day.official_season_id,
            "tags": tags,
        },
    ).fetchall()
    players: dict[int, dict[str, Any]] = {}
    for row in rows:
        player = players.setdefault(int(row[0]), {
            "tag": _text(row[1]),
            "reset_position": None if row[2] is None else int(row[2]),
            "season_weeks": [],
            "days": [],
        })
        if row[3] is None:
            continue
        attacks = None if row[4] is None else int(row[4])
        defenses = None if row[5] is None else int(row[5])
        reasons = _json_array(row[8])
        result = _shown_total(row[6], _text(row[7]), attacks, defenses, reasons)
        player["days"].append([
            (row[3] - day.season_start) // RANKED_DAY_DURATION + 1,
            attacks,
            defenses,
            result,
            attacks == 0 and defenses == 0 and result == 0
            and row[9] == SEASON_START_TROPHIES,
            "player_not_eligible" not in reasons or "not_enrolled" in reasons
            or bool(attacks) or bool(defenses),
        ])
    for player_id, week_start, league, entry in connection.execute(
        """
        SELECT player_id, week_start, league, entry FROM player_league_weeks
        WHERE player_id = ANY(%s) AND week_start >= %s AND week_start < %s
        ORDER BY player_id, week_start
        """,
        (list(players), day.season_start, day.season_start + SEASON_DURATION),
    ).fetchall():
        players[int(player_id)]["season_weeks"].append({
            "week_start": week_start.astimezone(UTC).isoformat(),
            "league": _text(league),
            "entry": None if entry is None else _text(entry),
        })
    return list(players.values())


def _reset_entries(
    connection: Any, tags: list[str], now: datetime
) -> dict[str, tuple[int | None, int | None, list[dict[str, Any]]]]:
    """Each tag's place on the newest Daily board published at the last
    Reset, its net so far today, and each of the last 7 finished Legend days
    by its start, oldest first, with that day's result or None.

    Today's net and each day's result come from the day's newest saved log,
    as the dashboard's Legend day card and the player page's Daily Legend
    log show them, so neither counts the automatic defense loss a Reset
    reading was taken before: today starts from the day's settled start.
    Only the current Season counts: on its first Legend day there is no
    place to compare with and no finished day. One read, of only these
    tags' rows."""
    if not tags:
        return {}
    board, place = _reset_place_sql(connection)
    day = ranked_day_for(now)
    window = [
        start
        for back in range(RECENT_DAYS, 0, -1)
        if (start := day.start - back * RANKED_DAY_DURATION) >= day.season_start
    ]
    rows = connection.execute(
        f"""
        {board}
        SELECT player.normalized_tag, place.position, daily.ranked_day_start,
               daily.attack_count, daily.attack_gain, daily.defense_count,
               daily.defense_loss, daily.net_trophy_change, daily.coverage,
               daily.partial_reasons
        FROM players AS player
        {place}
        LEFT JOIN LATERAL (
            SELECT DISTINCT ON (saved.ranked_day_start)
                   saved.ranked_day_start, saved.attack_count, saved.attack_gain,
                   saved.defense_count, saved.defense_loss,
                   saved.net_trophy_change, saved.coverage, saved.partial_reasons
            FROM api_player_daily_logs AS saved
            WHERE saved.player_id = player.id
              AND saved.ranked_day_start BETWEEN %(first)s AND %(reset)s
            ORDER BY saved.ranked_day_start DESC, saved.version DESC
        ) AS daily ON true
        WHERE player.normalized_tag = ANY(%(tags)s)
        """,
        {
            "first": window[0] if window else day.start,
            "reset": day.start,
            "season_id": day.official_season_id,
            "tags": tags,
        },
    ).fetchall()
    places: dict[str, int] = {}
    logs: dict[tuple[str, datetime], Any] = {}
    for row in rows:
        if row[1] is not None:
            places[_text(row[0])] = int(row[1])
        if row[2] is not None:
            logs[_text(row[0]), row[2]] = row
    result: dict[str, tuple[int | None, int | None, list[dict[str, Any]]]] = {}
    for tag in tags:
        today = logs.get((tag, day.start))
        days = []
        for start in window:
            log = logs.get((tag, start))
            days.append({
                "ranked_day_start": start.isoformat(),
                "net_trophy_change": None if log is None else _shown_total(
                    log[7], _text(log[8]), log[3], log[5], _json_array(log[9])),
            })
        result[tag] = (
            places.get(tag),
            None if today is None else _net_so_far(*today[3:7], _json_array(today[9])),
            days,
        )
    return result


def get_frozen_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    official_season_id: str | None = None,
    season_day_number: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    if (official_season_id is None) != (season_day_number is None):
        raise ValueError("season and day must be supplied together")
    now = datetime.now(UTC) if now is None else now
    with database.pool.connection() as connection:
        publications = _frozen_publications()
        locator = connection.execute(
            f"""
            WITH publications AS ({publications}), selected AS (
                SELECT * FROM publications
                WHERE (%s::text IS NULL OR
                       (official_season_id = %s AND season_day_number = %s))
                ORDER BY boundary_at DESC, (source = 'v2') DESC,
                         version DESC, generation DESC, id DESC LIMIT 1
            )
            SELECT selected.source, selected.id,
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at < selected.boundary_at
                    ORDER BY p.boundary_at DESC, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1),
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at > selected.boundary_at
                    ORDER BY p.boundary_at, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1)
            FROM selected
            """,
            (official_season_id, official_season_id, season_day_number),
        ).fetchone()
        if locator is None:
            return None
        snapshot = None
        if _text(locator[0]) == "v2":
            snapshot_query = """
                SELECT snapshot.id, snapshot.boundary_at, snapshot.version,
                       snapshot.ordering_rule_version,
                       snapshot.freshness_rule_version,
                       snapshot.measured_coverage,
                       snapshot.eligible_population_count,
                       snapshot.included_entry_count, snapshot.stale_entry_count,
                       snapshot.fresh_entry_count, snapshot.excluded_missing_count,
                       snapshot.excluded_invalid_count,
                       snapshot.excluded_malformed_count,
                       snapshot.excluded_conflicting_count,
                       COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
                       COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number,
                       (SELECT max(entry.profile_observed_at)
                        FROM leaderboard_snapshot_entries AS entry
                        WHERE entry.snapshot_id = snapshot.id)
                FROM leaderboard_snapshots AS snapshot
                LEFT JOIN ranked_day_versions AS source_day
                  ON source_day.id = snapshot.source_ranked_day_version_id
                LEFT JOIN boundary_publication_generations AS generation
                  ON generation.snapshot_id = snapshot.id
                LEFT JOIN LATERAL (
                    -- A proof frozen since 0081 records its newest ranked day;
                    -- an older one stores all its rows, so they give it.
                    SELECT ranked.official_season_id, ranked.season_day_number
                    FROM boundary_publication_manifests AS manifest
                    JOIN ranked_day_versions AS ranked
                      ON ranked.id = COALESCE(
                          manifest.newest_ranked_day_version_id,
                          (SELECT member.ranked_day_version_id
                           FROM boundary_publication_manifest_rows AS member
                           WHERE member.manifest_id = manifest.id
                             AND member.ranked_day_version_id IS NOT NULL
                           ORDER BY member.ranked_day_version_id DESC LIMIT 1))
                    WHERE manifest.id = generation.snapshot_manifest_id
                ) AS generation_day ON true
                WHERE snapshot.id = %s
                  AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
                ORDER BY generation.generation DESC NULLS LAST
                LIMIT 1
            """
            snapshot = connection.execute(snapshot_query, (locator[1],)).fetchone()
            assert snapshot is not None
            snapshot = (*snapshot, locator[2], locator[3])
        if snapshot is None:
            legacy_row = connection.execute(
                """
                SELECT leaderboard.id, leaderboard.public_id,
                       leaderboard.boundary_at, leaderboard.version,
                       leaderboard.ordering_rule_version, leaderboard.coverage,
                       selector.official_season_id, selector.season_day_number,
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id),
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id
                          AND e.freshness = 'stale'),
                       (SELECT max(e.observed_at) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id)
                FROM api_frozen_leaderboards AS leaderboard
                JOIN LATERAL (
                    SELECT day.official_season_id, day.season_day_number
                    FROM ranked_day_versions AS day
                    WHERE day.ranked_day_end = leaderboard.boundary_at
                    ORDER BY day.id DESC LIMIT 1
                ) AS selector ON true
                WHERE leaderboard.id = %s
                """,
                (locator[1],),
            ).fetchone()
            assert legacy_row is not None
            legacy = (*legacy_row[:8], locator[2], locator[3], *legacy_row[8:])
            total_entries = int(legacy[10])
            if offset and offset >= total_entries:
                return None
            rows = connection.execute(
                """
                SELECT entry.position, player.normalized_tag, profile.name,
                       profile.profile_json -> 'clan' ->> 'name',
                       entry.trophies, entry.observed_at, entry.freshness,
                       entry.confidence, entry.official_rank,
                       EXISTS (
                           SELECT FROM api_frozen_leaderboard_entries AS other
                           WHERE other.leaderboard_id = entry.leaderboard_id
                             AND other.position IN (entry.position - 1,
                                                    entry.position + 1)
                             AND other.trophies = entry.trophies
                       )
                FROM api_frozen_leaderboard_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                LEFT JOIN player_profile_versions AS profile
                  ON profile.id = player.current_profile_version_id
                WHERE entry.leaderboard_id = %s
                ORDER BY entry.position LIMIT %s OFFSET %s
                """,
                (legacy[0], limit, offset),
            ).fetchall()
            boundary_at = legacy[2].astimezone(UTC)
            newest_input_at = (legacy[12] or boundary_at).astimezone(UTC)
            season_start = boundary_at - timedelta(days=int(legacy[7]))
            season_end = season_start + timedelta(days=28)
            coverage = dict(legacy[5])
            measured = float(coverage.get("measured", 0))
            population = int(coverage.get("eligible_population", total_entries))
            page_count = (total_entries + limit - 1) // limit
            page = offset // limit + 1
            return {
                "kind": "frozen",
                "snapshot_id": str(legacy[1]),
                "boundary_at": boundary_at.isoformat(),
                "reset_at": boundary_at.isoformat(),
                "official_season_id": _text(legacy[6]),
                "season_day_number": int(legacy[7]),
                "season_start_at": season_start.isoformat(),
                "season_end_at": season_end.isoformat(),
                "previous_snapshot": legacy[8],
                "next_snapshot": legacy[9],
                "generated_at": boundary_at.isoformat(),
                "version": int(legacy[3]),
                "ordering_rule_version": _text(legacy[4]),
                "tracked_population": population,
                "total_entries": total_entries,
                "page": page,
                "page_size": limit,
                "page_count": page_count,
                "has_previous": page > 1,
                "has_next": page < page_count,
                "coverage": {
                    "state": "partial",
                    "tracked_players": population,
                    "measured_percent": measured * 100
                    if measured <= 1
                    else measured,
                    "note": "Published frozen snapshot coverage is measured from its accepted population.",
                },
                "provenance": {
                    "source": "published frozen leaderboard snapshot",
                    "observed_at": newest_input_at.isoformat(),
                    "freshness": "stale" if int(legacy[11]) else "fresh",
                    "confidence": "partial",
                    "coverage": "partial",
                    "version": _text(legacy[4]),
                },
                "quality_states": ["partial"]
                + (["stale"] if int(legacy[11]) else []),
                "entries": [
                    {
                        "position": int(row[0]),
                        "tag": _text(row[1]),
                        "name": None if row[2] is None else _text(row[2]),
                        "clan": None if row[3] is None else _text(row[3]),
                        "trophies": int(row[4]),
                        "observed_at": row[5].astimezone(UTC).isoformat(),
                        "age_seconds": max(
                            0,
                            int(
                                (
                                    now.astimezone(UTC) - row[5].astimezone(UTC)
                                ).total_seconds()
                            ),
                        ),
                        "freshness": _text(row[6]),
                        "confidence": _text(row[7]),
                        "public_confidence": _public_snapshot_confidence(
                            _text(row[7])
                        ),
                        "official_rank": None if row[8] is None else int(row[8]),
                        "tied": bool(row[9]),
                    }
                    for row in rows
                ],
            }
        total_entries = int(snapshot[7])
        if offset and offset >= total_entries:
            return None
        profile_identity_clause = (
            """
            version.id = entry.profile_version_id
               OR (entry.profile_version_id IS NULL
                   AND version.observation_id = entry.profile_observation_id)
            """
        )
        rows = connection.execute(
            f"""
            SELECT entry.position, player.normalized_tag, profile.name,
                   entry.trophies, entry.profile_observed_at,
                   entry.profile_freshness, entry.profile_confidence,
                   entry.official_rank, profile.clan,
                   -- Equal trophies sit next to each other on the board.
                   EXISTS (
                       SELECT FROM leaderboard_snapshot_entries AS other
                       WHERE other.snapshot_id = entry.snapshot_id
                         AND other.position IN (entry.position - 1,
                                                entry.position + 1)
                         AND other.trophies = entry.trophies
                   )
            FROM leaderboard_snapshot_entries AS entry
            JOIN players AS player ON player.id = entry.player_id
            LEFT JOIN LATERAL (
                SELECT version.name, version.profile_json -> 'clan' ->> 'name' AS clan
                FROM player_profile_versions AS version
                WHERE {profile_identity_clause}
                ORDER BY version.id DESC LIMIT 1
            ) AS profile ON true
            WHERE entry.snapshot_id = %s
            ORDER BY entry.position LIMIT %s OFFSET %s
            """,
            (snapshot[0], limit, offset),
        ).fetchall()
        boundary_at = snapshot[1].astimezone(UTC)
        newest_input_at = (snapshot[16] or boundary_at).astimezone(UTC)
        season_start = boundary_at - timedelta(days=int(snapshot[15]))
        season_end = season_start + timedelta(days=28)
        measured = float(snapshot[5])
        page_count = (total_entries + limit - 1) // limit
        page = offset // limit + 1
        return {
            "kind": "frozen",
            "snapshot_id": str(snapshot[0]),
            "boundary_at": boundary_at.isoformat(),
            "reset_at": boundary_at.isoformat(),
            "official_season_id": _text(snapshot[14]),
            "season_day_number": int(snapshot[15]),
            "season_start_at": season_start.isoformat(),
            "season_end_at": season_end.isoformat(),
            "previous_snapshot": snapshot[17],
            "next_snapshot": snapshot[18],
            "generated_at": boundary_at.isoformat(),
            "version": int(snapshot[2]),
            "ordering_rule_version": _text(snapshot[3]),
            "tracked_population": int(snapshot[6]),
            "total_entries": total_entries,
            "page": page,
            "page_size": limit,
            "page_count": page_count,
            "has_previous": page > 1,
            "has_next": page < page_count,
            "coverage": {
                "state": "partial",
                "tracked_players": int(snapshot[6]),
                "measured_percent": measured * 100 if measured <= 1 else measured,
                "note": "Published frozen snapshot coverage is measured from its accepted population.",
            },
            "provenance": {
                "source": "published frozen leaderboard snapshot",
                "observed_at": newest_input_at.isoformat(),
                "freshness": "stale" if int(snapshot[8]) else "fresh",
                "confidence": "partial",
                "coverage": "partial",
                "version": _text(snapshot[3]),
            },
            "quality_states": ["partial"] + (["stale"] if int(snapshot[8]) else []),
            "entries": [
                {
                    "position": int(row[0]),
                    "tag": _text(row[1]),
                    "name": None if row[2] is None else _text(row[2]),
                    "trophies": int(row[3]),
                    "observed_at": row[4].astimezone(UTC).isoformat(),
                    "age_seconds": max(
                        0,
                        int(
                            (
                                now.astimezone(UTC) - row[4].astimezone(UTC)
                            ).total_seconds()
                        ),
                    ),
                    "freshness": _text(row[5]),
                    "confidence": _text(row[6]),
                    "public_confidence": _public_snapshot_confidence(_text(row[6])),
                    "official_rank": None if row[7] is None else int(row[7]),
                    "clan": None if row[8] is None else _text(row[8]),
                    "tied": bool(row[9]),
                }
                for row in rows
            ],
        }
