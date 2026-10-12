"""The likely place at the next Reset: the places a player finishes in about
9 times in 10, always inside the outer range (api_dashboard.rank_range).

The dashboard imagines the rest of the Legend day 200 times. In each, every
player's trophy change from now to the Reset is copied from a player who, at
the same 15-minute step of one of the last 7 finished Legend days, was in the
same situation: the same attacks and defenses still open, the same activity
(all 8 attacks the day before, 1-7, none but some in the two days before, or
none in three days) and the same level (Top 200, 201-1,000, or below that
the same trophy bracket). A situation with fewer than 40 examples drops the
level, then the activity. Each imagined day copies one whole earlier day, so
a busy or quiet night moves everyone together. Everyone is ranked in each,
a tied player anywhere in their tied group, and the likely range runs from a
player's 10th-best to 190th-best place of the 200.

Tested on this Season's Days 5-7 (12 October 2026): 89-91% of players on the
held-back Sunday finished inside from 06:00 to 20:00 UTC, 83-87% from 02:00
to 04:00 on a quiet night, misses mostly under 25 places.

The worker saves each finished day's examples (``refresh_remaining_changes``)
from its Daily board and battles: a battle counts from a minute after it was
first saved, and a player's trophies at each step are their Daily board total
less the battles not counted yet.
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

from .domain import MAX_BATTLE_TROPHIES, ranked_day_for
from .reconciliation import MAX_DAILY_ATTACKS, MAX_DAILY_DEFENSES

SLOT = timedelta(minutes=15)
SLOTS = 96
DAYS = 7
DRAWS = 200
# The 10th-best and 190th-best of 200 imagined places.
BEST_DRAW = 9
WORST_DRAW = 189
MIN_POOL = 40
MIN_DAY_POOL = 20
# A battle counts from this long after it was first saved.
GUARD = timedelta(seconds=60)
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
) -> list[int]:
    """Each player's situation as one number: open attacks, open defenses,
    activity and level. ``situation // LEVELS`` drops the level and
    ``situation // (LEVELS * ACTIVITIES)`` the activity too."""
    ordered = sorted(trophies)
    n = len(ordered)
    out = []
    for points, attacks, defenses, act in zip(
        trophies, open_attacks, open_defenses, activities, strict=True
    ):
        # The middle of the places this player shares with equal trophies.
        middle = (1 + 2 * n - bisect_right(ordered, points) - bisect_left(ordered, points)) / 2
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


def _open_bounds(situation: int) -> tuple[int, int]:
    """The most a player in ``situation`` can lose and win before the Reset."""
    slots = situation // (LEVELS * ACTIVITIES)
    attacks, defenses = divmod(slots, MAX_DAILY_DEFENSES + 1)
    return -MAX_BATTLE_TROPHIES * defenses, MAX_BATTLE_TROPHIES * attacks


# ---------------------------------------------------------------------------
# Worker: saving each finished day's examples.


def _now() -> datetime:
    return datetime.now(UTC)


def refresh_remaining_changes(database: Any) -> None:
    """Save one finished Legend day's examples when due, on the worker's
    maintenance timer: a day of the last 7 whose Daily board has published
    and is not saved yet, or was saved from an older board over 6 hours ago.
    Days older than 7 are deleted. A failure is reported and tried again
    after RETRY_SECONDS."""
    global _next_refresh_at
    started = monotonic()
    if started < _next_refresh_at:
        return
    _next_refresh_at = started + REFRESH_SECONDS
    try:
        saved = _refresh(database, _now())
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
    if saved is not None:
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
                    ranked_day_start, slot, snapshot_id, situations, changes
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                [
                    (day_start, slot, snapshot_id, keys, changes)
                    for slot, (keys, changes) in enumerate(rows)
                ],
            )
        return {
            "ranked_day_start": day_start.astimezone(UTC).isoformat(),
            "snapshot_id": int(snapshot_id),
            "players": len(rows[0][0]),
        }


def build_day(
    connection: Any, day_start: datetime, snapshot_id: int
) -> list[tuple[list[int], list[int]]]:
    """For each 15-minute step of the day, every board player's situation and
    trophy change to the end of the day, ordered by situation then change."""
    players = connection.execute(
        f"""
        SELECT entry.player_id, entry.trophies, recent.yesterday, recent.before
        FROM leaderboard_snapshot_entries AS entry
        JOIN players AS player ON player.id = entry.player_id
        {RECENT_ATTACKS_SQL}
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
        WHERE battle.ranked_day_start = %s
        """,
        (day_start,),
    ):
        known = saved_at + GUARD
        if (player := index.get(int(attacker))) is not None:
            events.append((known, player, True, int(gain)))
        if (player := index.get(int(defender))) is not None:
            events.append((known, player, False, -int(loss)))
    events.sort(key=itemgetter(0))
    # Each player's trophy change from battles not counted yet.
    remaining = [0] * len(players)
    for _known, player, _attack, change in events:
        remaining[player] += change
    attacks = [0] * len(players)
    defenses = [0] * len(players)
    rows = []
    counted = 0
    for slot in range(SLOTS):
        cutoff = day_start + slot * SLOT
        while counted < len(events) and events[counted][0] <= cutoff:
            _known, player, attack, change = events[counted]
            remaining[player] -= change
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
        pairs = []
        for key, change in zip(keys, remaining, strict=True):
            lowest, highest = _open_bounds(key)
            pairs.append((key, min(highest, max(lowest, change))))
        pairs.sort()
        rows.append(([key for key, _ in pairs], [change for _, change in pairs]))
    return rows


# ---------------------------------------------------------------------------
# Dashboard: the likely range, recomputed with each board read.


def read_examples(
    connection: Any, now: datetime
) -> list[tuple[list[int], list[int]]] | None:
    """The saved examples for the current 15-minute step of the last 7
    finished days: one (situations, changes) per day, or None without any."""
    day = ranked_day_for(now)
    slot = min(SLOTS - 1, max(0, int((now - day.start) / SLOT)))
    rows = connection.execute(
        """
        SELECT situations, changes FROM dashboard_remaining_changes
        WHERE slot = %s AND ranked_day_start >= %s AND ranked_day_start < %s
        ORDER BY ranked_day_start
        """,
        (slot, day.start - timedelta(days=DAYS), day.start),
    ).fetchall()
    return [(list(keys), list(changes)) for keys, changes in rows] or None


class LikelyPlaces:
    """Every board player's imagined finishing trophies in 200 imagined
    days, kept as one array per imagined day with a sorted copy, so one
    player's range takes 400 binary searches."""

    def __init__(
        self,
        trophies: Sequence[int],
        open_attacks: Sequence[int],
        open_defenses: Sequence[int],
        activities: Sequence[int],
        examples: list[tuple[list[int], list[int]]],
        seed: int,
    ) -> None:
        keys = situations(trophies, open_attacks, open_defenses, activities)
        by_day: list[dict[int, list[int]]] = []
        pooled: list[dict[int, list[int]]] = [{}, {}, {}]
        for day_keys, day_changes in examples:
            day: dict[int, list[int]] = {}
            for key, change in zip(day_keys, day_changes, strict=True):
                day.setdefault(key, []).append(change)
                pooled[0].setdefault(key, []).append(change)
                pooled[1].setdefault(key // LEVELS, []).append(change)
                pooled[2].setdefault(key // (LEVELS * ACTIVITIES), []).append(change)
            by_day.append(day)
        groups: dict[int, list[int]] = {}
        for player, key in enumerate(keys):
            groups.setdefault(key, []).append(player)
        # Players in situation order, so each imagined day is built one
        # situation at a time; ``position`` finds a player in that order.
        self.position = [0] * len(keys)
        # For each day, each situation's players' trophies and examples, the
        # examples None when every one is 0.
        day_pools: list[list[tuple[list[int], list[int] | None]]] = [
            [] for _ in by_day
        ]
        placed = 0
        for key, members in groups.items():
            for player in members:
                self.position[player] = placed
                placed += 1
            current = [trophies[player] for player in members]
            fine = pooled[0].get(key)
            if fine is not None and len(fine) >= MIN_POOL:
                choice, own = fine, True
            else:
                own = False
                choice = pooled[1].get(key // LEVELS)
                if choice is None or len(choice) < MIN_POOL:
                    choice = pooled[2].get(key // (LEVELS * ACTIVITIES), [0])
            for day, pools in zip(by_day, day_pools, strict=True):
                pool = day.get(key) if own else None
                if pool is None or len(pool) < MIN_DAY_POOL:
                    pool = choice
                pools.append((current, pool if any(pool) else None))
        rng = random.Random(seed)
        draw = rng.random
        self.size = len(keys)
        self.finals: list[array] = []
        self.sorted: list[array] = []
        for _ in range(DRAWS):
            finals: list[int] = []
            for current, pool in day_pools[rng.randrange(len(day_pools))]:
                if pool is None:
                    finals.extend(current)
                    continue
                count = len(pool)
                finals.extend([points + pool[int(draw() * count)] for points in current])
            self.finals.append(array("i", finals))
            finals.sort()
            self.sorted.append(array("i", finals))

    def range(self, player: int, best: int, worst: int) -> dict[str, int]:
        """``player``'s likely best and worst place, inside ``best``-``worst``."""
        n = self.size
        player = self.position[player]
        highs = sorted(
            1 + n - bisect_right(ordered, finals[player])
            for finals, ordered in zip(self.finals, self.sorted, strict=True)
        )
        lows = sorted(
            n - bisect_left(ordered, finals[player])
            for finals, ordered in zip(self.finals, self.sorted, strict=True)
        )
        return {
            "best": min(worst, max(best, highs[BEST_DRAW])),
            "worst": min(worst, max(best, lows[WORST_DRAW])),
        }
