"""The projected place at the next Reset: one estimated place for every
player, always inside the outer range (api_dashboard.rank_range).

The dashboard imagines the rest of the Legend day 200 times, in a separate
process after each read of its shared board, so no request waits for it.
In each, every player's trophy change from now to the Reset is copied from
a player who, at
the same 15-minute step of one of the last 7 finished Legend days, was in the
same situation: the same attacks and defenses still open, the same activity
(all 8 attacks the day before, 1-7, none but some in the two days before, or
none in three days) and the same level (Top 200, 201-1,000, or below that
the same trophy bracket). A situation with fewer than 40 examples drops the
level, then the activity; one with no examples at all copies the nearest
open attacks and defenses, kept to what the player's own open slots allow.
Each imagined day copies one whole earlier day, so a busy or quiet night
moves everyone together.

Everyone is ranked in each imagined day by the Daily board's order for equal
trophies (analytics.latest_change_order_key): the latest trophy change
first, then no change that day, then the tag hash. A player whose imagined
day has a trophy-changing battle left, even battles that net to zero,
changed after everyone's known battles, in a random order among those who
also did. The projected place is the middle of a player's 200 imagined
places.

Tested on this Season's Days 1-7 (12 October 2026), the held-back Sunday's
typical miss was about 570 places at 06:00 UTC, 290 at 17:00, 100 at 23:00
and 50 at 01:00. Players who had finished their battles were typically
25-35 places off; one with a battle left at 23:00, about 250-300.

The worker saves each finished day's examples (``refresh_remaining_changes``)
from its Daily board and battles, every day that is due in one run, so its
first run saves all of the last 7 days at once: a battle counts from a
minute after it was first saved, as the tests measured, and a player's
trophies at each step are their Daily board total less the battles not
counted yet. A report corrected later counts with its corrected trophies
from that first save, a small known gap. The live dashboard instead counts
a battle once the player's published log has it, usually a few minutes
later, so its examples are a little ahead of what the card knows.
"""

from __future__ import annotations

import json
import random
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from operator import itemgetter
from time import monotonic
from typing import Any

from .analytics import latest_change_order_key
from .domain import MAX_BATTLE_TROPHIES, ranked_day_for
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

SLOT = timedelta(minutes=15)
SLOTS = 96
DAYS = 7
DRAWS = 200
MIN_POOL = 40
MIN_DAY_POOL = 20
# A battle counts from this long after it was first saved.
GUARD = timedelta(seconds=60)
# Those who move are ordered at random among this many tie numbers each.
SPREAD = 1 << 20
# Below rank 1,000, level is the trophy bracket: under 4,800, 4,800s, 4,900s,
# 5,000s, 5,100s, 5,200-5,399 and 5,400+.
TROPHY_EDGES = (4800, 4900, 5000, 5100, 5200, 5400)
LEVELS = 2 + len(TROPHY_EDGES) + 1
ACTIVITIES = 4
# A day counted from an older Daily board is counted again from the newest
# one at most this often.
RECOUNT_AFTER = timedelta(hours=6)
REFRESH_SECONDS = 60
RETRY_SECONDS = 600
_next_refresh_at = float("-inf")

# Each player's attacks on the Legend day before %(day_start)s, and on the
# two days before that, from their newest published log of each day.
RECENT_ATTACKS_SQL = """
LEFT JOIN LATERAL (
    SELECT COALESCE(sum(latest.attack_count) FILTER (
               WHERE latest.ranked_day_start = %(day_start)s - interval '1 day'), 0)
               AS yesterday,
           COALESCE(sum(latest.attack_count) FILTER (
               WHERE latest.ranked_day_start < %(day_start)s - interval '1 day'), 0)
               AS before
    FROM (
        SELECT DISTINCT ON (recent.ranked_day_start)
               recent.ranked_day_start, recent.attack_count
        FROM api_player_daily_logs AS recent
        WHERE recent.player_id = player.id
          AND recent.ranked_day_start >= %(day_start)s - interval '3 days'
          AND recent.ranked_day_start < %(day_start)s
        ORDER BY recent.ranked_day_start, recent.version DESC
    ) AS latest
) AS recent ON true
"""

# The game's "no opponent, no battle" attack and defense slots of the
# player's newest published log for the Legend day %(day_start)s, which the
# game counts as used.
ZERO_SLOTS_SQL = """
LEFT JOIN LATERAL (
    SELECT COALESCE((ranked_day.input_evidence ->> 'zero_result_attack_slots')::int, 0)
               AS attacks,
           COALESCE((ranked_day.input_evidence ->> 'zero_result_defense_slots')::int, 0)
               AS defenses
    FROM api_player_daily_logs AS daily
    JOIN ranked_day_versions AS ranked_day ON ranked_day.id = daily.ranked_day_version_id
    WHERE daily.player_id = player.id AND daily.ranked_day_start = %(day_start)s
    ORDER BY daily.version DESC
    LIMIT 1
) AS zero ON true
"""


def activity(yesterday: int, before: int) -> int:
    """0: no attacks in three days; 1: none yesterday but some before;
    2: 1-7 yesterday; 3: all 8 yesterday."""
    if yesterday >= MAX_DAILY_ATTACKS:
        return 3
    if yesterday > 0:
        return 2
    return 1 if before > 0 else 0


def situations(
    trophies: Sequence[int],
    open_attacks: Sequence[int],
    open_defenses: Sequence[int],
    activities: Sequence[int],
    places: Sequence[float] | None = None,
) -> list[int]:
    """Each player's situation as one number: open attacks, open defenses,
    activity and level. ``situation // LEVELS`` drops the level and
    ``situation // (LEVELS * ACTIVITIES)`` the activity too. ``places``
    gives each player's place on the board when they aren't all of it."""
    ordered = sorted(trophies)
    n = len(ordered)
    out = []
    for index, (points, attacks, defenses, act) in enumerate(zip(
        trophies, open_attacks, open_defenses, activities, strict=True
    )):
        # The middle of the places this player shares with equal trophies.
        middle = places[index] if places is not None else (
            (1 + 2 * n - bisect_right(ordered, points) - bisect_left(ordered, points)) / 2)
        if middle <= 200:
            level = 0
        elif middle <= 1000:
            level = 1
        else:
            level = 2 + bisect_right(TROPHY_EDGES, points)
        out.append(
            ((attacks * (MAX_DAILY_DEFENSES + 1) + defenses) * ACTIVITIES + act) * LEVELS
            + level
        )
    return out


def _open_slots(situation: int) -> tuple[int, int]:
    """Open attacks and open defenses of ``situation``."""
    return divmod(situation // (LEVELS * ACTIVITIES), MAX_DAILY_DEFENSES + 1)


def _open_bounds(situation: int) -> tuple[int, int]:
    """The most a player in ``situation`` can lose and win before the Reset."""
    attacks, defenses = _open_slots(situation)
    return -MAX_BATTLE_TROPHIES * defenses, MAX_BATTLE_TROPHIES * attacks


# ---------------------------------------------------------------------------
# Worker: saving each finished day's examples.


def _now() -> datetime:
    return datetime.now(UTC)


def refresh_remaining_changes(database: Any) -> None:
    """Save the finished Legend days' examples that are due, on the worker's
    maintenance timer: every day of the last 7 whose Daily board has
    published and is not saved yet, or was saved from an older board over 6
    hours ago, one day at a time. Days older than 7 are deleted. A failure
    is reported and tried again after RETRY_SECONDS."""
    global _next_refresh_at
    if monotonic() < _next_refresh_at:
        return
    _next_refresh_at = monotonic() + REFRESH_SECONDS
    now = _now()
    for _ in range(DAYS):
        started = monotonic()
        try:
            saved = _refresh(database, now)
        except Exception as error:  # noqa: BLE001 - retried after RETRY_SECONDS
            _next_refresh_at = monotonic() + RETRY_SECONDS
            print(
                json.dumps({
                    "event": "dashboard_remaining_changes",
                    "status": "failed",
                    "error": type(error).__name__,
                }),
                flush=True,
            )
            return
        if saved is None:
            return
        print(
            json.dumps({
                "event": "dashboard_remaining_changes",
                "status": "complete",
                **saved,
                "seconds": round(monotonic() - started, 1),
            }),
            flush=True,
        )


def _refresh(database: Any, now: datetime) -> dict[str, Any] | None:
    today = ranked_day_for(now).start
    with database.pool.connection() as connection, connection.transaction():
        if not connection.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
            ("dashboard-remaining-changes",),
        ).fetchone()[0]:
            return None
        connection.execute(
            "DELETE FROM dashboard_remaining_changes WHERE ranked_day_start < %s",
            (today - timedelta(days=DAYS),),
        )
        due = connection.execute(
            """
            SELECT day.start, board.id
            FROM generate_series(%(first)s::timestamptz, %(last)s::timestamptz,
                                 interval '1 day') AS day (start)
            CROSS JOIN LATERAL (
                SELECT id FROM leaderboard_snapshots
                WHERE snapshot_kind = 'frozen' AND state = 'published'
                  AND boundary_at = day.start + interval '1 day'
                ORDER BY version DESC LIMIT 1
            ) AS board
            LEFT JOIN LATERAL (
                SELECT saved.snapshot_id, saved.built_at
                FROM dashboard_remaining_changes AS saved
                WHERE saved.ranked_day_start = day.start
                LIMIT 1
            ) AS saved ON true
            WHERE saved.snapshot_id IS NULL
               OR (saved.snapshot_id <> board.id AND saved.built_at < %(recount)s)
            ORDER BY day.start DESC
            LIMIT 1
            """,
            {
                "first": today - timedelta(days=DAYS),
                "last": today - timedelta(days=1),
                "recount": now - RECOUNT_AFTER,
            },
        ).fetchone()
        if due is None:
            return None
        day_start, snapshot_id = due
        rows = build_day(connection, day_start, int(snapshot_id))
        connection.execute(
            "DELETE FROM dashboard_remaining_changes WHERE ranked_day_start = %s",
            (day_start,),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO dashboard_remaining_changes (
                    ranked_day_start, slot, snapshot_id, situations, changes, moved
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (day_start, slot, snapshot_id, keys, changes, moved)
                    for slot, (keys, changes, moved) in enumerate(rows)
                ],
            )
        return {
            "ranked_day_start": day_start.astimezone(UTC).isoformat(),
            "snapshot_id": int(snapshot_id),
            "players": len(rows[0][0]),
        }


def build_day(
    connection: Any, day_start: datetime, snapshot_id: int
) -> list[tuple[list[int], list[int], list[bool]]]:
    """For each 15-minute step of the day, every board player's situation,
    trophy change to the end of the day and whether a battle that moved
    their trophies was still to come, ordered by situation then change.
    A player's "no opponent, no battle" slots count as used all day."""
    players = connection.execute(
        f"""
        SELECT entry.player_id, entry.trophies, recent.yesterday, recent.before,
               COALESCE(zero.attacks, 0), COALESCE(zero.defenses, 0)
        FROM leaderboard_snapshot_entries AS entry
        JOIN players AS player ON player.id = entry.player_id
        {RECENT_ATTACKS_SQL}{ZERO_SLOTS_SQL}
        WHERE entry.snapshot_id = %(snapshot_id)s
        ORDER BY entry.position
        """,
        {"snapshot_id": snapshot_id, "day_start": day_start},
    ).fetchall()
    index = {int(row[0]): position for position, row in enumerate(players)}
    final = [int(row[1]) for row in players]
    activities = [activity(int(row[2]), int(row[3])) for row in players]
    # (known at, player, is attack, trophy change)
    events: list[tuple[datetime, int, bool, int]] = []
    # The Daily board's total is taken at exactly the Reset, so a battle
    # whose attacker's report is stamped at or after it is not in it
    # (boundary_manifest.reset_trophies).
    for attacker, defender, saved_at, gain, loss in connection.execute(
        """
        SELECT battle.attacker_player_id, battle.defender_player_id,
               battle.created_at,
               COALESCE(attacker.attacker_gain, defender.attacker_gain, 0),
               COALESCE(defender.defender_loss, attacker.defender_loss, 0)
        FROM legend_battles AS battle
        LEFT JOIN battle_perspectives AS attacker_side
          ON attacker_side.battle_id = battle.id AND attacker_side.perspective = 'attacker'
        LEFT JOIN battle_evidence AS attacker ON attacker.id = attacker_side.evidence_id
        LEFT JOIN battle_perspectives AS defender_side
          ON defender_side.battle_id = battle.id AND defender_side.perspective = 'defender'
        LEFT JOIN battle_evidence AS defender ON defender.id = defender_side.evidence_id
        WHERE battle.ranked_day_start = %(day_start)s
          AND (attacker.battle_timestamp IS NULL
               OR attacker.battle_timestamp < %(day_end)s)
        """,
        {"day_start": day_start, "day_end": day_start + timedelta(days=1)},
    ):
        known = saved_at + GUARD
        if (player := index.get(int(attacker))) is not None:
            events.append((known, player, True, int(gain)))
        if (player := index.get(int(defender))) is not None:
            events.append((known, player, False, -int(loss)))
    events.sort(key=itemgetter(0))
    # Each player's trophy change from battles not counted yet, and how many
    # of those moved their trophies.
    remaining = [0] * len(players)
    moving = [0] * len(players)
    for _known, player, _attack, change in events:
        remaining[player] += change
        moving[player] += change != 0
    attacks = [int(row[4]) for row in players]
    defenses = [int(row[5]) for row in players]
    rows = []
    counted = 0
    for slot in range(SLOTS):
        cutoff = day_start + slot * SLOT
        while counted < len(events) and events[counted][0] <= cutoff:
            _known, player, attack, change = events[counted]
            remaining[player] -= change
            moving[player] -= change != 0
            if attack:
                attacks[player] += 1
            else:
                defenses[player] += 1
            counted += 1
        keys = situations(
            [total - left for total, left in zip(final, remaining, strict=True)],
            [max(0, MAX_DAILY_ATTACKS - used) for used in attacks],
            [max(0, MAX_DAILY_DEFENSES - used) for used in defenses],
            activities,
        )
        triples = []
        for key, change, left in zip(keys, remaining, moving, strict=True):
            lowest, highest = _open_bounds(key)
            triples.append(
                (key, min(highest, max(lowest, change)), left > 0 and lowest < highest)
            )
        triples.sort()
        rows.append((
            [key for key, _, _ in triples],
            [change for _, change, _ in triples],
            [moved for _, _, moved in triples],
        ))
    return rows


# ---------------------------------------------------------------------------
# Dashboard: the projected places, worked out after each board read.


def read_examples(
    connection: Any, now: datetime
) -> list[tuple[list[int], list[int], list[bool]]] | None:
    """The saved examples for the current 15-minute step of the last 7
    finished days: one (situations, changes, moved) per day, or None
    without any."""
    day = ranked_day_for(now)
    slot = min(SLOTS - 1, max(0, int((now - day.start) / SLOT)))
    rows = connection.execute(
        """
        SELECT situations, changes, moved FROM dashboard_remaining_changes
        WHERE slot = %s AND ranked_day_start >= %s AND ranked_day_start < %s
        ORDER BY ranked_day_start
        """,
        (slot, day.start - timedelta(days=DAYS), day.start),
    ).fetchall()
    return [
        (list(keys), list(changes), list(moved)) for keys, changes, moved in rows
    ] or None


def _nearest(open_slots: dict[int, list[int]], situation: int) -> int | None:
    """The open attacks and defenses with examples nearest ``situation``'s,
    as ``situation // (LEVELS * ACTIVITIES)``, or None without any."""
    attacks, defenses = _open_slots(situation)
    return min(
        open_slots,
        key=lambda slots: (
            abs(slots // (MAX_DAILY_DEFENSES + 1) - attacks)
            + abs(slots % (MAX_DAILY_DEFENSES + 1) - defenses),
            slots,
        ),
        default=None,
    )


class ProjectedPlaces:
    """Every board player's place in 200 imagined days, kept as each
    player's ranking key per imagined day."""

    def __init__(
        self,
        trophies: Sequence[int],
        open_attacks: Sequence[int],
        open_defenses: Sequence[int],
        activities: Sequence[int],
        latest_changes: Sequence[datetime | None],
        tags: Sequence[str],
        examples: list[tuple[list[int], list[int], list[bool]]],
        seed: int,
        places: Sequence[float] | None = None,
    ) -> None:
        keys = situations(trophies, open_attacks, open_defenses, activities, places)
        size = len(keys)
        # The examples by situation, then without level, then by open slots
        # only: pooled over the days and for each day. An example's change is
        # None when no battle that moved their trophies was still to come.
        steps = (1, LEVELS, LEVELS * ACTIVITIES)
        pooled: list[dict[int, list[int | None]]] = [{} for _ in steps]
        by_day: list[list[dict[int, list[int | None]]]] = []
        for day_keys, day_changes, day_moved in examples:
            day: list[dict[int, list[int | None]]] = [{} for _ in steps]
            for key, change, moved in zip(day_keys, day_changes, day_moved, strict=True):
                example = change if moved else None
                for step, everyday, today in zip(steps, pooled, day, strict=True):
                    everyday.setdefault(key // step, []).append(example)
                    today.setdefault(key // step, []).append(example)
            by_day.append(day)
        # Among equal trophies, a larger tie number places first: the
        # board's order for players whose trophies stay put, and above
        # everyone who stays put for those who move.
        order = sorted(
            range(size), key=lambda player: latest_change_order_key(
                latest_changes[player], tags[player]
            )
        )
        tie = [0] * size
        for place, player in enumerate(order):
            tie[player] = size - 1 - place
        width = (SPREAD + 2) * size
        groups: dict[int, list[int]] = {}
        for player, key in enumerate(keys):
            groups.setdefault(key, []).append(player)
        # Players in situation order, so each imagined day is built one
        # situation at a time; ``position`` finds a player in that order.
        self.position = [0] * size
        # For each day, each situation's players' ranking keys if they stay
        # put, and the examples, None when no one in them moved.
        day_pools: list[list[tuple[list[int], list[int | None] | None]]] = [
            [] for _ in by_day
        ]
        placed = 0
        for key, members in groups.items():
            for player in members:
                self.position[player] = placed
                placed += 1
            still = [trophies[player] * width + tie[player] for player in members]
            level = next(
                (
                    level for level, step in enumerate(steps)
                    if len(pooled[level].get(key // step, ())) >= MIN_POOL
                ),
                None,
            )
            lowest = highest = None
            if level is not None:
                pool_key = key // steps[level]
            elif key // steps[-1] in pooled[-1]:
                level, pool_key = len(steps) - 1, key // steps[-1]
            else:
                level = len(steps) - 1
                pool_key = _nearest(pooled[-1], key)
                lowest, highest = _open_bounds(key)
            choice = pooled[level].get(pool_key, [None])
            for day, pools in zip(by_day, day_pools, strict=True):
                pool = day[level].get(pool_key)
                if pool is None or len(pool) < MIN_DAY_POOL:
                    pool = choice
                if lowest is not None:
                    pool = [
                        None if change is None or lowest == highest
                        else min(highest, max(lowest, change))
                        for change in pool
                    ]
                moves = any(change is not None for change in pool)
                pools.append((still, pool if moves else None))
        rng = random.Random(seed)
        draw = rng.random
        self.size = size
        self.keys: list[array] = []
        for _ in range(DRAWS):
            ranked: list[int] = []
            for still, pool in day_pools[rng.randrange(len(day_pools))]:
                if pool is None:
                    ranked.extend(still)
                    continue
                count = len(pool)
                # Those who move rank above those who stay put on the same
                # trophies, at random by what is left of ``pick``, then by
                # tie number, so no two keys are equal.
                ranked.extend([
                    stays if (change := pool[int(pick := draw() * count)]) is None
                    else stays + change * width + size * (1 + int(pick % 1 * SPREAD))
                    for stays in still
                ])
            self.keys.append(array("q", ranked))

    def middles(self) -> list[int]:
        """Every player's projected place before it is kept inside their
        outer range, in the order given: the middle of their places on the
        imagined days, each day sorted once."""
        n = self.size
        columns = []
        for keys in self.keys:
            places = [0] * n
            for index, position in enumerate(sorted(range(n), key=keys.__getitem__)):
                places[position] = n - index
            columns.append(places)
        by_position = [_middle(sorted(places)) for places in zip(*columns, strict=True)]
        return [by_position[position] for position in self.position]


def _middle(places: list[int]) -> int:
    """The middle of one player's ``DRAWS`` sorted places."""
    return (places[DRAWS // 2 - 1] + places[DRAWS // 2] + 1) // 2


def middle_places(*inputs: Any, seed: int) -> list[int]:
    """``ProjectedPlaces(*inputs, seed=seed).middles()``: what the dashboard
    works out in its separate process and keeps until the next."""
    return ProjectedPlaces(*inputs, seed=seed).middles()
