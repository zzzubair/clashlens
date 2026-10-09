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

The board for that comparison, and today's held share across it, are read in
one query and kept for a minute, so dashboards share one read per minute.
"""

from __future__ import annotations

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .api_db import ApiDatabase, _screen_events, _text
from .api_leaderboard import _LIVE_PLAYERS_SQL, _season_params
from .domain import CHAIN_BREAK_REASONS, MAX_BATTLE_TROPHIES, ranked_day_for
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

BOARD_SECONDS = 60
# The bases a player attacked: at most one row per attack.
_MAX_OPPONENTS = MAX_DAILY_ATTACKS

_BOARD_SQL = f"""
WITH live AS MATERIALIZED ({_LIVE_PLAYERS_SQL})
SELECT live.normalized_tag, live.trophies, ranked_day.start_trophies,
       day.attack_count, day.attack_gain, day.defense_count, day.defense_loss,
       day.defense_three_star_count,
       COALESCE((ranked_day.input_evidence ->> 'zero_result_attack_slots')::int, 0),
       COALESCE((ranked_day.input_evidence ->> 'zero_result_defense_slots')::int, 0)
FROM live
JOIN players AS player ON player.normalized_tag = live.normalized_tag
LEFT JOIN LATERAL (
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
"""

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


def _open(used: Any, allowed: int) -> int:
    return max(0, allowed - (0 if used is None else int(used)))


def _read_board(connection: Any, now: datetime) -> _Board:
    day_start = ranked_day_for(now).start
    rows = connection.execute(
        _BOARD_SQL, {**_season_params(now), "day_start": day_start}
    ).fetchall()
    ends: dict[str, tuple[int, int]] = {}
    held = defenses = 0
    for (
        tag, trophies, start, attacks, gained, defended, lost, tripled,
        zero_attacks, zero_defenses,
    ) in rows:
        if None in (start, attacks, gained, defended, lost):
            basis, open_attacks, open_defenses = (
                int(trophies), MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES
            )
        else:
            basis = int(start) + int(gained) - int(lost)
            open_attacks = _open(int(attacks) + int(zero_attacks), MAX_DAILY_ATTACKS)
            open_defenses = _open(
                int(defended) + int(zero_defenses), MAX_DAILY_DEFENSES
            )
        ends[_text(tag)] = (
            basis + MAX_BATTLE_TROPHIES * open_attacks,
            basis - MAX_BATTLE_TROPHIES * open_defenses,
        )
        if defended:
            defenses += int(defended)
            held += int(defended) - int(tripled or 0)
    return _Board(
        day_start=day_start,
        computed_at=now,
        ends=ends,
        best_ends=sorted(best for best, _ in ends.values()),
        worst_ends=sorted(worst for _, worst in ends.values()),
        held=held,
        defenses=defenses,
    )


class DashboardBoard:
    """The board for rank ranges and held share, read at most once a minute."""

    def __init__(self, seconds: float = BOARD_SECONDS) -> None:
        self._seconds = seconds
        self._lock = Lock()
        self._board: _Board | None = None
        self._refresh_after = 0.0

    def current(self, database: ApiDatabase, now: datetime) -> _Board:
        with self._lock:
            board = self._board
            stale = (
                board is None
                or time.monotonic() >= self._refresh_after
                or board.day_start != ranked_day_for(now).start
            )
            if stale:
                with database.pool.connection() as connection:
                    board = _read_board(connection, now)
                self._board = board
                self._refresh_after = time.monotonic() + self._seconds
            assert board is not None
            return board


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
    database: ApiDatabase, board_cache: DashboardBoard, tag: str, *, now: datetime
) -> dict[str, Any] | None:
    """One player's dashboard numbers for today, or None for an unknown tag."""
    day = ranked_day_for(now)
    board = board_cache.current(database, now)
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
                "yours": event["opponent"]["tag"] == tag,
                "at": event["battle_timestamp"],
            }
            for event in defended
        ]
        if not any(defense["yours"] for defense in defenses):
            # This attack is in your log but not yet in theirs.
            defenses.append(
                {"stars": attack["stars"], "yours": True, "at": attack["battle_timestamp"]}
            )
        defenses.sort(key=lambda defense: defense["at"])
        opponents.append(
            {
                "tag": opponent_tag,
                "name": attack["opponent"]["name"],
                "reset_trophies": None if row is None or row[7] is None else int(row[7]),
                "hit": {
                    "stars": attack["stars"],
                    "destruction_percentage": attack["destruction_percentage"],
                    "trophy_change": attack["trophy_change"],
                    "battle_timestamp": attack["battle_timestamp"],
                },
                "defenses": [
                    {"stars": defense["stars"], "yours": defense["yours"]}
                    for defense in defenses
                ],
                "observed_at": None if row is None else _iso(row[6]),
            }
        )

    open_defenses, auto_each = _automatic_defense(
        today, previous, season_first_day=day.day_number == 1
    )
    return {
        "tag": tag,
        "ranked_day_start": _iso(day.start),
        "board_computed_at": _iso(board.computed_at),
        "rank_range": rank_range(board, tag),
        "legends_held": {"held": board.held, "defenses": board.defenses}
        if board.defenses
        else None,
        "open_defenses": open_defenses,
        "automatic_defense_each": auto_each,
        "opponents": opponents,
    }
