"""Pull the game's own ranking lists around each Reset and score the Daily board.

With ``CLASHLENS_GAME_RANKINGS_ENABLED=true`` the collector pulls, for each
Reset R, the global top 200 at R+30 s, then every 3 minutes until its
``previousRank`` differs from that early pull's, then every country's top 200
(migration 0099). Untracked Legend I players in country lists are saved as due
a discovery check. From R+2 h each newly published frozen Daily board for R is
scored against the pulls: S1-S6 from the 11 Oct 2026 blind review, ``unknown``
whenever a pull failed or is missing. Schedule, storage and the score's
meaning: docs/collector-polling.md, "The game's ranking lists, once a day".
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import os
import time
from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .collector_http import FetchedResponse, ProviderFailure
from .domain import ranked_day_for
from .profile import LEGEND_I_TIER_ID, ProfileParseError, normalize_player_tag

if TYPE_CHECKING:
    from .collector import Collector
    from .collector_db import CollectorDatabase

EARLY_AT = timedelta(seconds=30)
EARLY_UNTIL = timedelta(minutes=5)
POLL_FROM = timedelta(minutes=3)
POLL_EVERY = timedelta(minutes=3)
POLL_UNTIL = timedelta(hours=1)
SCORE_FROM = timedelta(hours=2)
RETRY_AFTER = timedelta(minutes=2)
EARLY_RETRY_AFTER = timedelta(minutes=1)
LOCATIONS_EVERY = timedelta(days=7)
KEEP_PULLS = timedelta(days=28)
SWITCHED_SHARE = 0.25
REQUEST_SECONDS = 0.5
TICK_SECONDS = 15.0
# How often published boards are checked for a new version to score.
SCORE_SECONDS = 600.0
_DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Row:
    tag: str
    trophies: int
    attack_wins: int
    previous_rank: int | None
    legend_i: bool


@dataclass(frozen=True, slots=True)
class Pull:
    purpose: str
    location_id: int | None
    requested_at: datetime
    failure: str | None
    body: bytes | None


def reset_at(now: datetime) -> datetime:
    """The latest Reset at or before ``now``."""
    boundary = now.astimezone(UTC).replace(hour=5, minute=0, second=0, microsecond=0)
    return boundary if boundary <= now else boundary - _DAY


def read_rows(body: bytes) -> list[Row]:
    """A ranking list's rows; ValueError when any row cannot be read."""
    items = json.loads(body)["items"]
    rows = []
    for item in items:
        try:
            tag = normalize_player_tag(item["tag"])
        except ProfileParseError as error:
            raise ValueError("unreadable tag") from error
        trophies, wins, previous = item["trophies"], item["attackWins"], item["previousRank"]
        if not all(type(value) is int for value in (trophies, wins, previous)):
            raise ValueError("unreadable row")
        tier = (item.get("leagueTier") or {}).get("id")
        rows.append(Row(tag, trophies, wins, previous if previous > 0 else None,
                        tier == LEGEND_I_TIER_ID))
    return rows


def read_countries(body: bytes) -> list[tuple[int, str]]:
    """The game's countries as (location ID, country code)."""
    return sorted(
        (item["id"], item["countryCode"])
        for item in json.loads(body)["items"]
        if item.get("isCountry") is True
    )


def switched(rows: list[Row], reference: list[Row]) -> bool:
    """Whether ``rows`` hold a newer Reset's ranks than ``reference``."""
    before = {row.tag: row.previous_rank for row in reference}
    common = [row for row in rows if row.tag in before]
    changed = sum(row.previous_rank != before[row.tag] for row in common)
    return bool(common) and changed >= SWITCHED_SHARE * len(common)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 2) if whole else None


def score(
    board: dict[str, tuple[int, int, str]],
    early: list[Row] | None,
    reset: list[Row] | None,
    countries: dict[int, list[Row]],
    stuck: set[str],
) -> dict[str, Any]:
    """Score one board, ``{tag: (position, trophies, confidence)}``, against the pulls.

    ``stuck`` are the game's stuck 5,000 rows; they and tags in two country
    lists are left out of every number and listed instead.
    """
    value = {tag: entry[1] for tag, entry in board.items()}
    position = {tag: entry[0] for tag, entry in board.items()}
    confirmed = {tag for tag, entry in board.items() if entry[2] == "confirmed"}
    seen = Counter(row.tag for rows in countries.values() for row in rows)
    twice = {tag for tag, count in seen.items() if count > 1}
    faults = stuck | twice
    countries = {location: [row for row in rows if row.tag not in faults]
                 for location, rows in countries.items()}
    early = [row for row in early or [] if row.tag not in faults]
    reset = [row for row in reset or [] if row.tag not in faults]
    pairs = breaks = tied = tie_agree = 0
    contradicted: set[str] = set()
    for rows in countries.values():
        ranked = sorted(
            (row for row in rows if row.legend_i and row.tag in board and row.previous_rank),
            key=lambda row: row.previous_rank,
        )
        for index, higher in enumerate(ranked):
            for lower in ranked[index + 1:]:
                pairs += 1
                if value[higher.tag] < value[lower.tag]:
                    breaks += 1
                    contradicted |= {higher.tag, lower.tag} & confirmed
                elif value[higher.tag] == value[lower.tag]:
                    tied += 1
                    tie_agree += position[higher.tag] < position[lower.tag]
    legend_i = {row.tag for rows in countries.values() for row in rows if row.legend_i}
    missing_active = sorted({
        row.tag
        for rows in countries.values()
        for row in rows
        if row.legend_i and row.tag not in board
        and not (row.trophies == 5000 and row.attack_wins == 0)
    })
    ordered = sorted(value.values())
    global_ranked = [row for row in reset if row.previous_rank]
    inside = exact = 0
    for row in global_ranked:
        if row.tag not in board:
            continue
        above = len(ordered) - bisect_right(ordered, value[row.tag])
        same = bisect_right(ordered, value[row.tag]) - bisect_left(ordered, value[row.tag])
        inside += above < row.previous_rank <= above + same
        exact += position[row.tag] == row.previous_rank
    uncertain = len(board) - len(confirmed)
    return {
        "s1_country_order_breaks_per_100k": round(1e5 * breaks / pairs, 1) if pairs else None,
        "country_pairs": pairs,
        "country_order_breaks": breaks,
        "s2_global_inside_trophy_group": inside,
        "global_ranked": len(global_ranked),
        "global_on_board": sum(row.tag in board for row in reset),
        "s3_confirmed_entries_contradicted": len(contradicted),
        "s4_active_legend_i_missing": len(missing_active),
        "s4_tags": missing_active,
        "s5_legend_i_on_board_percent": _percent(len(legend_i & board.keys()), len(legend_i)),
        "legend_i_in_country_lists": len(legend_i),
        "s6_uncertain_percent": _percent(uncertain, len(board)),
        "uncertain_entries": uncertain,
        "board_entries": len(board),
        "global_exact_rank": exact,
        "tied_pairs": tied,
        "tie_order_agreement_percent": _percent(tie_agree, tied),
        "early_on_board": sum(row.tag in board for row in early),
        "early_trophies_equal": sum(
            row.tag in board and row.trophies == value[row.tag] for row in early
        ),
        "game_faults": {"stuck_5000": sorted(stuck), "tag_in_two_countries": sorted(twice)},
    }


def load_pulls(
    database: CollectorDatabase, boundary: datetime, bodies: tuple[str, ...]
) -> list[Pull]:
    """R's pulls and the latest locations pull before R+2 h, oldest first.

    Only pulls whose purpose is in ``bodies`` carry their raw bytes.
    """
    with database._connection() as connection:
        rows = connection.execute(
            """
            SELECT purpose, location_id, requested_at, failure,
                   CASE WHEN purpose = ANY(%(bodies)s) THEN body_gzip END
            FROM game_ranking_pulls WHERE boundary_at = %(boundary)s
            UNION ALL (
                SELECT purpose, location_id, requested_at, failure,
                       CASE WHEN purpose = ANY(%(bodies)s) THEN body_gzip END
                FROM game_ranking_pulls
                WHERE purpose = 'locations' AND failure IS NULL AND requested_at < %(until)s
                  AND boundary_at <> %(boundary)s
                ORDER BY requested_at DESC LIMIT 1
            )
            ORDER BY requested_at
            """,
            {"boundary": boundary, "until": boundary + SCORE_FROM, "bodies": list(bodies)},
        ).fetchall()
    return [
        Pull(row[0], row[1], row[2], row[3],
             None if row[4] is None else gzip.decompress(row[4]))
        for row in rows
    ]


def _latest(pulls: list[Pull], purpose: str, location_id: int | None = None) -> Pull | None:
    found = [
        pull for pull in pulls
        if pull.purpose == purpose and pull.failure is None and pull.location_id == location_id
    ]
    return found[-1] if found else None


def _last_try(pulls: list[Pull], purpose: str, location_id: int | None = None) -> datetime | None:
    tries = [pull.requested_at for pull in pulls
             if pull.purpose == purpose and pull.location_id == location_id]
    return max(tries, default=None)


def save_pull(
    database: CollectorDatabase,
    boundary: datetime,
    purpose: str,
    location: tuple[int, str] | None,
    requested_at: datetime,
    response: FetchedResponse | None,
    failure: str | None,
    *,
    discover: list[Row] = (),
) -> int:
    """Save one pull and hand its untracked Legend I players to discovery; returns how many were added."""
    body = None if response is None else response.body
    added = 0
    with database._connection() as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO game_ranking_pulls (
                boundary_at, purpose, location_id, country_code, requested_at,
                completed_at, http_status, failure, body_sha256,
                body_bytes, body_gzip
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                boundary, purpose,
                None if location is None else location[0],
                None if location is None else location[1],
                requested_at,
                datetime.now(UTC) if response is None else response.response_completed_at,
                None if response is None else response.http_status,
                failure,
                None if body is None else hashlib.sha256(body).hexdigest(),
                None if body is None else len(body),
                None if body is None else gzip.compress(body, mtime=0),
            ),
        )
        tags = sorted({row.tag for row in discover if row.legend_i})
        tracked = {
            str(tag) for (tag,) in connection.execute(
                "SELECT normalized_tag FROM players WHERE normalized_tag = ANY(%s) AND active",
                (tags,),
            )
        }
        for tag in tags:
            if tag not in tracked:
                added += connection.execute(
                    "SELECT added FROM clashlens_queue_promoted_player(%s)", (tag,)
                ).fetchone()[0]
    return added


def _prune(database: CollectorDatabase, now: datetime) -> int:
    with database._connection() as connection, connection.transaction():
        return connection.execute(
            "DELETE FROM game_ranking_pulls WHERE boundary_at < %s"
            " AND NOT (purpose = 'locations' AND id = (SELECT id FROM game_ranking_pulls"
            " WHERE purpose = 'locations' AND failure IS NULL ORDER BY requested_at DESC LIMIT 1))",
            (reset_at(now) - KEEP_PULLS,),
        ).rowcount


def score_day(database: CollectorDatabase, boundary: datetime) -> dict[str, Any] | None:
    """Score R's published board once per board version; None when there is nothing new."""
    with database._connection() as connection:
        board_rows = connection.execute(
            "SELECT * FROM clashlens_game_ranking_board(%s)", (boundary,)
        ).fetchall()
        if not board_rows or connection.execute(
            "SELECT 1 FROM game_ranking_scores WHERE boundary_at = %s AND snapshot_id = %s",
            (boundary, board_rows[0][0]),
        ).fetchone():
            return None
    pulls = load_pulls(
        database, boundary, ("global_early", "global_reset", "locations", "country")
    )
    reasons = []
    early, reset, locations = (_latest(pulls, purpose)
                               for purpose in ("global_early", "global_reset", "locations"))
    if early is None:
        reasons.append("global_early_missing")
    if reset is None:
        reasons.append("previous_rank_not_switched" if _last_try(pulls, "global_poll")
                       else "global_reset_missing")
    expected = [] if locations is None else read_countries(locations.body)
    if locations is None:
        reasons.append("locations_missing")
    countries = {}
    for location_id, _code in expected:
        pull = _latest(pulls, "country", location_id)
        if pull is not None:
            countries[location_id] = read_rows(pull.body)
    if len(countries) < len(expected):
        reasons.append(f"countries_missing:{len(expected) - len(countries)}")
    board = {str(row[2]): (int(row[3]), int(row[4]), str(row[5])) for row in board_rows}
    # A stuck 5,000 row: the list shows 5,000 trophies and 0 attack wins for a
    # player whose own profile showed other trophies earlier this Season.
    at_5000 = sorted({row.tag for rows in countries.values() for row in rows
                      if row.legend_i and row.trophies == 5000 and row.attack_wins == 0})
    day = ranked_day_for(boundary)
    with database._connection() as connection:
        stuck = {str(tag) for (tag,) in connection.execute(
            "SELECT * FROM clashlens_game_ranking_season_battlers(%s, %s, %s, %s)",
            (at_5000, day.official_season_id, day.season_start, boundary),
        )}
    country_times = [pull.requested_at for pull in pulls if pull.purpose == "country"]
    result = {
        "unknown_reasons": reasons,
        "global_early_at": None if early is None else _utc(early.requested_at),
        "global_reset_at": None if reset is None else _utc(reset.requested_at),
        "countries_expected": len(expected),
        "countries_pulled": len(countries),
        "first_country_at": _utc(min(country_times)) if country_times else None,
        "last_country_at": _utc(max(country_times)) if country_times else None,
        **score(
            board,
            None if early is None else read_rows(early.body),
            None if reset is None else read_rows(reset.body),
            countries,
            stuck,
        ),
    }
    with database._connection() as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO game_ranking_scores (boundary_at, snapshot_id, snapshot_version, status, score)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (boundary_at, snapshot_id) DO NOTHING
            """,
            (boundary, board_rows[0][0], board_rows[0][1],
             "unknown" if reasons else "scored", json.dumps(result, sort_keys=True)),
        )
    return {"boundary_at": boundary.isoformat(), "snapshot_version": board_rows[0][1],
            "status": "unknown" if reasons else "scored", "unknown_reasons": reasons}


def _days_to_score(database: CollectorDatabase, now: datetime) -> list[datetime]:
    """Resets past R+2 h whose pulls are still kept, since pulls began; scoring each is idempotent."""
    with database._connection() as connection:
        first = connection.execute("SELECT min(boundary_at) FROM game_ranking_pulls").fetchone()[0]
    if first is None:
        return []
    days = [reset_at(now) - _DAY * back for back in range(KEEP_PULLS // _DAY)]
    return [day for day in days if day >= first and now >= day + SCORE_FROM]


class _Puller:
    """Paced pulls on the regular keys, one at a time."""

    def __init__(self, collector: Collector, clock: Callable[[], datetime]) -> None:
        self.collector = collector
        self.clock = clock
        self._next_start = 0.0

    async def fetch(
        self, location: str | None
    ) -> tuple[datetime, FetchedResponse | None, str | None]:
        delay = self._next_start - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self._next_start = time.monotonic() + REQUEST_SECONDS
        client, keys = self.collector.client, self.collector.regular_keys
        attempted_at = self.clock()
        try:
            response = await (client.fetch_locations(keys) if location is None
                              else client.fetch_rankings(keys, location))
        except ProviderFailure as error:
            return attempted_at, None, error.category
        requested_at = response.request_started_at
        if response.http_status != 200:
            return requested_at, response, f"http_{response.http_status}"
        try:
            (read_rows if location else read_countries)(response.body)
        except (ValueError, KeyError, TypeError):
            return requested_at, response, "unreadable"
        return requested_at, response, None


async def pull_due(collector: Collector, puller: _Puller) -> Counter[str]:
    """Make every pull due now for the current Reset; returns counts for the log."""
    now = puller.clock()
    boundary = reset_at(now)
    database = collector.database
    pulls = await collector._database_call(
        load_pulls, database, boundary, ("global_early", "locations")
    )
    counts: Counter[str] = Counter()

    async def pull(
        purpose: str,
        location: tuple[int, str] | None,
        judge: Callable[[list[Row]], bool] | None = None,
    ) -> None:
        target = None if purpose == "locations" else "global" if location is None else str(location[0])
        requested_at, response, failure = await puller.fetch(target)
        rows = None if failure or target is None else read_rows(response.body)
        if judge is not None and rows is not None and judge(rows):
            purpose = "global_reset"
        counts["added_to_discovery"] += await collector._database_call(
            save_pull, database, boundary, purpose, location, requested_at, response, failure,
            discover=rows if purpose == "country" and rows else [],
        )
        counts[purpose if failure is None else "failed"] += 1
        pulls.append(Pull(purpose, None if location is None else location[0], requested_at,
                          failure, None if response is None else response.body))

    def due(purpose: str, location_id: int | None, gap: timedelta) -> bool:
        last = _last_try(pulls, purpose, location_id)
        return _latest(pulls, purpose, location_id) is None and (last is None or now - last >= gap)

    if boundary + EARLY_AT <= now < boundary + EARLY_UNTIL and due(
        "global_early", None, EARLY_RETRY_AFTER
    ):
        await pull("global_early", None)
    early = _latest(pulls, "global_early")
    if (
        early is not None
        and _latest(pulls, "global_reset") is None
        and boundary + POLL_FROM <= now < boundary + POLL_UNTIL
        and now - (_last_try(pulls, "global_poll") or boundary) >= POLL_EVERY
    ):
        reference = read_rows(early.body)
        await pull("global_poll", None, judge=lambda rows: switched(rows, reference))
    if _latest(pulls, "global_reset") is None or now >= boundary + SCORE_FROM:
        return counts
    locations = _latest(pulls, "locations")
    last = _last_try(pulls, "locations")
    if (locations is None or now - locations.requested_at >= LOCATIONS_EVERY) and (
        last is None or now - last >= RETRY_AFTER
    ):
        await pull("locations", None)
        locations = _latest(pulls, "locations") or locations
    if locations is None:
        return counts
    for location in read_countries(locations.body):
        if collector._stopping.is_set():
            break
        if due("country", location[0], RETRY_AFTER):
            await pull("country", location)
    return counts


async def run(collector: Collector, stop_requested: asyncio.Event) -> None:
    """Pull, score and prune until stopped; never ends the collector early."""
    if os.environ.get("CLASHLENS_GAME_RANKINGS_ENABLED", "false") != "true":
        await stop_requested.wait()
        return
    puller = _Puller(collector, lambda: datetime.now(UTC))
    next_score = 0.0
    while not stop_requested.is_set():
        event: dict[str, Any] = {"event": "game_rankings"}
        try:
            counts = await pull_due(collector, puller)
            if counts:
                print(json.dumps({**event, "status": "pulled", **counts}), flush=True)
            if time.monotonic() >= next_score:
                next_score = time.monotonic() + SCORE_SECONDS
                now = puller.clock()
                for day in await collector._database_call(_days_to_score, collector.database, now):
                    scored = await collector._database_call(score_day, collector.database, day)
                    if scored:
                        print(json.dumps({**event, **scored}), flush=True)
                pruned = await collector._database_call(_prune, collector.database, now)
                if pruned:
                    print(json.dumps({**event, "status": "pruned", "pulls": pruned}), flush=True)
        except Exception as error:  # noqa: BLE001 - tried again next tick
            print(json.dumps({**event, "status": "failed", "error": repr(error)[:300]}), flush=True)
        try:
            await asyncio.wait_for(stop_requested.wait(), timeout=TICK_SECONDS)
        except TimeoutError:
            pass


def add_command(
    subparsers: Any, database_argument: Callable[[argparse.ArgumentParser], None]
) -> None:
    """Add the ``game-ranking-score`` command to the CLI."""
    command = subparsers.add_parser(
        "game-ranking-score",
        help="print the Daily board's score against the game's ranking lists (collector database role)",
    )
    database_argument(command)
    command.add_argument("--days", type=int, default=14, help="how many recent Resets to print")


def run_command(database_url: str, *, days: int) -> int:
    """Print one JSON line per recent Reset: its latest board version's score."""
    import psycopg

    with psycopg.connect(database_url) as connection:
        connection.read_only = True
        rows = connection.execute(
            """
            SELECT DISTINCT ON (boundary_at) boundary_at, snapshot_version, status, scored_at, score
            FROM game_ranking_scores WHERE boundary_at >= %s
            ORDER BY boundary_at DESC, snapshot_version DESC, id DESC
            """,
            (reset_at(datetime.now(UTC)) - _DAY * (days - 1),),
        ).fetchall()
    for boundary, version, status, scored_at, result in rows:
        print(json.dumps({"reset": _utc(boundary), "board_version": version,
                          "status": status, "scored_at": _utc(scored_at), **result},
                         sort_keys=True))
    return 0
