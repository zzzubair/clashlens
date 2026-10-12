"""Army types: one level-1 name for each distinct army, from its home troops.

A distinct army is an ``exact_armies`` row: the same home troops, spells and
heroes with their pets and equipment share one row however many attacks used
it. Each gets one type in ``army_package_types``; the worker saves each
completed Legend day's counts per player and distinct army in
``army_package_day_counts``; the Armies page adds those counts up by type.

So a rule change relabels the distinct armies only (``RULES_VERSION``), and
a rename changes ``TYPE_NAMES`` only: the type ids saved with each army never
change meaning. docs/domain.md says how to add a type.

The type comes from home troops weighted by camp space (count x housing
space). Heroes, pets, equipment, spells, siege machines and Clan Castle troops
never decide it; they stay saved with the army for a later second level.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from time import monotonic
from typing import Any

from . import army_analytics
from .army_decoder import DECODER_VERSION
from .catalog import CATALOG_VERSION

CATEGORY = "army-types"
# Bump on any change to the rules or camp spaces below, never for a rename.
RULES_VERSION = "army-types-v4"

# Permanent type ids: rename freely, never reuse or renumber one.
OTHER, UNREADABLE = 1, 2
THROWER, HYDRA, SUPER_BOWLER, FIREBALL_SUPER_MINION = 3, 4, 5, 6
SUPER_YETI, DRAGON_RIDER, LAVALOON, RUIN_WITCH = 7, 8, 9, 10
SUPER_ARCHER_RUIN_WITCH, ROCKET_BALLOON, ROOT_RIDER, DRAGONS = 11, 12, 13, 14
SUPER_WITCH, THROWER_RUIN_WITCH = 15, 16
TYPE_NAMES = {
    OTHER: "Other",
    UNREADABLE: "Unreadable",
    THROWER: "Thrower",
    HYDRA: "Hydra",
    SUPER_BOWLER: "Super Bowler",
    FIREBALL_SUPER_MINION: "Fireball Super Minion",
    SUPER_YETI: "Super Yeti",
    DRAGON_RIDER: "Dragon Rider",
    LAVALOON: "LaLo",
    RUIN_WITCH: "Fireball Ruin Witch",
    SUPER_ARCHER_RUIN_WITCH: "Super Archer + Ruin Witch",
    ROCKET_BALLOON: "Fireball Rocket Balloon",
    ROOT_RIDER: "Root Rider",
    DRAGONS: "Dragons",
    SUPER_WITCH: "Fireball Super Witch",
    THROWER_RUIN_WITCH: "Thrower + Ruin Witch",
}

# Camp space per home troop, from the game data (49 troops, checked against
# ClashKing's data with 0 mismatches). An army with a troop missing here is
# Unreadable until its camp space is added.
CAMP_SPACE = {
    "troop:0": 1, "troop:1": 1, "troop:2": 1, "troop:3": 5, "troop:4": 2,
    "troop:5": 5, "troop:6": 4, "troop:7": 14, "troop:8": 20, "troop:9": 25,
    "troop:10": 2, "troop:11": 5, "troop:12": 8, "troop:13": 30,
    "troop:15": 12, "troop:17": 30, "troop:22": 6, "troop:23": 10,
    "troop:24": 6, "troop:26": 5, "troop:27": 12, "troop:28": 8,
    "troop:29": 10, "troop:53": 18, "troop:55": 3, "troop:56": 24,
    "troop:57": 8, "troop:58": 15, "troop:59": 30, "troop:63": 15,
    "troop:64": 20, "troop:65": 25, "troop:66": 40, "troop:76": 40,
    "troop:80": 30, "troop:81": 40, "troop:82": 6, "troop:83": 10,
    "troop:84": 12, "troop:95": 32, "troop:97": 20, "troop:98": 12,
    "troop:109": 26, "troop:110": 20, "troop:123": 16, "troop:132": 16,
    "troop:147": 35, "troop:150": 18, "troop:177": 40,
}


@dataclass(frozen=True)
class Combination:
    """A type made of several troops: each group of troops needs at least
    its count, and all of them together at least ``share`` percent of the
    army's camp space."""

    type_id: int
    minimums: tuple[tuple[tuple[str, ...], int], ...]
    share: int

    def matches(self, quantities: Counter[str], space: dict[str, int], total: int) -> bool:
        troops = {troop for group, _count in self.minimums for troop in group}
        return all(
            sum(quantities[troop] for troop in group) >= count
            for group, count in self.minimums
        ) and sum(space.get(troop, 0) for troop in troops) * 100 >= self.share * total


# Checked first. One match names the army; two or more make it Other.
COMBINATIONS = (
    Combination(HYDRA, ((("troop:8",), 4), (("troop:65",), 2)), 50),
    Combination(LAVALOON, ((("troop:5",), 12), (("troop:17", "troop:76"), 1)), 40),
    Combination(FIREBALL_SUPER_MINION, ((("troop:84",), 4), (("troop:57",), 8)), 35),
    Combination(SUPER_ARCHER_RUIN_WITCH, ((("troop:27",), 8), (("troop:109",), 3)), 55),
)
# Support troops never name an army and are left out of the leader check:
# Healer, Druid, Ice Golem, Golem, Lava Hound, Ice Hound, Apprentice Warden,
# Headhunter, Wall Breaker, Super Wall Breaker, Archer, Barbarian, Goblin,
# Sneaky Goblin, Minion and Baby Dragon.
SUPPORT_TROOPS = frozenset({
    "troop:7", "troop:123", "troop:58", "troop:13", "troop:17", "troop:76",
    "troop:97", "troop:82", "troop:4", "troop:28", "troop:1", "troop:0",
    "troop:2", "troop:55", "troop:10", "troop:23",
})
# Otherwise the main troop (any troop not in support) with the most camp
# space names the army when it is clearly ahead of the other main troops: at
# least 3 of it, at least 35% of their camp space, and ahead of the next main
# troop by at least 10% of that space. Leaders not listed here make it Other.
LEADER_TYPES = {
    "troop:132": THROWER,
    "troop:80": SUPER_BOWLER,
    "troop:147": SUPER_YETI,
    "troop:65": DRAGON_RIDER,
    "troop:109": RUIN_WITCH,
    "troop:57": ROCKET_BALLOON,
    "troop:110": ROOT_RIDER,
    "troop:8": DRAGONS,
    "troop:66": SUPER_WITCH,
}
LEADER_MINIMUM, LEADER_SHARE, LEADER_MARGIN = 3, 35, 10
# When no main troop is clearly ahead, the two leading main troops name the
# army if they are a pair listed here, with at least 3 of each and together at
# least 50% of the main troops' camp space.
LEADING_PAIRS = {frozenset({"troop:132", "troop:109"}): THROWER_RUIN_WITCH}
PAIR_MINIMUM, PAIR_SHARE = 3, 50


def classify(home_troops: Iterable[Any]) -> int:
    """The type id for an army's home troops, as [troop id, count] pairs."""
    quantities: Counter[str] = Counter()
    for entry in home_troops:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and int(entry[1]) > 0:
            quantities[str(entry[0])] += int(entry[1])
    if any(troop not in CAMP_SPACE for troop in quantities):
        return UNREADABLE
    space = {troop: count * CAMP_SPACE[troop] for troop, count in quantities.items()}
    total = sum(space.values())
    if total == 0:
        return UNREADABLE
    matches = [rule.type_id for rule in COMBINATIONS if rule.matches(quantities, space, total)]
    if matches:
        return matches[0] if len(matches) == 1 else OTHER
    main = {troop: used for troop, used in space.items() if troop not in SUPPORT_TROOPS}
    if not main:
        return OTHER
    main_total = sum(main.values())
    ranked = sorted(main.items(), key=lambda item: (-item[1], item[0]))
    leader, lead = ranked[0]
    second, runner_up = ranked[1] if len(ranked) > 1 else (leader, 0)
    if (
        lead != runner_up
        and quantities[leader] >= LEADER_MINIMUM
        and lead * 100 >= LEADER_SHARE * main_total
        and (lead - runner_up) * 100 >= LEADER_MARGIN * main_total
    ):
        return LEADER_TYPES.get(leader, OTHER)
    pair = LEADING_PAIRS.get(frozenset({leader, second}), OTHER)
    if (
        min(quantities[leader], quantities[second]) < PAIR_MINIMUM
        or (lead + runner_up) * 100 < PAIR_SHARE * main_total
    ):
        return OTHER
    return pair


def label_new_army(connection: Any, exact_army_id: int, home_troops: Iterable[Any]) -> None:
    """Save a newly seen distinct army's type, as its attack arrives."""
    connection.execute(
        """
        INSERT INTO army_package_types (exact_army_id, type_id, rules_version)
        VALUES (%s, %s, %s) ON CONFLICT (exact_army_id) DO NOTHING
        """,
        (exact_army_id, classify(home_troops), RULES_VERSION),
    )


# The worker counts nothing in this UTC window, which keeps it free for the
# Reset, the same window past Resets wait out.
QUIET_START, QUIET_END = time(4, 30), time(7, 0)
LABEL_BATCH = 50_000
RETRY_SECONDS = 600
_retry_at = float("-inf")


def refresh_army_types(database: Any, now: datetime | None = None) -> None:
    """Label distinct armies under the current rules, then count one Legend day.

    Runs on the worker's maintenance timer. The first run under a new
    ``RULES_VERSION`` labels every saved distinct army, ``LABEL_BATCH`` per
    run; then each run counts one completed day of the current Season whose
    counts are missing or older than the day, newest first. A failure is
    reported and tried again after ``RETRY_SECONDS``.
    """
    global _retry_at
    current = (now or datetime.now(UTC)).astimezone(UTC).time()
    started = monotonic()
    if started < _retry_at or QUIET_START <= current < QUIET_END:
        return
    try:
        with database.pool.connection() as connection, connection.transaction():
            if not connection.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                ("army-type-counts",),
            ).fetchone()[0]:
                return
            event = _label(connection) or _count_day(connection)
    except Exception as error:  # noqa: BLE001 - retried after RETRY_SECONDS
        _retry_at = monotonic() + RETRY_SECONDS
        print(json.dumps({"event": "army_types", "status": "failed",
                          "error": type(error).__name__}), flush=True)
        return
    if event is not None:
        print(json.dumps({**event, "event": "army_types",
                          "seconds": round(monotonic() - started, 1)}), flush=True)


def _label(connection: Any) -> dict[str, Any] | None:
    if connection.execute(
        "SELECT 1 FROM army_type_rule_versions WHERE rules_version = %s",
        (RULES_VERSION,),
    ).fetchone():
        return None
    # Labels are being replaced, so no other version's labelling is complete.
    connection.execute(
        "DELETE FROM army_type_rule_versions WHERE rules_version <> %s", (RULES_VERSION,)
    )
    armies = connection.execute(
        """
        SELECT army.id, army.home_troops FROM exact_armies AS army
        LEFT JOIN army_package_types AS label ON label.exact_army_id = army.id
        WHERE label.exact_army_id IS NULL OR label.rules_version <> %s
        ORDER BY army.id LIMIT %s
        """,
        (RULES_VERSION, LABEL_BATCH),
    ).fetchall()
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO army_package_types (exact_army_id, type_id, rules_version)
            VALUES (%s, %s, %s)
            ON CONFLICT (exact_army_id) DO UPDATE
            SET type_id = EXCLUDED.type_id, rules_version = EXCLUDED.rules_version
            """,
            [(int(army_id), classify(troops or []), RULES_VERSION) for army_id, troops in armies],
        )
    if len(armies) < LABEL_BATCH:
        connection.execute(
            "INSERT INTO army_type_rule_versions (rules_version) VALUES (%s)",
            (RULES_VERSION,),
        )
    return {"status": "labelled", "armies": len(armies), "rules_version": RULES_VERSION}


# One row per attack of a completed Legend day: the attacker, the distinct
# army (0 when unreadable; minus its decode id when an older partly read army
# has none), stars and destruction. Army types count attacks only.
_FACTS = """
    SELECT fact.ranked_day_start, fact.population_player_id,
           COALESCE(CASE WHEN fact.army_state IN ('decoded', 'partial')
                         THEN COALESCE(decode.exact_army_id, -decode.id) END,
                    0) AS exact_army_id,
           fact.stars, fact.destruction_percentage
    FROM army_analytics_battle_facts AS fact
    LEFT JOIN battle_army_decodes AS decode ON decode.id = fact.decode_id
    WHERE fact.is_current AND fact.lens = 'offense'
      AND fact.official_season_id = %s
      AND fact.season_day_number = ANY(%s::integer[])
"""


def _count_day(connection: Any) -> dict[str, Any] | None:
    day = connection.execute(
        """
        SELECT day.ranked_day_start, day.official_season_id,
               day.season_day_number, day.fact_input_hash
        FROM army_analytics_completed_days AS day
        LEFT JOIN army_type_counted_days AS counted
          ON counted.ranked_day_start = day.ranked_day_start
        WHERE day.official_season_id = (
                SELECT official_season_id FROM army_analytics_completed_days
                ORDER BY ranked_day_start DESC LIMIT 1)
          AND counted.fact_input_hash IS DISTINCT FROM day.fact_input_hash
        ORDER BY day.ranked_day_start DESC LIMIT 1
        """
    ).fetchone()
    if day is None:
        return None
    ranked_day_start, season_id, day_number, marker = day
    # This day's older count goes; earlier Seasons' counts stay.
    for table in ("army_package_day_counts", "army_type_counted_days"):
        connection.execute(
            f"DELETE FROM {table} WHERE ranked_day_start = %s", (ranked_day_start,)
        )
    inserted = connection.execute(
        f"""
        INSERT INTO army_package_day_counts (
            ranked_day_start, population_player_id, exact_army_id,
            attacks, one_star, two_star, three_star, destruction
        )
        SELECT ranked_day_start, population_player_id, exact_army_id, count(*),
               count(*) FILTER (WHERE stars = 1), count(*) FILTER (WHERE stars = 2),
               count(*) FILTER (WHERE stars = 3), sum(destruction_percentage)
        FROM ({_FACTS}) AS fact
        GROUP BY 1, 2, 3
        """,
        (season_id, [int(day_number)]),
    ).rowcount
    connection.execute(
        "INSERT INTO army_type_counted_days (ranked_day_start, fact_input_hash) VALUES (%s, %s)",
        (ranked_day_start, marker),
    )
    return {"status": "counted", "official_season_id": str(season_id),
            "season_day_number": int(day_number), "rows": inserted}


def read_type_rows(
    connection: Any,
    *,
    season_id: str,
    days: dict[int, Any],
    member_ids: list[int] | None,
    trophy_range: tuple[int, int] | None,
    boards: tuple[dict[Any, int], int, int] | None = None,
) -> list[tuple[int, ...]]:
    """Attack totals per type for the selected players and Legend days.

    ``days`` maps each day number to its ``ranked_day_start``. Counted days
    are added up from their saved counts; days not counted yet, and trophy
    ranges, which saved counts cannot split, are added up from the day's
    attacks. An older partly read army is typed from its saved home troops.
    ``boards`` (each day's board by ``ranked_day_start``, lowest and highest
    rank) selects each day's players from that day's board instead of
    ``member_ids``. Rows are (type id, attacks, one-star, two-star,
    three-star, destruction), by type id.
    """
    counted = {
        row[0]
        for row in connection.execute(
            """
            SELECT counted.ranked_day_start
            FROM army_type_counted_days AS counted
            JOIN army_analytics_completed_days AS day
              ON day.ranked_day_start = counted.ranked_day_start
             AND day.fact_input_hash = counted.fact_input_hash
            WHERE counted.ranked_day_start = ANY(%s::timestamptz[])
            """,
            (list(days.values()),),
        ).fetchall()
    } if trophy_range is None else set()
    saved_days = [start for start in days.values() if start in counted]
    fact_days = [number for number, start in days.items() if start not in counted]
    if boards is None:
        members, member_params = "{0}population_player_id = ANY(%s::bigint[])", [member_ids]
    else:
        members = """({0}ranked_day_start, {0}population_player_id) IN (
            SELECT board.ranked_day_start, entry.player_id
            FROM unnest(%s::timestamptz[], %s::bigint[])
                AS board (ranked_day_start, snapshot_id)
            JOIN leaderboard_snapshot_entries AS entry
              ON entry.snapshot_id = board.snapshot_id
            WHERE entry.position BETWEEN %s AND %s)"""
        member_params = [list(boards[0]), list(boards[0].values()), boards[1], boards[2]]
    parts, params = [], []
    if saved_days:
        parts.append(
            f"""
            SELECT population_player_id, exact_army_id, attacks, one_star,
                   two_star, three_star, destruction
            FROM army_package_day_counts
            WHERE ranked_day_start = ANY(%s::timestamptz[])
              AND {members.format("")}
            """
        )
        params += [saved_days, *member_params]
    if fact_days:
        if trophy_range is None:
            fact_filter, filters = members.format("fact."), member_params
        else:
            fact_filter, filters = "fact.battle_time_trophies BETWEEN %s AND %s", [*trophy_range]
        parts.append(
            f"""
            SELECT population_player_id, exact_army_id, 1, (stars = 1)::int,
                   (stars = 2)::int, (stars = 3)::int, destruction_percentage
            FROM ({_FACTS} AND {fact_filter}) AS fact
            """
        )
        params += [season_id, fact_days, *filters]
    totals: dict[int, list[int]] = {}
    for type_id, troops, unnamed, *counts in connection.execute(
        f"""
        SELECT CASE WHEN source.exact_army_id = 0 THEN {UNREADABLE}
                    ELSE label.type_id END,
               decode.home_troops, decode.unresolved_components,
               sum(attacks), sum(one_star), sum(two_star), sum(three_star),
               sum(destruction)
        FROM ({" UNION ALL ".join(parts)}) AS source (
            population_player_id, exact_army_id, attacks, one_star, two_star,
            three_star, destruction)
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = source.exact_army_id
        LEFT JOIN battle_army_decodes AS decode ON decode.id = -source.exact_army_id
        GROUP BY 1, 2, 3
        """,
        params,
    ).fetchall():
        if troops is not None:
            type_id = classify(army_analytics.with_unnamed_ids(
                {"home_troops": troops, "unresolved_components": unnamed}
            )["home_troops"])
        total = totals.setdefault(type_id or OTHER, [0] * 5)
        for index, value in enumerate(counts):
            total[index] += int(value)
    return [(type_id, *total) for type_id, total in sorted(totals.items())]


def read_type_outcomes(
    connection: Any,
    *,
    season_id: str,
    day_numbers: list[int],
    lens: str,
    player_id: int,
) -> list[tuple[str, int, int, int, int]]:
    """One tracked player's attacks or defenses per type, stars and
    destruction on completed Legend days; a defense's type is the attacker's
    army. Rows are (lens, type id, stars, destruction, battles).
    """
    totals: Counter[tuple[str, int, int, int]] = Counter()
    for fact_lens, type_id, troops, unnamed, stars, destruction, battles in connection.execute(
        f"""
        SELECT fact.lens,
               CASE WHEN decode.id IS NULL
                      OR fact.army_state NOT IN ('decoded', 'partial')
                    THEN {UNREADABLE} ELSE label.type_id END,
               CASE WHEN decode.exact_army_id IS NULL THEN decode.home_troops END,
               CASE WHEN decode.exact_army_id IS NULL
                    THEN decode.unresolved_components END,
               fact.stars, fact.destruction_percentage, count(*)
        FROM army_analytics_battle_facts AS fact
        LEFT JOIN battle_army_decodes AS decode ON decode.id = fact.decode_id
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = decode.exact_army_id
        WHERE fact.is_current AND fact.official_season_id = %s
          AND fact.season_day_number = ANY(%s::integer[])
          AND fact.lens = %s AND fact.population_player_id = %s
        GROUP BY 1, 2, 3, 4, 5, 6
        """,
        (season_id, day_numbers, lens, player_id),
    ).fetchall():
        if type_id != UNREADABLE and troops is not None:
            # An older partly read army, typed from its saved home troops.
            type_id = classify(army_analytics.with_unnamed_ids(
                {"home_troops": troops, "unresolved_components": unnamed}
            )["home_troops"])
        key = (str(fact_lens), int(type_id or OTHER), int(stars), int(destruction))
        totals[key] += int(battles)
    return [(*key, battles) for key, battles in sorted(totals.items())]


def type_result(
    connection: Any,
    selection: Any,
    *,
    days: dict[int, Any],
    day_markers: dict[int, str],
    member_ids: list[int] | None,
    trophy_range: tuple[int, int] | None,
    boards: tuple[dict[Any, int], int, int] | None = None,
) -> tuple[dict[str, Any], str]:
    """The Armies page result for army types, and its evidence hash, which
    covers the selected days' completed-day markers and boards.

    Every attack counts once, under exactly one type, Other and Unreadable
    included, so the shares add up to 100%.
    """
    rows = read_type_rows(
        connection, season_id=selection.season, days=days,
        member_ids=member_ids, trophy_range=trophy_range, boards=boards,
    )
    total = sum(row[1] for row in rows)
    unreadable = sum(row[1] for row in rows if row[0] == UNREADABLE)
    sort_field = {
        "usage-rate": "usage_rate", "usage-count": "usage_count",
        "three-star-rate": "three_star_rate", "average-stars": "average_stars",
        "average-destruction": "average_destruction",
    }[selection.sort]
    result_rows = sorted(
        type_result_rows(rows, total), key=lambda row: (-row[sort_field], row["type_id"])
    )
    result = {
        "kind": "army-analytics", "total_attacks": total,
        "usable_army_sample": total - unreadable,
        "army_states": {"fully_decoded": total - unreadable, "unreadable": unreadable},
        "army_states_sum_confirmed": True,
        "unknown_affected_attacks": 0, "unknown_component_occurrences": 0,
        "perspective_disagreement_count": 0,
        "missing_trophy_membership_evidence": 0,
        "collection_coverage": {"state": "complete", "completed_days": len(days)},
        "freshness": {"state": "frozen"},
        "versions": {"decoder": DECODER_VERSION, "catalog": CATALOG_VERSION,
                     "analytics": RULES_VERSION},
        "rows": result_rows,
    }
    source_hash = hashlib.sha256(json.dumps(
        {"rules": RULES_VERSION, "rows": rows, "days": sorted(day_markers.items()),
         "selection": selection.as_dict(),
         "players": member_ids, "trophies": trophy_range,
         "boards": None if boards is None else sorted(boards[0].values())},
        separators=(",", ":"),
    ).encode()).hexdigest()
    return result, source_hash


def rules_ready(connection: Any) -> bool:
    """Whether every saved distinct army has its type under these rules."""
    return connection.execute(
        "SELECT 1 FROM army_type_rule_versions WHERE rules_version = %s",
        (RULES_VERSION,),
    ).fetchone() is not None


def type_result_rows(rows: list[tuple[int, ...]], total_attacks: int) -> list[dict[str, Any]]:
    """Armies page rows, one per type, in the shape the unit rows use."""
    result = []
    for type_id, attacks, one, two, three, destruction in rows:
        star_counts = [attacks - one - two - three, one, two, three]
        star_rates = [count / attacks if attacks else 0 for count in star_counts]
        result.append({
            "key": f"army-type:{type_id}", "label": TYPE_NAMES.get(type_id, f"Type {type_id}"),
            "type_id": type_id, "usage_count": attacks, "usage_denominator": total_attacks,
            "usage_rate": attacks / total_attacks if total_attacks else 0,
            "star_counts": star_counts, "star_rates": star_rates,
            "three_star_rate": star_rates[3],
            "average_stars": (one + 2 * two + 3 * three) / attacks if attacks else 0,
            "average_destruction": destruction / attacks if attacks else 0,
            "unknown_excluded_attacks": 0,
        })
    return result
