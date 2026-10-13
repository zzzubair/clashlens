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

The projected place inside it (projected_place.py) is recomputed with each
board read from the same trophies and open slots, when each player's newest
battle that moved their trophies in that log ended by the Daily board's
rule (the attacker's report time, from the attacker's own log, else 155
seconds after the defender's), their attacks on the three Legend days
before today, and the saved examples of the last 7 days.

The board for that comparison, and today's held share across it, are read in
one query and kept for a minute, so dashboards share one read per minute.

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

import time
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from . import army_types, projected_place
from .api_db import ApiDatabase, _screen_events, _text
from .api_leaderboard import _LIVE_PLAYERS_SQL, _season_params
from .domain import (
    CHAIN_BREAK_REASONS,
    MAX_BATTLE_TROPHIES,
    allocate_trophies,
    ranked_day_for,
)
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

BOARD_SECONDS = 60
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

# A battle a published log keeps, as ``event``, with a report time to read.
_KEPT_EVENT_SQL = """event ->> 'battle_timestamp' ~ '^[0-9]{4}-[0-9][0-9]-[0-9][0-9]T'
      AND event ->> 'included' IS DISTINCT FROM 'false'
      AND event ->> 'valid' IS DISTINCT FROM 'false'"""

_BOARD_SQL = f"""
WITH live AS MATERIALIZED ({_LIVE_PLAYERS_SQL}),
board AS MATERIALIZED (
    SELECT live.normalized_tag, live.trophies, player.id AS player_id,
           recent.yesterday, recent.before, day.attack_count, day.attack_gain,
           day.defense_count, day.defense_loss, day.defense_three_star_count,
           day.ranked_day_version_id, day.battles
    FROM live
    JOIN players AS player ON player.normalized_tag = live.normalized_tag
    {projected_place.RECENT_ATTACKS_SQL}LEFT JOIN LATERAL (
        SELECT attack_count, attack_gain, defense_count, defense_loss,
               defense_three_star_count, ranked_day_version_id, battles
        FROM api_player_daily_logs AS daily
        WHERE daily.player_id = player.id
          AND daily.ranked_day_start = %(day_start)s
        ORDER BY daily.version DESC
        LIMIT 1
    ) AS day ON true
),
-- Each battle those logs keep: the player's side, their report's time and
-- the opponent.
report AS MATERIALIZED (
    SELECT board.player_id, event ->> 'lens' AS lens,
           event ->> 'battle_identity' AS battle,
           event -> 'opponent' ->> 'tag' AS opponent,
           (event ->> 'battle_timestamp')::timestamptz AS reported_at,
           event ->> 'trophy_change' <> '0' AS moved
    FROM board
    CROSS JOIN LATERAL jsonb_array_elements(CASE jsonb_typeof(board.battles)
                                            WHEN 'array' THEN board.battles
                                            ELSE '[]' END) AS event
    WHERE {_KEPT_EVENT_SQL}
),
-- Attackers' own reports of those battles: in the board's logs, and for a
-- defense none of those has, in its attacker's newest log of the day,
-- whether or not the attacker is on the board.
attack AS MATERIALIZED (
    SELECT battle, reported_at FROM report WHERE lens = 'offense'
    UNION ALL
    SELECT event ->> 'battle_identity', (event ->> 'battle_timestamp')::timestamptz
    FROM (
        SELECT DISTINCT player.id
        FROM report AS own
        JOIN players AS player ON player.normalized_tag = own.opponent
        WHERE own.lens = 'defense' AND own.moved
          AND NOT EXISTS (
              SELECT 1 FROM report AS known
              WHERE known.lens = 'offense' AND known.battle = own.battle)
    ) AS attacker
    CROSS JOIN LATERAL (
        SELECT daily.battles
        FROM api_player_daily_logs AS daily
        WHERE daily.player_id = attacker.id
          AND daily.ranked_day_start = %(day_start)s
        ORDER BY daily.version DESC
        LIMIT 1
    ) AS day
    CROSS JOIN LATERAL jsonb_array_elements(CASE jsonb_typeof(day.battles)
                                            WHEN 'array' THEN day.battles
                                            ELSE '[]' END) AS event
    WHERE event ->> 'lens' = 'offense' AND {_KEPT_EVENT_SQL}
),
-- When each player's newest battle that moved their trophies ended, by the
-- Daily board's rule (analytics.latest_trophy_changes): at the attacker's
-- report, from the attacker's own log, else 155 seconds after the
-- defender's.
latest_change AS (
    SELECT own.player_id,
           max(CASE own.lens
                   WHEN 'defense' THEN COALESCE(
                       attack.reported_at, own.reported_at + interval '155 seconds')
                   ELSE own.reported_at
               END) AS at
    FROM report AS own
    LEFT JOIN attack ON own.lens = 'defense' AND attack.battle = own.battle
    WHERE own.moved
    GROUP BY own.player_id
)
SELECT board.normalized_tag, board.trophies, ranked_day.start_trophies,
       board.attack_count, board.attack_gain, board.defense_count,
       board.defense_loss, board.defense_three_star_count,
       COALESCE((ranked_day.input_evidence ->> 'zero_result_attack_slots')::int, 0),
       COALESCE((ranked_day.input_evidence ->> 'zero_result_defense_slots')::int, 0),
       board.yesterday, board.before, latest_change.at
FROM board
LEFT JOIN latest_change ON latest_change.player_id = board.player_id
LEFT JOIN ranked_day_versions AS ranked_day
    ON ranked_day.id = board.ranked_day_version_id
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
    # Distinct day start trophies, ascending, and the defenses and held
    # defenses of players who started below each (one more entry than starts).
    starts: list[int]
    defenses_below: list[int]
    held_below: list[int]
    # tag -> its index in ``projected``, which is None until examples are saved.
    players: dict[str, int]
    projected: projected_place.ProjectedPlaces | None


def _open(used: Any, allowed: int) -> int:
    return max(0, allowed - (0 if used is None else int(used)))


def _read_board(connection: Any, now: datetime) -> _Board:
    day_start = ranked_day_for(now).start
    rows = connection.execute(
        _BOARD_SQL, {**_season_params(now), "day_start": day_start}
    ).fetchall()
    ends: dict[str, tuple[int, int]] = {}
    held = defenses = 0
    # Day start trophies -> [defenses, held defenses].
    by_start: dict[int, list[int]] = {}
    # tag -> (trophies now, open attacks, open defenses, activity, latest
    # trophy change)
    inputs: dict[str, tuple[int, int, int, int, datetime | None]] = {}
    for (
        tag, trophies, start, attacks, gained, defended, lost, tripled,
        zero_attacks, zero_defenses, yesterday, before, latest_change_at,
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
        inputs[_text(tag)] = (
            basis, open_attacks, open_defenses,
            projected_place.activity(int(yesterday), int(before)),
            latest_change_at,
        )
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
    examples = projected_place.read_examples(connection, now) if inputs else None
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
        players={tag: index for index, tag in enumerate(inputs)},
        projected=None if examples is None else projected_place.ProjectedPlaces(
            *(list(column) for column in zip(*inputs.values(), strict=True)),
            list(inputs),
            examples,
            # Dashboards read in the same minute imagine the same days.
            seed=int(now.timestamp()) // 60,
        ),
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


def projected_rank(board: _Board, tag: str) -> int | None:
    """The place ``tag`` is projected to finish today on, inside its rank
    range; None off the board or before any day's examples are saved."""
    outer = rank_range(board, tag)
    if outer is None or board.projected is None:
        return None
    return board.projected.projected(board.players[tag], outer["best"], outer["worst"])


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
    """One player's dashboard numbers for today, or None for an unknown tag.
    ``opponents`` is None until the player's log for today is published."""
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
                "legends_held": similar_held(board, reset_trophies),
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
        "projected_rank": projected_rank(board, tag),
        # Every player's, only for the website released before each base had
        # its own; never a base's comparison.
        "legends_held": {"held": board.held, "defenses": board.defenses}
        if board.defenses
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
