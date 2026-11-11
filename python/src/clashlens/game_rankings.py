"""Pull the game's own ranking lists around each Reset and score the Daily board.

With ``CLASHLENS_GAME_RANKINGS_ENABLED=true`` the collector pulls, for each
Reset R, the global top 200 at R+30 s, then every 3 minutes until its
``previousRank`` differs from that early pull's, then every country's top 200
(migration 0099), each again until it too has switched. An early pull that already holds R's ranks is R's list. Untracked Legend I players in country lists are saved as due
a discovery check. From R+2 h each newly published frozen Daily board for R is
scored against the pulls: S1-S6 from the 11 Oct 2026 blind review, ``unknown``
whenever a pull failed or is missing. Schedule, storage and the score's
meaning: docs/collector-polling.md, "The game's ranking lists, once a day".
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
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
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NamedTuple

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
# A country list with fewer rows to compare than this is too small to tell
# whether it has switched; it is scored as it comes.
MIN_SWITCH_ROWS = 10
# A country pull's failure when its previousRank still holds the Reset before.
NOT_SWITCHED = "not_switched"
REQUEST_SECONDS = 0.5
TICK_SECONDS = 15.0
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
    """Whether ``rows`` hold a newer Reset's ranks than ``reference``; unknown ranks are no evidence."""
    before = {row.tag: row.previous_rank for row in reference if row.previous_rank}
    common = [row for row in rows if row.previous_rank and row.tag in before]
    changed = sum(row.previous_rank != before[row.tag] for row in common)
    return bool(common) and changed >= SWITCHED_SHARE * len(common)


def holds_reset_ranks(rows: list[Row]) -> bool:
    """Whether a list read just after a Reset already gives that Reset's
    ranks as ``previousRank``: fewer than a quarter of its players have
    moved from them. Before the game switches, ``previousRank`` holds the
    Reset before, a day of battles away. On 12 Oct 2026 the 05:00:35 pull
    gave all 200 players their own rank, so no later poll differed from it
    and no country list was pulled."""
    ranked = [(position, row.previous_rank)
              for position, row in enumerate(rows, 1) if row.previous_rank]
    moved = sum(position != previous for position, previous in ranked)
    return bool(ranked) and moved < SWITCHED_SHARE * len(ranked)


def country_switched(rows: list[Row], before: list[Row] | None) -> bool | None:
    """Whether a country list gives this Reset's ranks as ``previousRank``,
    judged against ``before``, the same country's list from the Reset before;
    None when fewer than MIN_SWITCH_ROWS rows can be compared.

    The game switches countries one by one after the Reset: on 10 Nov 2026
    Austria (05:00:38) and Bangladesh (05:00:40) still gave every player
    the 9 November rank, and on 11 Nov the ten lists pulled first did. A list
    that has not switched gives the ranks ``before`` gave, or, when ``before``
    had not switched either, the order ``before`` was in.
    """
    ranked = [row for row in rows if row.previous_rank]
    if len(ranked) < MIN_SWITCH_ROWS:
        return None
    if holds_reset_ranks(rows):
        return True
    known = {row.tag for row in before or [] if row.previous_rank}
    if sum(row.tag in known for row in ranked) < MIN_SWITCH_ROWS:
        return None
    by_position = [dataclasses.replace(row, previous_rank=position)
                   for position, row in enumerate(before, 1)]
    return switched(rows, before) and switched(rows, by_position)


def _reset_pull(pulls: list[Pull]) -> Pull | None:
    """R's global list: the poll seen to switch, or the early pull when it
    already holds R's ranks."""
    early = _latest(pulls, "global_early")
    if early is not None and early.body is not None and holds_reset_ranks(read_rows(early.body)):
        return early
    return _latest(pulls, "global_reset")


class RankingStep(StrEnum):
    """What a Reset R's ranking check does now, from R's saved pulls
    (``ranking_step``):

    - ``early``: before R+5 min with no early global list saved: pull it
      from R+30 s, again a minute after a failed try.
    - ``polling``: the early list saved, R's list not yet seen, before R+1 h:
      from R+3 min pull the global list every 3 minutes until a poll's
      ``previousRank`` has switched from the early list's (saved as
      ``global_reset``), or the early list already holds R's ranks.
    - ``countries``: R's list seen, before R+2 h: pull the countries (at
      most weekly) and each country's list, a failed one again after 2
      minutes, and one not yet switched (``country_switched``) again after
      2 minutes until R+1 h; a country list's untracked Legend I players go
      to discovery.
    - ``missed``: before R+2 h, R's list never seen and its window passed:
      nothing more is pulled.
    - ``scoring``: from R+2 h each newly published Daily board version of R
      is scored once against the pulls (``score_day``), ``unknown`` with
      its reasons whenever a pull failed or is missing; a later version is
      scored again.

    ``pull_due`` moves through the pulling steps on the collector's 15 s
    tick and ``run`` scores every 10 minutes. Pulls are saved rows, so a
    restarted collector carries on from them; a failed tick logs ``failed``
    and the next tick tries again.
    """

    EARLY = "early"
    POLLING = "polling"
    COUNTRIES = "countries"
    MISSED = "missed"
    SCORING = "scoring"


def ranking_step(boundary: datetime, now: datetime, pulls: list[Pull]) -> RankingStep:
    """The step Reset ``boundary``'s check is in at ``now``, given its pulls."""
    since = now - boundary
    early = _latest(pulls, "global_early")
    if early is None and since < EARLY_UNTIL:
        return RankingStep.EARLY
    if since >= SCORE_FROM:
        return RankingStep.SCORING
    if _reset_pull(pulls) is not None:
        return RankingStep.COUNTRIES
    if early is not None and since < POLL_UNTIL:
        return RankingStep.POLLING
    return RankingStep.MISSED


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
    readings: dict[str, int],
    received: dict[str, int],
    unswitched: set[int],
    first: dict[int, list[Row]],
    battles: dict[str, int],
) -> dict[str, Any]:
    """Score one board, ``{tag: (position, trophies, confidence)}``, against the pulls.

    ``stuck`` are the game's stuck 5,000 rows; they and tags in two country
    lists are left out of every number and listed instead. Rank comparisons
    still use the published positions and trophies of the whole board,
    except in the ``unswitched`` country lists, whose ``previousRank``
    still holds the Reset before.
    ``readings`` are our newest trophies the worker had saved when each
    country list was requested; ``received``, S5's definition until 6
    November 2026, the newest whose response had arrived by then.
    ``first`` is each country's first list read after the Reset, compared
    with the board's trophies; ``battles``, for a player it differs on, the
    trophies their battles moved from the Reset to that list's request.
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
    for location, rows in countries.items():
        if location in unswitched:
            continue
        ranked = sorted(
            (row for row in rows if row.legend_i and row.tag in board and row.previous_rank),
            key=lambda row: row.previous_rank,
        )
        for index, higher in enumerate(ranked):
            for lower in ranked[index + 1:]:
                pairs += 1
                if value[higher.tag] < value[lower.tag]:
                    breaks += 1
                    # An uncertain partner may be the wrong one, so only a
                    # pair of confirmed entries contradicts either.
                    if {higher.tag, lower.tag} <= confirmed:
                        contradicted |= {higher.tag, lower.tag}
                elif value[higher.tag] == value[lower.tag]:
                    tied += 1
                    tie_agree += position[higher.tag] < position[lower.tag]
    def matches(saved: dict[str, int]) -> tuple[int, int]:
        live = [row for rows in countries.values() for row in rows
                if row.legend_i and row.tag in saved]
        return sum(row.trophies == saved[row.tag] for row in live), len(live)

    (live_match, live), (received_match, received_live) = matches(readings), matches(received)
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
    counted = board.keys() - faults
    uncertain = len(counted - confirmed)
    daily = [row for rows in first.values() for row in rows
             if row.legend_i and row.tag in board and row.tag not in faults]
    unexplained = sorted(row.tag for row in daily if row.trophies != value[row.tag]
                         and battles.get(row.tag) != row.trophies - value[row.tag])
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
        "s5_live_trophies_match_percent": _percent(live_match, live),
        "live_trophies_compared": live,
        "live_trophies_matched": live_match,
        "s5_received_live_trophies_match_percent": _percent(received_match, received_live),
        "received_live_trophies_compared": received_live,
        "received_live_trophies_matched": received_match,
        "s6_uncertain_percent": _percent(uncertain, len(counted)),
        "uncertain_entries": uncertain,
        "board_entries": len(counted),
        "global_exact_rank": exact,
        "tied_pairs": tied,
        "tie_order_agreement_percent": _percent(tie_agree, tied),
        "early_on_board": sum(row.tag in board for row in early),
        "early_trophies_equal": sum(
            row.tag in board and row.trophies == value[row.tag] for row in early
        ),
        "game_faults": {"stuck_5000": sorted(stuck), "tag_in_two_countries": sorted(twice)},
        "daily_trophies_compared": len(daily),
        "daily_trophies_equal": sum(row.trophies == value[row.tag] for row in daily),
        "daily_trophies_equal_after_battles": len(daily) - len(unexplained),
        "daily_trophies_unexplained_tags": unexplained,
    }


def load_pulls(
    database: CollectorDatabase, boundary: datetime, bodies: tuple[str, ...]
) -> list[Pull]:
    """R's pulls and the latest locations pull before R+2 h, oldest first.

    An earlier locations pull counts only within its 7-day refresh window at R.

    Only pulls whose purpose is in ``bodies`` carry their raw bytes.
    """
    with database.connection() as connection:
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
                  AND requested_at > %(expired)s AND boundary_at <> %(boundary)s
                ORDER BY requested_at DESC LIMIT 1
            )
            ORDER BY requested_at
            """,
            {"boundary": boundary, "until": boundary + SCORE_FROM,
             "expired": boundary - LOCATIONS_EVERY, "bodies": list(bodies)},
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


def _country_pulls(pulls: list[Pull], location_id: int) -> tuple[Pull, Pull] | None:
    """A country's list to score, the latest that switched or else the latest
    not yet switched, and its first list read; None when none was read."""
    read = [pull for pull in pulls if pull.purpose == "country"
            and pull.location_id == location_id and pull.failure in (None, NOT_SWITCHED)]
    return ((_latest(pulls, "country", location_id) or read[-1]), read[0]) if read else None


def previous_lists(
    database: CollectorDatabase, boundary: datetime, location_id: int | None = None
) -> dict[int, list[Row]]:
    """Each country's list to score (``_country_pulls``) from the Reset
    before ``boundary``, or only ``location_id``'s."""
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT ON (location_id) location_id, body_gzip
            FROM game_ranking_pulls
            WHERE boundary_at = %(before)s AND purpose = 'country'
              AND (failure IS NULL OR failure = %(not_switched)s)
              AND (%(location)s::integer IS NULL OR location_id = %(location)s)
            ORDER BY location_id, failure IS NOT NULL, requested_at DESC
            """,
            {"before": boundary - _DAY, "not_switched": NOT_SWITCHED, "location": location_id},
        ).fetchall()
    return {location: read_rows(gzip.decompress(body)) for location, body in rows}


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
) -> tuple[int, str | None]:
    """Save one pull and hand its untracked Legend I players to discovery.

    Returns how many were added and the pull's failure: a pull with a player
    another job holds is saved as failed, so it is tried again.
    """
    body = None if response is None else response.body
    added = 0
    with database.connection() as connection, connection.transaction():
        tags = sorted({row.tag for row in discover if row.legend_i})
        tracked = {
            str(tag) for (tag,) in connection.execute(
                "SELECT normalized_tag FROM players WHERE normalized_tag = ANY(%s) AND active",
                (tags,),
            )
        }
        for tag in tags:
            if tag not in tracked:
                handed, new = connection.execute(
                    "SELECT handed, added FROM clashlens_queue_promoted_player(%s)", (tag,)
                ).fetchone()
                added += new
                if not handed:
                    failure = failure or "discovery_not_handed"
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
    return added, failure


def _prune(database: CollectorDatabase, now: datetime) -> int:
    with database.connection() as connection, connection.transaction():
        return connection.execute(
            "DELETE FROM game_ranking_pulls WHERE boundary_at < %s",
            (reset_at(now) - KEEP_PULLS,),
        ).rowcount


class BoardRow(NamedTuple):
    snapshot_id: int
    snapshot_version: int
    normalized_tag: str | None
    board_position: int | None
    trophies: int | None
    confidence: str | None
    measured_coverage: Decimal


def score_day(database: CollectorDatabase, boundary: datetime) -> dict[str, Any] | None:
    """Score R's published board once per board version; None when there is nothing new."""
    with database.connection() as connection:
        board_rows = [BoardRow(*row) for row in connection.execute(
            "SELECT * FROM clashlens_game_ranking_board(%s)", (boundary,)
        ).fetchall()]
        if not board_rows or connection.execute(
            "SELECT 1 FROM game_ranking_scores WHERE boundary_at = %s AND snapshot_id = %s",
            (boundary, board_rows[0].snapshot_id),
        ).fetchone():
            return None
    pulls = load_pulls(
        database, boundary, ("global_early", "global_reset", "locations", "country")
    )
    reasons = []
    early, locations = _latest(pulls, "global_early"), _latest(pulls, "locations")
    reset = _reset_pull(pulls)
    if early is None:
        reasons.append("global_early_missing")
    if reset is None:
        reasons.append("previous_rank_not_switched" if _last_try(pulls, "global_poll")
                       else "global_reset_missing")
    expected = [] if locations is None else read_countries(locations.body)
    if locations is None:
        reasons.append("locations_missing")
    before = previous_lists(database, boundary)
    countries, pulled_at, first, first_at = {}, {}, {}, {}
    unswitched, too_small = {}, 0
    for location_id, code in expected:
        found = _country_pulls(pulls, location_id)
        if found is None:
            continue
        pull, earliest = found
        countries[location_id] = rows = read_rows(pull.body)
        pulled_at[location_id] = pull.requested_at
        first[location_id] = rows if earliest is pull else read_rows(earliest.body)
        first_at[location_id] = earliest.requested_at
        verdict = country_switched(rows, before.get(location_id))
        if verdict is False:
            unswitched[location_id] = code
        too_small += verdict is None
    if len(countries) < len(expected):
        reasons.append(f"countries_missing:{len(expected) - len(countries)}")
    board = {str(row.normalized_tag): (int(row.board_position), int(row.trophies), str(row.confidence))
             for row in board_rows if row.normalized_tag is not None}
    # A stuck 5,000 row: the list shows 5,000 trophies and 0 attack wins for a
    # player with a stored Legend battle, attack or defense, earlier this Season.
    at_5000 = sorted({row.tag for rows in countries.values() for row in rows
                      if row.legend_i and row.trophies == 5000 and row.attack_wins == 0})
    # Our newest saved trophies when each board player's country list was requested.
    live = [(row.tag, pulled_at[location]) for location, rows in countries.items()
            for row in rows if row.legend_i and row.tag in board]
    # Board players whose trophies differ from their country's first list.
    differ = [(row.tag, first_at[location]) for location, rows in first.items()
              for row in rows if row.legend_i and row.tag in board
              and row.trophies != board[row.tag][1]]
    with database.connection() as connection:
        stuck = {str(tag) for (tag,) in connection.execute(
            "SELECT * FROM clashlens_game_ranking_season_battlers(%s, %s, %s)",
            (at_5000, ranked_day_for(boundary).season_start, boundary),
        )}
        readings, received = (
            {str(tag): int(trophies) for tag, trophies in connection.execute(
                f"SELECT * FROM {lookup}(%s, %s)",
                ([tag for tag, _at in live], [at for _tag, at in live]),
            )}
            for lookup in ("clashlens_game_ranking_processed_readings",
                           "clashlens_game_ranking_readings")
        )
        # A battle that may have ended inside the window, known only from
        # the defender's side, leaves the difference unexplained.
        battles = {str(tag): int(change) for tag, change, unsure in connection.execute(
            "SELECT * FROM clashlens_game_ranking_battles(%s, %s, %s)",
            ([tag for tag, _at in differ], boundary, [at for _tag, at in differ]),
        ) if unsure == 0}
    country_times = [pull.requested_at for pull in pulls if pull.purpose == "country"]
    result = {
        "unknown_reasons": reasons,
        "board_measured_coverage": float(board_rows[0].measured_coverage),
        "global_early_at": None if early is None else _utc(early.requested_at),
        "global_reset_at": None if reset is None else _utc(reset.requested_at),
        "countries_expected": len(expected),
        "countries_pulled": len(countries),
        "countries_not_switched": sorted(unswitched.values()),
        "countries_too_small_to_tell": too_small,
        "first_country_at": _utc(min(country_times)) if country_times else None,
        "last_country_at": _utc(max(country_times)) if country_times else None,
        **score(
            board,
            None if early is None else read_rows(early.body),
            None if reset is None else read_rows(reset.body),
            countries,
            stuck,
            readings,
            received,
            set(unswitched),
            first,
            battles,
        ),
    }
    with database.connection() as connection, connection.transaction():
        connection.execute(
            """
            INSERT INTO game_ranking_scores (boundary_at, snapshot_id, snapshot_version, status, score)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (boundary_at, snapshot_id) DO NOTHING
            """,
            (boundary, board_rows[0].snapshot_id, board_rows[0].snapshot_version,
             "unknown" if reasons else "scored", json.dumps(result, sort_keys=True)),
        )
    return {"boundary_at": boundary.isoformat(), "snapshot_version": board_rows[0].snapshot_version,
            "status": "unknown" if reasons else "scored", "unknown_reasons": reasons}


def _days_to_score(database: CollectorDatabase, now: datetime) -> list[datetime]:
    """Resets past R+2 h whose pulls are still kept, since pulls began; scoring each is idempotent."""
    with database.connection() as connection:
        first = connection.execute("SELECT min(boundary_at) FROM game_ranking_pulls").fetchone()[0]
    if first is None:
        return []
    days = [reset_at(now) - _DAY * back for back in range(KEEP_PULLS // _DAY + 1)]
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
    pulls = await collector.database_call(
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
        if purpose == "country" and rows is not None:
            before = await collector.database_call(previous_lists, database, boundary, location[0])
            if country_switched(rows, before.get(location[0])) is False:
                failure = NOT_SWITCHED
        added, failure = await collector.database_call(
            save_pull, database, boundary, purpose, location, requested_at, response, failure,
            discover=rows if purpose == "country" and rows else [],
        )
        counts["added_to_discovery"] += added
        counts[purpose if failure is None else NOT_SWITCHED if failure == NOT_SWITCHED
               else "failed"] += 1
        pulls.append(Pull(purpose, None if location is None else location[0], requested_at,
                          failure, None if response is None else response.body))

    def due(purpose: str, location_id: int | None, gap: timedelta) -> bool:
        last = _last_try(pulls, purpose, location_id)
        return _latest(pulls, purpose, location_id) is None and (last is None or now - last >= gap)

    # Worked out again only after a pull: each reading parses the early list.
    step = ranking_step(boundary, now, pulls)
    if (
        step is RankingStep.EARLY
        and now >= boundary + EARLY_AT
        and due("global_early", None, EARLY_RETRY_AFTER)
    ):
        await pull("global_early", None)
        step = ranking_step(boundary, now, pulls)
    if (
        step is RankingStep.POLLING
        and now >= boundary + POLL_FROM
        and now - (_last_try(pulls, "global_poll") or boundary) >= POLL_EVERY
    ):
        reference = read_rows(_latest(pulls, "global_early").body)
        await pull("global_poll", None, judge=lambda rows: switched(rows, reference))
        step = ranking_step(boundary, now, pulls)
    if step is not RankingStep.COUNTRIES:
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
        if collector.stopping.is_set():
            break
        tries = [tried for tried in pulls
                 if tried.purpose == "country" and tried.location_id == location[0]]
        # A list not yet switched is pulled again until R+1 h.
        given_up = (bool(tries) and tries[-1].failure == NOT_SWITCHED
                    and now >= boundary + POLL_UNTIL)
        if due("country", location[0], RETRY_AFTER) and not given_up:
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
                for day in await collector.database_call(_days_to_score, collector.database, now):
                    scored = await collector.database_call(score_day, collector.database, day)
                    if scored:
                        print(json.dumps({**event, **scored}), flush=True)
                pruned = await collector.database_call(_prune, collector.database, now)
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
