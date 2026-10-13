"""Dashboard reads for one player's Legend day.

The next Reset rank range compares every Live Leaderboard player's best and
worst possible end of day: each attack still open can win at most 40 trophies
and each defense still open can lose at most 40. A player surely finishes
above you when their worst end beats your best end, and may finish above you
when their best end reaches your worst end. The counts come from each
player's latest published log for today, and so do the trophies they start
from: that log's Reset trophies plus its battles, so a battle the profile
has not caught up with is never counted without its trophies. A player
without them starts from their profile trophies with every slot open. The
range is an estimate: a battle not yet read leaves a slot looking open.

The board for that comparison, and today's held share across it, are read
in the background at most once a minute and shared by every dashboard, so a
request waits for them only when it asks to: until the first read of a
Legend day finishes, and once the newest read is 10 minutes old, the range
and the comparisons are left out. The dashboard asks again a few times,
each waiting up to 4 seconds for today's read, so it fills them in when that
finishes. The board's players and trophies are the Live Leaderboard's shared
read, never one from before the Reset; each log's "no opponent, no battle"
slots sit in a large saved record, so each saved day's are read once, not
every minute.

The projected place inside the range (projected_place.py) is worked out
after each board read, from that read's trophies and open slots, each
player's attacks on the three Legend days before today, when their newest
battle that moved their trophies ended by the Daily board's rule
(analytics.latest_trophy_changes, here from each player's newest published
log of today, each read once as published logs never change) and the saved
examples of the last 7 days. It takes seconds of calculation, so it runs in
a separate Python process, one at a time, and requests look it up: until
the first one of a Legend day finishes, and once the newest is 10 minutes
old, it is left out. The dashboard asks again while the range shows without
it, each follow-up waiting for the one under way within its 4 seconds.

Each base you attacked is compared with players at similar trophies: today's
held share among players whose day started within 100 trophies of the
base's, widened evenly until it holds 200 defenses but never past 300 either
side, because held share falls as trophies rise (last Season: about 65% under
4,900, 30% at 5,400 and up). Under 200 defenses within 300, it uses those if
there are at least 50. A base with fewer, or whose Reset trophies are
unknown, gets no comparison: it is never compared with every player.

The two army cards read finished Legend days whose army records are complete:
a player's attacks or defenses per army type. A defense's type is the
attacker's army, and a battle's trophies follow from its stars and
destruction by the game's rule. Both compare with every tracked player's
battles with each type, from the worker's saved counts for each day.
"""

from __future__ import annotations

import json
import multiprocessing
import time
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Condition, Lock, Thread
from typing import Any

from . import army_types, projected_place
from .api_db import ApiDatabase, _screen_events, _text
from .api_leaderboard import LiveBoardCache
from .domain import (
    CHAIN_BREAK_REASONS,
    MAX_BATTLE_TROPHIES,
    allocate_trophies,
    ranked_day_for,
)
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

BOARD_SECONDS = 60
# A board read this long ago is no longer shown: its refreshes are failing.
BOARD_MAX_AGE_SECONDS = 600
# A follow-up request waits this long for the board read under way, inside
# the website's 5 second limit.
BOARD_WAIT_SECONDS = 4
# Bases you attacked: compare with players whose day started this near the
# base's Reset trophies, widened until the comparison holds enough defenses
# but never past the most; with fewer than the fewest there, no comparison.
SIMILAR_TROPHIES = 100
SIMILAR_MOST_TROPHIES = 300
SIMILAR_DEFENSES = 200
SIMILAR_FEWEST_DEFENSES = 50
# The army cards look back at most this many finished Legend days; Armies
# on offense offers only these.
MAX_ARMY_DAYS = 28
OFFENSE_ARMY_DAYS = (3, army_types.OFFENSE_CARD_DAYS)
# The bases a player attacked: at most one row per attack.
_MAX_OPPONENTS = MAX_DAILY_ATTACKS

# Each active player's latest log for one Legend day, with its start
# trophies. Reading every log's saved input record took 4.4 of 4.8 s on
# 13 October 2026 production data: about 6 KB each, stored apart from the row.
_BOARD_SQL = """
SELECT player.normalized_tag, ranked_day.start_trophies,
       day.attack_count, day.attack_gain, day.defense_count, day.defense_loss,
       day.defense_three_star_count, day.ranked_day_version_id
FROM players AS player
JOIN LATERAL (
    SELECT attack_count, attack_gain, defense_count, defense_loss,
           defense_three_star_count, ranked_day_version_id
    FROM api_player_daily_logs AS daily
    WHERE daily.player_id = player.id
      AND daily.ranked_day_start = %(day_start)s
    ORDER BY daily.version DESC
    LIMIT 1
) AS day ON true
LEFT JOIN ranked_day_versions AS ranked_day
    ON ranked_day.id = day.ranked_day_version_id
WHERE player.active
"""
# A saved day's "no opponent, no battle" attack and defense slots.
_ZERO_SLOTS_SQL = """
SELECT id,
       COALESCE((input_evidence ->> 'zero_result_attack_slots')::int, 0),
       COALESCE((input_evidence ->> 'zero_result_defense_slots')::int, 0)
FROM ranked_day_versions
WHERE id = ANY(%s)
"""

# Each player's id and attacks on the three Legend days before
# %(day_start)s, for their projected place.
_PROJECTION_PLAYERS_SQL = f"""
SELECT player.normalized_tag, player.id, recent.yesterday, recent.before
FROM players AS player
{projected_place.RECENT_ATTACKS_SQL}WHERE player.normalized_tag = ANY(%(tags)s)
"""

# Each player's newest published log of the Legend day %(day_start)s.
_NEWEST_LOGS_SQL = """
SELECT DISTINCT ON (daily.player_id) daily.player_id, daily.id
FROM api_player_daily_logs AS daily
WHERE daily.ranked_day_start = %(day_start)s
ORDER BY daily.player_id, daily.version DESC
"""
# The battles the given logs keep with a report time: each report's side,
# battle, time and whether it moved the player's trophies.
_LOG_REPORTS_SQL = """
SELECT daily.id, event ->> 'lens', event ->> 'battle_identity',
       (event ->> 'battle_timestamp')::timestamptz,
       event ->> 'trophy_change' <> '0'
FROM api_player_daily_logs AS daily
CROSS JOIN LATERAL jsonb_array_elements(CASE jsonb_typeof(daily.battles)
                                        WHEN 'array' THEN daily.battles
                                        ELSE '[]' END) AS event
WHERE daily.id = ANY(%s)
  AND event ->> 'battle_timestamp' ~ '^[0-9]{4}-[0-9][0-9]-[0-9][0-9]T'
  AND event ->> 'included' IS DISTINCT FROM 'false'
  AND event ->> 'valid' IS DISTINCT FROM 'false'
"""
_LOGS_A_READ = 2000
# With only the defender's report, a battle ends this long after it, the
# median attack length (analytics.latest_trophy_changes).
_DEFENSE_REPORT_TO_END = timedelta(seconds=155)

# Each player's latest log for one Legend day, with the day's start trophies
# and its "no opponent, no battle" defense slots, which the game counts as
# used.
_DAY_LOGS_SQL = """
SELECT DISTINCT ON (daily.player_id)
       player.normalized_tag, daily.attack_count, daily.defense_count,
       daily.defense_loss, daily.coverage, daily.battles, daily.published_at,
       ranked_day.start_trophies,
       COALESCE((ranked_day.input_evidence ->> 'zero_result_defense_slots')::int, 0),
       daily.state, daily.partial_reasons
FROM api_player_daily_logs AS daily
JOIN players AS player ON player.id = daily.player_id
LEFT JOIN ranked_day_versions AS ranked_day
    ON ranked_day.id = daily.ranked_day_version_id
WHERE player.normalized_tag = ANY(%(tags)s)
  AND daily.ranked_day_start = %(day_start)s
ORDER BY daily.player_id, daily.version DESC
"""


@dataclass(frozen=True)
class _Board:
    day_start: datetime
    computed_at: datetime
    # tag -> (best end of day, worst end of day)
    ends: dict[str, tuple[int, int]]
    best_ends: list[int]
    worst_ends: list[int]
    held: int
    defenses: int
    # Distinct day start trophies, ascending, and the defenses and held
    # defenses of players who started below each (one more entry than starts).
    starts: list[int]
    defenses_below: list[int]
    held_below: list[int]
    # tag -> (trophies now, open attacks, open defenses), for the projection.
    slots_left: dict[str, tuple[int, int, int]]


@dataclass(frozen=True)
class _Projection:
    day_start: datetime
    # tag -> projected place, before it is kept inside the tag's range.
    places: dict[str, int]


def _open(used: Any, allowed: int) -> int:
    return max(0, allowed - (0 if used is None else int(used)))


def _read_board(
    connection: Any,
    now: datetime,
    live: list[tuple[Any, ...]],
    slots: dict[int, tuple[int, int]],
) -> tuple[_Board, dict[int, tuple[int, int]]]:
    """The board for ``live``, the Live Leaderboard's rows, and the saved
    days' slots it used; ``slots`` holds those already read."""
    day_start = ranked_day_for(now).start
    days = {
        _text(row[0]): row[1:]
        for row in connection.execute(_BOARD_SQL, {"day_start": day_start}).fetchall()
    }
    versions = {int(day[6]) for day in days.values() if day[6] is not None}
    unread = sorted(versions - slots.keys())
    if unread:
        slots = slots | {
            int(row[0]): (int(row[1]), int(row[2]))
            for row in connection.execute(_ZERO_SLOTS_SQL, (unread,)).fetchall()
        }
    slots = {version: slots[version] for version in versions if version in slots}
    ends: dict[str, tuple[int, int]] = {}
    slots_left: dict[str, tuple[int, int, int]] = {}
    held = defenses = 0
    # Day start trophies -> [defenses, held defenses].
    by_start: dict[int, list[int]] = {}
    for row in live:
        tag, trophies = _text(row[0]), row[2]
        start, attacks, gained, defended, lost, tripled, version = days.get(
            tag, (None,) * 7
        )
        zero_attacks, zero_defenses = slots.get(
            -1 if version is None else int(version), (0, 0)
        )
        if None in (start, attacks, gained, defended, lost):
            basis, open_attacks, open_defenses = (
                int(trophies), MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES
            )
        else:
            basis = int(start) + int(gained) - int(lost)
            open_attacks = _open(int(attacks) + zero_attacks, MAX_DAILY_ATTACKS)
            open_defenses = _open(int(defended) + zero_defenses, MAX_DAILY_DEFENSES)
        ends[tag] = (
            basis + MAX_BATTLE_TROPHIES * open_attacks,
            basis - MAX_BATTLE_TROPHIES * open_defenses,
        )
        slots_left[tag] = (basis, open_attacks, open_defenses)
        if defended:
            defenses += int(defended)
            held += int(defended) - int(tripled or 0)
            if start is not None:
                tally = by_start.setdefault(int(start), [0, 0])
                tally[0] += int(defended)
                tally[1] += int(defended) - int(tripled or 0)
    starts = sorted(by_start)
    defenses_below, held_below = [0], [0]
    for value in starts:
        defenses_below.append(defenses_below[-1] + by_start[value][0])
        held_below.append(held_below[-1] + by_start[value][1])
    return _Board(
        day_start=day_start,
        computed_at=now,
        ends=ends,
        best_ends=sorted(best for best, _ in ends.values()),
        worst_ends=sorted(worst for _, worst in ends.values()),
        held=held,
        defenses=defenses,
        starts=starts,
        defenses_below=defenses_below,
        held_below=held_below,
        slots_left=slots_left,
    ), slots


def _in_background(read: Callable[[], None]) -> None:
    Thread(target=read, name="dashboard-board", daemon=True).start()


# A log's kept battles: (attack, battle, report time, moved trophies).
_Reports = tuple[tuple[bool, str, datetime, bool], ...]


def _latest_changes(
    connection: Any, day_start: datetime, reports: dict[int, _Reports]
) -> tuple[dict[int, datetime], dict[int, _Reports]]:
    """When each player's newest battle that moved their trophies today
    ended, by the Daily board's rule: at the attacker's report, from the
    attacker's own log whether or not they are on the board, else 155
    seconds after the defender's. ``reports`` holds the logs already read
    and comes back with only today's newest."""
    newest = dict(connection.execute(_NEWEST_LOGS_SQL, {"day_start": day_start}).fetchall())
    kept = {log: reports[log] for log in newest.values() if log in reports}
    unread = [log for log in newest.values() if log not in kept]
    read: dict[int, list[tuple[bool, str, datetime, bool]]] = {log: [] for log in unread}
    # A few thousand at a time: all of them, after a restart, took about
    # 3 s on 13,300 players with 11 battles each.
    for first in range(0, len(unread), _LOGS_A_READ):
        for log, lens, battle, at, moved in connection.execute(
            _LOG_REPORTS_SQL, (unread[first : first + _LOGS_A_READ],)
        ):
            read[log].append((lens == "offense", _text(battle), at, moved is True))
    kept |= {log: tuple(events) for log, events in read.items()}
    attack_ends = {
        battle: at
        for events in kept.values()
        for attack, battle, at, _ in events
        if attack
    }
    latest: dict[int, datetime] = {}
    for player, log in newest.items():
        ends = [
            at if attack else attack_ends.get(battle, at + _DEFENSE_REPORT_TO_END)
            for attack, battle, at, moved in kept[log]
            if moved
        ]
        if ends:
            latest[player] = max(ends)
    return latest, kept


def _projection_inputs(
    connection: Any, now: datetime, board: _Board, reports: dict[int, _Reports]
) -> tuple[tuple[Any, ...] | None, dict[int, _Reports]]:
    """projected_place.middle_places's inputs for ``board``'s players, or
    None before any day's examples are saved, and ``_latest_changes``'s
    logs read."""
    examples = projected_place.read_examples(connection, now)
    if examples is None or not board.slots_left:
        return None, reports
    tags = list(board.slots_left)
    players = {
        _text(row[0]): row[1:]
        for row in connection.execute(
            _PROJECTION_PLAYERS_SQL, {"tags": tags, "day_start": board.day_start}
        ).fetchall()
    }
    latest, reports = _latest_changes(connection, board.day_start, reports)
    trophies, open_attacks, open_defenses = (
        list(column) for column in zip(*board.slots_left.values(), strict=True)
    )
    found = [players.get(tag, (None, 0, 0)) for tag in tags]
    return (
        trophies,
        open_attacks,
        open_defenses,
        [projected_place.activity(int(yesterday), int(before))
         for _, yesterday, before in found],
        [None if player_id is None else latest.get(int(player_id))
         for player_id, _, _ in found],
        tags,
        examples,
    ), reports


_child: ProcessPoolExecutor | None = None
_child_lock = Lock()


def _in_child_process(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """``function(*args, **kwargs)`` in a separate Python process, started
    once and kept, so its seconds of calculation never hold up this
    process's requests. One that died is started again next time."""
    global _child
    with _child_lock:
        if _child is None:
            _child = ProcessPoolExecutor(
                1, mp_context=multiprocessing.get_context("spawn")
            )
        child = _child
    try:
        return child.submit(function, *args, **kwargs).result()
    except BrokenProcessPool:
        with _child_lock:
            if _child is child:
                _child = None
        raise


class DashboardBoard:
    """The board for rank ranges and held share, read in the background at
    most once every ``seconds``, one read at a time, so no request waits for
    it unless it asks to, and the projected places worked out after each
    read, one at a time. ``run`` starts a read; tests pass one that reads at
    once. ``compute`` runs the projection's calculation."""

    def __init__(
        self,
        live_board: LiveBoardCache | None = None,
        *,
        seconds: float = BOARD_SECONDS,
        run: Callable[[Callable[[], None]], None] = _in_background,
        clock: Callable[[], float] = time.monotonic,
        compute: Callable[..., Any] = _in_child_process,
    ) -> None:
        self._live_board = live_board or LiveBoardCache()
        self._seconds = seconds
        self._run = run
        self._clock = clock
        self._compute = compute
        self._lock = Lock()
        self._read_done = Condition(self._lock)
        self._board: _Board | None = None
        self._read_at = float("-inf")
        self._next_read = float("-inf")
        self._reading = False
        self._tried_day: datetime | None = None
        # Saved day -> its (attack, defense) "no opponent, no battle" slots.
        self._slots: dict[int, tuple[int, int]] = {}
        self._projection: _Projection | None = None
        self._projected_at = float("-inf")
        self._projecting = False
        # Published log -> its battles, for the projection's equal-trophy order.
        self._reports: dict[int, _Reports] = {}

    def current(
        self, database: ApiDatabase, now: datetime, *, wait: float = 0
    ) -> _Board | None:
        """Today's newest board, or None while today's first read runs or
        once the newest is too old, after waiting up to ``wait`` seconds for
        today's read. A due read starts in the background."""
        day_start = ranked_day_for(now).start
        until = time.monotonic() + wait

        def shown() -> _Board | None:
            board = self._board
            if (
                board is None
                or board.day_start != day_start
                or self._clock() >= self._read_at + BOARD_MAX_AGE_SECONDS
            ):
                return None
            return board

        while True:
            with self._lock:
                # A new Legend day reads at once; a failed read waits its turn.
                start = not self._reading and (
                    self._clock() >= self._next_read or self._tried_day != day_start
                )
                if start:
                    self._reading, self._tried_day = True, day_start
            if start:
                self._run(lambda: self._read(database, now))
            with self._lock:
                self._read_done.wait_for(
                    lambda: shown() is not None or not self._reading,
                    max(0.0, until - time.monotonic()),
                )
                # A read from before the Reset hands the wait to today's.
                if (
                    shown() is not None
                    or self._tried_day == day_start
                    or time.monotonic() >= until
                ):
                    return shown()

    def _read(self, database: ApiDatabase, now: datetime) -> None:
        # A failed read waits a turn instead of starting again at once.
        try:
            live = self._live_board.current(database, now).rows
            with database.pool.connection() as connection:
                board, slots = _read_board(connection, now, live, self._slots)
        except Exception as error:  # noqa: BLE001 - read again at the next turn
            print(
                json.dumps(
                    {
                        "event": "dashboard_board",
                        "status": "failed",
                        "error": type(error).__name__,
                    }
                ),
                flush=True,
            )
            with self._lock:
                self._next_read = self._clock() + self._seconds
                self._reading = False
                self._read_done.notify_all()
            return
        with self._lock:
            self._board, self._slots = board, slots
            self._read_at = self._clock()
            self._next_read = self._read_at + self._seconds
            self._reading = False
            self._read_done.notify_all()
            project, self._projecting = not self._projecting, True
        if project:
            self._project(database, now, board)

    def _project(self, database: ApiDatabase, now: datetime, board: _Board) -> None:
        """Work out ``board``'s projected places, after the board is shown.
        A failure keeps the last ones until they are too old."""
        try:
            with database.pool.connection() as connection:
                inputs, self._reports = _projection_inputs(
                    connection, now, board, self._reports
                )
            places = {}
            if inputs is not None:
                middles = self._compute(
                    projected_place.middle_places, *inputs,
                    # Projections from the same minute imagine the same days.
                    seed=int(now.timestamp()) // 60,
                )
                places = dict(zip(board.slots_left, middles, strict=True))
        except Exception as error:  # noqa: BLE001 - tried again after the next read
            print(
                json.dumps(
                    {
                        "event": "dashboard_projection",
                        "status": "failed",
                        "error": type(error).__name__,
                    }
                ),
                flush=True,
            )
            places = None
        with self._lock:
            if places is not None:
                self._projection = _Projection(board.day_start, places)
                self._projected_at = self._clock()
            self._projecting = False
            self._read_done.notify_all()

    def projection(self, now: datetime, *, wait: float = 0) -> _Projection | None:
        """Today's newest projected places, or None before the first of the
        Legend day or once the newest is too old, after waiting up to
        ``wait`` seconds for the one being worked out."""
        day_start = ranked_day_for(now).start

        def shown() -> _Projection | None:
            projection = self._projection
            if (
                projection is None
                or projection.day_start != day_start
                or self._clock() >= self._projected_at + BOARD_MAX_AGE_SECONDS
            ):
                return None
            return projection

        with self._lock:
            self._read_done.wait_for(
                lambda: shown() is not None or not self._projecting, max(0.0, wait)
            )
            return shown()


def rank_range(board: _Board, tag: str) -> dict[str, int] | None:
    """The best and worst rank ``tag`` can still finish today on, or None off the board."""
    ends = board.ends.get(tag)
    if ends is None:
        return None
    best_end, worst_end = ends
    # Players whose worst end beats this best end finish above it whatever happens.
    surely_above = len(board.worst_ends) - bisect_right(board.worst_ends, best_end)
    # Players whose best end reaches this worst end may finish above it; the
    # count includes this player, whose best end always reaches its own worst.
    maybe_above_or_self = len(board.best_ends) - bisect_left(board.best_ends, worst_end)
    return {"best": surely_above + 1, "worst": maybe_above_or_self}


def projected_rank(
    board: _Board, projection: _Projection | None, tag: str
) -> int | None:
    """The place ``tag`` is projected to finish today on, inside its rank
    range; None off the board or without a projection for it."""
    outer = rank_range(board, tag)
    middle = None if projection is None else projection.places.get(tag)
    if outer is None or middle is None:
        return None
    return min(outer["worst"], max(outer["best"], middle))


def similar_held(board: _Board, trophies: int | None) -> dict[str, int] | None:
    """Today's held and all defenses of players whose day started within
    100 trophies of ``trophies``, widened evenly until there are 200 defenses
    but never past 300. None under 50 defenses there, or when ``trophies`` is
    unknown."""
    if trophies is None:
        return None

    def within(reach: int) -> tuple[int, int]:
        low = bisect_left(board.starts, trophies - reach)
        high = bisect_right(board.starts, trophies + reach)
        return (
            board.held_below[high] - board.held_below[low],
            board.defenses_below[high] - board.defenses_below[low],
        )

    reach = SIMILAR_TROPHIES
    if within(reach)[1] < SIMILAR_DEFENSES:
        # The narrowest wider reach with enough defenses, by halving between
        # one too narrow and the most, which is kept if none has enough.
        narrow, reach = reach, SIMILAR_MOST_TROPHIES
        while reach - narrow > 1:
            middle = (narrow + reach) // 2
            if within(middle)[1] < SIMILAR_DEFENSES:
                narrow = middle
            else:
                reach = middle
    held, defenses = within(reach)
    return (
        {"held": held, "defenses": defenses}
        if defenses >= SIMILAR_FEWEST_DEFENSES
        else None
    )


def _battles_known(day: tuple[Any, ...]) -> bool:
    """Whether a published day can average the automatic loss, by the
    worker's rule (ranked_day_inputs.load_previous_day): continuous logs, a
    saved Legend day and no 9th attack or defense."""
    reasons = day[10] if isinstance(day[10], list) else []
    return (
        day[4] == "complete"
        and day[9] in {"Complete", "Partial"}
        and not any(
            reason in CHAIN_BREAK_REASONS
            or str(reason).startswith("ranked_day_state:")
            for reason in reasons
        )
    )


def _automatic_defense(
    today: tuple[Any, ...] | None,
    previous: tuple[Any, ...] | None,
    *,
    season_first_day: bool,
) -> tuple[int | None, int | None]:
    """Open defense slots at Reset and the game's loss for each, as the worker
    charges it (reconciliation.automatic_defense_loss). The loss is None
    unless both days' battles are known."""
    if today is None:
        return None, None
    defenses = int(today[2] or 0) + int(today[8] or 0)
    open_slots = max(0, MAX_DAILY_DEFENSES - defenses)
    if not 1 <= defenses < MAX_DAILY_DEFENSES or today[4] != "complete":
        return open_slots, None
    loss = int(today[3] or 0)
    previous_defenses = previous_loss = 0
    if not season_first_day:
        if previous is None or not _battles_known(previous) or previous[2] is None:
            return open_slots, None
        previous_defenses = int(previous[2]) + int(previous[8] or 0)
        previous_loss = int(previous[3] or 0)
    return open_slots, (previous_loss + loss) // (previous_defenses + defenses)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def get_player_today(
    database: ApiDatabase,
    board_cache: DashboardBoard,
    tag: str,
    *,
    now: datetime,
    board_wait: float = 0,
) -> dict[str, Any] | None:
    """One player's dashboard numbers for today, or None for an unknown tag.
    ``opponents`` is None until the player's log for today is published; the
    range and comparisons are None while the shared board is unavailable,
    after waiting up to ``board_wait`` seconds for it, the projected place
    also while its projection is worked out, within the same wait, and
    ``board_computed_at`` says when the board was read."""
    day = ranked_day_for(now)
    until = time.monotonic() + board_wait
    board = board_cache.current(database, now, wait=board_wait)
    projection = None if board is None else board_cache.projection(
        now, wait=until - time.monotonic()
    )
    with database.pool.connection() as connection:
        if connection.execute(
            "SELECT 1 FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone() is None:
            return None
        own_rows = {
            _text(row[0]): row
            for row in connection.execute(
                _DAY_LOGS_SQL, {"tags": [tag], "day_start": day.start}
            ).fetchall()
        }
        today = own_rows.get(tag)
        previous = None
        if day.day_number > 1:
            previous = next(
                iter(
                    connection.execute(
                        _DAY_LOGS_SQL,
                        {"tags": [tag], "day_start": day.start - timedelta(days=1)},
                    ).fetchall()
                ),
                None,
            )
        attacks, _ = _screen_events(None if today is None else today[5])
        # In the order they happened.
        attacks = sorted(attacks[:_MAX_OPPONENTS], key=lambda event: event["battle_timestamp"])
        opponent_tags = sorted({event["opponent"]["tag"] for event in attacks})
        opponent_rows = {
            _text(row[0]): row
            for row in connection.execute(
                _DAY_LOGS_SQL, {"tags": opponent_tags, "day_start": day.start}
            ).fetchall()
        } if opponent_tags else {}

    opponents = []
    for attack in attacks:
        opponent_tag = attack["opponent"]["tag"]
        row = opponent_rows.get(opponent_tag)
        _, defended = _screen_events(None if row is None else row[5])
        defenses = [
            {
                "stars": event["stars"],
                "trophy_change": event["trophy_change"],
                "yours": event["opponent"]["tag"] == tag,
                "at": event["battle_timestamp"],
            }
            for event in defended
        ]
        if not any(defense["yours"] for defense in defenses):
            # This attack is in your log but not yet in theirs. A defense
            # loses what the attack won, except a 0-star one, which loses 0.
            defenses.append({
                "stars": attack["stars"],
                "trophy_change": -attack["trophy_change"] if attack["stars"] else 0,
                "yours": True,
                "at": attack["battle_timestamp"],
            })
        defenses.sort(key=lambda defense: defense["at"])
        reset_trophies = None if row is None or row[7] is None else int(row[7])
        opponents.append(
            {
                "tag": opponent_tag,
                "name": attack["opponent"]["name"],
                "reset_trophies": reset_trophies,
                "hit": {
                    "stars": attack["stars"],
                    "destruction_percentage": attack["destruction_percentage"],
                    "trophy_change": attack["trophy_change"],
                    "battle_timestamp": attack["battle_timestamp"],
                },
                "defenses": [
                    {key: defense[key] for key in ("stars", "trophy_change", "yours")}
                    for defense in defenses
                ],
                "observed_at": None if row is None else _iso(row[6]),
                "legends_held": None
                if board is None
                else similar_held(board, reset_trophies),
            }
        )

    open_defenses, auto_each = _automatic_defense(
        today, previous, season_first_day=day.day_number == 1
    )
    return {
        "tag": tag,
        "ranked_day_start": _iso(day.start),
        "board_computed_at": None if board is None else _iso(board.computed_at),
        "rank_range": None if board is None else rank_range(board, tag),
        "projected_rank": None if board is None else projected_rank(board, projection, tag),
        # Every player's, only for the website released before each base had
        # its own; never a base's comparison.
        "legends_held": {"held": board.held, "defenses": board.defenses}
        if board is not None and board.defenses
        else None,
        "open_defenses": open_defenses,
        "automatic_defense_each": auto_each,
        "opponents": None if today is None else opponents,
    }


# Per army type: battles, three-star battles, and trophies won on attack or
# lost on defense.
_Totals = dict[int, list[int]]
# Named types only: Other and Unreadable count in the totals, never as a row.
_UNNAMED = frozenset({army_types.OTHER, army_types.UNREADABLE})


def _add_outcomes(totals: _Totals, rows: list[tuple[str, int, int, int, int]]) -> None:
    for lens, type_id, stars, destruction, battles in rows:
        allocation = allocate_trophies(stars, destruction)
        trophies = (
            allocation.attacker_gain if lens == "offense" else allocation.defender_loss
        )
        total = totals.setdefault(type_id, [0, 0, 0])
        total[0] += battles
        total[1] += battles if stars == 3 else 0
        total[2] += battles * trophies


class ArmyDays:
    """Each counted Legend day's attacks or defenses per army type across
    every tracked player, read from the worker's saved counts and kept until
    the day is counted again or the type rules change."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._days: dict[
            tuple[str, datetime], tuple[tuple[str, str], dict[int, list[int]]]
        ] = {}

    def totals(
        self, connection: Any, days: list[tuple[datetime, str]], lens: str = "offense"
    ) -> list[dict[int, list[int]]]:
        """Totals for each (day start, completed-day marker), reading only the
        days not kept yet, in the snapshot that found those markers."""
        with self._lock:
            missing = [
                start for start, marker in days
                if self._days.get((lens, start), (None,))[0]
                != (marker, army_types.RULES_VERSION)
            ]
            if missing:
                read = army_types.read_day_totals(connection, missing, lens)
                for start, marker in days:
                    if start in read:
                        self._days[(lens, start)] = (
                            (marker, army_types.RULES_VERSION), read[start]
                        )
                # Older days than any card can ask for go.
                for key in sorted(self._days, key=lambda key: key[1])[
                    : -4 * MAX_ARMY_DAYS
                ]:
                    del self._days[key]
            return [self._days[(lens, start)][1] for start, _marker in days]


def get_player_armies(
    database: ApiDatabase,
    army_days: ArmyDays,
    tag: str,
    *,
    lens: str,
    days: int,
    now: datetime,
) -> dict[str, Any] | None:
    """One player's attacks or defenses per army type over the last ``days``
    finished Legend days, beside every tracked player's with each type; None
    for an unknown tag.

    Only days whose army records are complete, for attacks also counted by
    the worker, and whose newest saved daily log for this player is Complete
    with complete coverage, count, and only once every saved army has its type
    under the current rules: ``covered_days`` names them. A finished Season's
    records go 7 days after it ends. Known limit: a corrected day counts as
    soon as its newest log is published, so until that day's army records are
    rebuilt it shows the earlier build's battles.

    Every tracked player's defenses count only once the worker has counted
    every covered day for that day's build; until then ``legends_total`` and
    each defense row's ``legends`` are None.
    """
    today = ranked_day_for(now).start
    window = [today - timedelta(days=offset) for offset in range(days, 0, -1)]
    mine: _Totals = {}
    with database.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        player = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
        ).fetchone()
        if player is None:
            return None
        covered = [
            (start, _text(season_id), int(day_number), _text(marker))
            for start, season_id, day_number, marker in connection.execute(
                """
                SELECT day.ranked_day_start, day.official_season_id,
                       day.season_day_number, day.fact_input_hash
                FROM army_analytics_completed_days AS day
                WHERE day.ranked_day_start = ANY(%s::timestamptz[])
                  AND (%s <> 'offense' OR EXISTS (
                      SELECT 1 FROM army_type_counted_days AS counted
                      WHERE counted.ranked_day_start = day.ranked_day_start
                        AND counted.fact_input_hash = day.fact_input_hash
                  ))
                  AND (
                      SELECT daily.state = 'Complete'
                             AND daily.coverage = 'complete'
                      FROM api_player_daily_logs AS daily
                      WHERE daily.player_id = %s
                        AND daily.ranked_day_start = day.ranked_day_start
                      ORDER BY daily.version DESC
                      LIMIT 1
                  )
                ORDER BY day.ranked_day_start
                """,
                (window, lens, int(player[0])),
            ).fetchall()
        ] if army_types.rules_ready(connection) else []
        seasons: dict[str, list[int]] = {}
        for _start, season_id, day_number, _marker in covered:
            seasons.setdefault(season_id, []).append(day_number)
        for season_id, day_numbers in seasons.items():
            _add_outcomes(mine, army_types.read_type_outcomes(
                connection, season_id=season_id, day_numbers=day_numbers,
                lens=lens, player_id=int(player[0]),
            ))
        markers = [(day[0], day[3]) for day in covered]
        legend_days = army_days.totals(connection, markers, lens) if (
            lens == "offense" or army_types.all_counted(connection, markers, lens)
        ) else []

    def outcome(counts: list[int]) -> dict[str, int]:
        return dict(zip(("battles", "three_stars", "trophies"), counts, strict=True))

    rows = [
        {
            "type_id": type_id,
            "name": army_types.TYPE_NAMES.get(type_id, f"Type {type_id}"),
            **outcome(counts),
        }
        for type_id, counts in mine.items()
        if type_id not in _UNNAMED
    ]
    # Defenses: the armies that cost the most trophies first; attacks: the
    # armies used most.
    rows.sort(key=lambda row: (
        -row["trophies"] if lens == "defense" else 0, -row["battles"], row["type_id"]
    ))
    # Every tracked player's battles with each type over the same days.
    width = 2 if lens == "offense" else 3
    legends: dict[int, list[int]] = {}
    for day in legend_days:
        for type_id, counts in day.items():
            total = legends.setdefault(type_id, [0] * width)
            for index, value in enumerate(counts):
                total[index] += value
    for row in rows:
        counts = legends.get(row["type_id"], [0] * width)
        row["legends"] = (
            {"battles": counts[0], "three_stars": counts[1]} if lens == "offense"
            else outcome(counts) if legend_days else None
        )
    result = {
        "tag": tag,
        "lens": lens,
        "days": days,
        "covered_days": [_iso(day[0]) for day in covered],
        "rows": rows,
        "total": outcome(
            [sum(counts[index] for counts in mine.values()) for index in range(3)]
        ),
    }
    if lens == "defense":
        # Every defense, Other and unreadable armies included.
        result["legends_total"] = outcome(
            [sum(counts[index] for counts in legends.values()) for index in range(3)]
        ) if legend_days else None
    return result
