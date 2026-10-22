"""Army types: one level-1 name for each distinct army, from its home troops,
and one variant inside it, from its heroes' equipment.

A distinct army is an ``exact_armies`` row: the same home troops, spells and
heroes with their pets and equipment share one row however many attacks used
it. Each gets one type and variant in ``army_package_types``; the worker saves each
completed Legend day's counts per player and distinct army in
``army_package_day_counts``; the Armies page and the dashboard's Armies on
offense card add those counts up by type. Defenses are counted the same way,
per defender and distinct army, in ``army_package_defense_day_counts``: a
defense's army is the attacker's, read from the defender's own battle report,
and the dashboard's Armies on defense card adds those up by type.

So a rule change relabels the distinct armies only (``RULES_VERSION``), and
a rename changes ``TYPE_NAMES`` or ``VARIANTS`` names only: the type and
variant ids saved with each army never change meaning. docs/domain.md says how
to add a type or a variant.

The type comes from home troops weighted by camp space (count x housing
space). Heroes, pets, equipment, spells, siege machines and Clan Castle troops
never decide it. The variant comes from hero equipment only, by the hand-kept
list in ``VARIANTS``.
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
from .domain import DomainRuleError, allocate_trophies

CATEGORY = "army-types"
# Bump on any change to the rules, camp spaces or variant list below, never
# for a rename. v5 is v4's types plus the first variant list.
RULES_VERSION = "army-types-v5"

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


# Variants: per type, (permanent variant id, name, equipment the army's heroes
# must all carry), checked in order. The first match names the army; an army
# matching none is its type's Other, variant 0. Names are the equipment word
# plus the type, as Clashers say them. Rename freely; never reuse or renumber
# an id. A new variant gets the next unused id and a RULES_VERSION bump.
OTHER_VARIANT = 0
QUEEN, WARDEN, PRINCE, DUKE = "hero:1", "hero:2", "hero:6", "hero:7"
MONOLITH, GIANT_ARROW, MAGIC_MIRROR = (
    (QUEEN, "equipment:16"), (QUEEN, "equipment:17"), (QUEEN, "equipment:39"))
FIREBALL, ETERNAL_TOME, LAVALOON_PUPPET = (
    (WARDEN, "equipment:22"), (WARDEN, "equipment:4"), (WARDEN, "equipment:41"))
PORTAL_PENDANT = (PRINCE, "equipment:61")
REVENGE, BACKPACK, FIRE_HEART = (
    (DUKE, "equipment:60"), (DUKE, "equipment:53"), (DUKE, "equipment:52"))
STUN_BLASTER, FLAME_BLOWER, ELECTRO_FANGS = (
    (DUKE, "equipment:56"), (DUKE, "equipment:57"), (DUKE, "equipment:59"))
VARIANTS: dict[int, tuple[tuple[int, str, tuple[tuple[str, str], ...]], ...]] = {
    THROWER: (
        (1, "Fireball Thrower", (FIREBALL,)),
        (2, "Revenge Backpack Thrower", (REVENGE, BACKPACK)),
        (3, "Revenge Thrower", (REVENGE,)),
        (4, "Backpack Thrower", (BACKPACK,)),
    ),
    HYDRA: (
        (5, "Revenge Backpack Hydra", (REVENGE, BACKPACK)),
        (6, "Revenge Hydra", (REVENGE,)),
        (7, "Backpack Hydra", (BACKPACK,)),
    ),
    # The Queen's Monolith Arrow first, then the Duke, as other trackers split it.
    SUPER_BOWLER: (
        (8, "Monolith Super Bowler", (MONOLITH,)),
        (9, "Electro Fangs Super Bowler", (ELECTRO_FANGS,)),
        (10, "Revenge Super Bowler", (REVENGE,)),
    ),
    FIREBALL_SUPER_MINION: (
        (11, "Revenge Super Minion", (REVENGE,)),
        (12, "Fire Heart Super Minion", (FIRE_HEART,)),
    ),
    SUPER_YETI: (
        (13, "Magic Mirror Super Yeti", (MAGIC_MIRROR,)),
        (14, "Giant Arrow Super Yeti", (GIANT_ARROW,)),
    ),
    DRAGON_RIDER: (
        (15, "Revenge Dragon Rider", (REVENGE,)),
        (16, "Backpack Dragon Rider", (BACKPACK,)),
        (17, "Electro Fangs Dragon Rider", (ELECTRO_FANGS,)),
    ),
    RUIN_WITCH: (
        (18, "Revenge Fireball Ruin Witch", (REVENGE,)),
        (19, "Backpack Fireball Ruin Witch", (BACKPACK,)),
    ),
    # The Minion Prince's Portal Pendant first: nearly all of them also carry
    # the Warden's Fireball.
    LAVALOON: (
        (20, "Portal Pendant LaLo", (PORTAL_PENDANT,)),
        (21, "Fireball LaLo", (FIREBALL,)),
        (22, "Lavaloon Puppet LaLo", (LAVALOON_PUPPET,)),
        (23, "Eternal Tome LaLo", (ETERNAL_TOME,)),
    ),
    THROWER_RUIN_WITCH: (
        (24, "Fireball Thrower + Ruin Witch", (FIREBALL,)),
        (25, "Revenge Thrower + Ruin Witch", (REVENGE,)),
    ),
    SUPER_ARCHER_RUIN_WITCH: (
        (26, "Revenge Backpack Super Archer + Ruin Witch", (REVENGE, BACKPACK)),
        (27, "Revenge Super Archer + Ruin Witch", (REVENGE,)),
        (28, "Backpack Super Archer + Ruin Witch", (BACKPACK,)),
    ),
    ROCKET_BALLOON: (
        (29, "Flame Blower Rocket Balloon", (FLAME_BLOWER,)),
        (30, "Stun Blaster Rocket Balloon", (STUN_BLASTER,)),
        (31, "Revenge Backpack Rocket Balloon", (REVENGE, BACKPACK)),
        (32, "Revenge Rocket Balloon", (REVENGE,)),
        (33, "Backpack Rocket Balloon", (BACKPACK,)),
    ),
    SUPER_WITCH: (
        (34, "Revenge Backpack Super Witch", (REVENGE, BACKPACK)),
        (35, "Revenge Super Witch", (REVENGE,)),
        (36, "Backpack Super Witch", (BACKPACK,)),
    ),
    ROOT_RIDER: (
        (37, "Flame Blower Root Rider", (FLAME_BLOWER,)),
        (38, "Stun Blaster Root Rider", (STUN_BLASTER,)),
        (39, "Revenge Backpack Root Rider", (REVENGE, BACKPACK)),
        (40, "Revenge Root Rider", (REVENGE,)),
        (41, "Backpack Root Rider", (BACKPACK,)),
    ),
}


def variant(type_id: int, heroes: Iterable[Any]) -> int:
    """The variant id for an army of ``type_id`` with these saved heroes."""
    carried = {
        (str(hero.get("hero")), str(item))
        for hero in heroes if isinstance(hero, dict)
        for item in hero.get("equipment") or ()
    }
    for variant_id, _name, items in VARIANTS.get(type_id, ()):
        if all(item in carried for item in items):
            return variant_id
    return OTHER_VARIANT


def label_new_army(
    connection: Any, exact_army_id: int, home_troops: Iterable[Any], heroes: Iterable[Any]
) -> None:
    """Save a newly seen distinct army's type and variant, as its attack arrives."""
    type_id = classify(home_troops)
    connection.execute(
        """
        INSERT INTO army_package_types (exact_army_id, type_id, variant_id, rules_version)
        VALUES (%s, %s, %s, %s) ON CONFLICT (exact_army_id) DO NOTHING
        """,
        (exact_army_id, type_id, variant(type_id, heroes), RULES_VERSION),
    )


# The worker counts nothing in this UTC window, which keeps it free for the
# Reset, the same window past Resets wait out.
QUIET_START, QUIET_END = time(4, 30), time(7, 0)
LABEL_BATCH = 50_000
# The dashboard's Armies on offense card compares up to the last 7 finished
# Legend days with these counts, so those days are counted whatever their
# Season.
OFFENSE_CARD_DAYS = 7
# The Armies on defense card looks back up to 28 finished Legend days. Each
# day's defenses are about 88,000 rows, so a day is counted at most once a
# minute: after a deploy the current Season fills in under half an hour.
DEFENSE_CARD_DAYS = 28
DEFENSE_PACE_SECONDS = 60
RETRY_SECONDS = 600
_retry_at = float("-inf")
_defense_at = float("-inf")


def refresh_army_types(database: Any, now: datetime | None = None) -> None:
    """Label distinct armies under the current rules, then count one Legend day.

    Runs on the worker's maintenance timer. The first run under a new
    ``RULES_VERSION`` labels every saved distinct army, ``LABEL_BATCH`` per
    run; then each run counts one completed day of the current Season, or of
    the last ``OFFENSE_CARD_DAYS``, whose counts are missing or older than the
    day, newest first. Once no attack count is due, it counts one day's
    defenses the same way, for the current Season or the last
    ``DEFENSE_CARD_DAYS``, at most once every ``DEFENSE_PACE_SECONDS``. A
    failure is reported and tried again after ``RETRY_SECONDS``.
    """
    global _retry_at, _defense_at
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
            event = _label(connection) or _count_day(connection, "offense")
            if event is None and started >= _defense_at:
                event = _count_day(connection, "defense")
                if event is not None:
                    _defense_at = monotonic() + DEFENSE_PACE_SECONDS
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
        SELECT army.id, army.home_troops, army.heroes FROM exact_armies AS army
        LEFT JOIN army_package_types AS label ON label.exact_army_id = army.id
        WHERE label.exact_army_id IS NULL OR label.rules_version <> %s
        ORDER BY army.id LIMIT %s
        """,
        (RULES_VERSION, LABEL_BATCH),
    ).fetchall()
    labels = []
    for army_id, troops, heroes in armies:
        type_id = classify(troops or [])
        labels.append((int(army_id), type_id, variant(type_id, heroes or []), RULES_VERSION))
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO army_package_types (exact_army_id, type_id, variant_id, rules_version)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (exact_army_id) DO UPDATE
            SET type_id = EXCLUDED.type_id, variant_id = EXCLUDED.variant_id,
                rules_version = EXCLUDED.rules_version
            """,
            labels,
        )
    if len(armies) < LABEL_BATCH:
        connection.execute(
            "INSERT INTO army_type_rule_versions (rules_version) VALUES (%s)",
            (RULES_VERSION,),
        )
    return {"status": "labelled", "armies": len(armies), "rules_version": RULES_VERSION}


def _facts(lens: str) -> str:
    """One row per attack or defense of a completed Legend day: the tracked
    attacker or defender, the distinct army (0 when unreadable; minus its
    decode id when an older partly read army has none), stars and
    destruction."""
    return f"""
    SELECT fact.ranked_day_start, fact.population_player_id,
           COALESCE(CASE WHEN fact.army_state IN ('decoded', 'partial')
                         THEN COALESCE(decode.exact_army_id, -decode.id) END,
                    0) AS exact_army_id,
           fact.stars, fact.destruction_percentage
    FROM army_analytics_battle_facts AS fact
    LEFT JOIN battle_army_decodes AS decode ON decode.id = fact.decode_id
    WHERE fact.is_current AND fact.lens = '{lens}'
      AND fact.official_season_id = %s
      AND fact.season_day_number = ANY(%s::integer[])
"""


# The Armies page counts attacks only.
_FACTS = _facts("offense")
# Per lens: the counts table, its battle column, its counted-days table, how
# many recent days are counted whatever their Season, and its trophies column.
_COUNTS = {
    "offense": ("army_package_day_counts", "attacks", "army_type_counted_days",
                OFFENSE_CARD_DAYS, "trophies"),
    "defense": ("army_package_defense_day_counts", "defenses",
                "army_type_defense_counted_days", DEFENSE_CARD_DAYS, "trophies_lost"),
}
# Every 3-star wins these; a miss wins fewer.
THREE_STAR_TROPHIES = allocate_trophies(3, 100).attacker_gain
# Joins a battle's stars and destruction to the trophies it moved (_trophies).
_TROPHIES_JOIN = """
    LEFT JOIN unnest(%s::integer[], %s::integer[], %s::integer[])
        AS moved (stars, destruction, trophies)
      ON moved.stars = fact.stars
     AND moved.destruction = fact.destruction_percentage"""


def _trophies(lens: str) -> tuple[list[int], list[int], list[int]]:
    """Trophies an attacker wins, or a defender loses, for each possible
    stars and destruction, from the game's Legend trophy table."""
    rows = []
    for stars in range(4):
        for destruction in range(101):
            try:
                allocation = allocate_trophies(stars, destruction)
            except DomainRuleError:
                continue
            rows.append((stars, destruction, allocation.attacker_gain
                         if lens == "offense" else allocation.defender_loss))
    return ([row[0] for row in rows], [row[1] for row in rows],
            [row[2] for row in rows])


def _count_day(connection: Any, lens: str) -> dict[str, Any] | None:
    counts_table, battles, counted_table, recent_days, trophies = _COUNTS[lens]
    day = connection.execute(
        f"""
        SELECT day.ranked_day_start, day.official_season_id,
               day.season_day_number, day.fact_input_hash
        FROM army_analytics_completed_days AS day
        LEFT JOIN {counted_table} AS counted
          ON counted.ranked_day_start = day.ranked_day_start
        CROSS JOIN (
            SELECT official_season_id, ranked_day_start
            FROM army_analytics_completed_days
            ORDER BY ranked_day_start DESC LIMIT 1) AS newest
        WHERE (day.official_season_id = newest.official_season_id
               OR day.ranked_day_start
                  > newest.ranked_day_start - %s * interval '1 day')
          AND counted.fact_input_hash IS DISTINCT FROM day.fact_input_hash
        ORDER BY day.ranked_day_start DESC LIMIT 1
        """,
        (recent_days,),
    ).fetchone()
    if day is None:
        return None
    ranked_day_start, season_id, day_number, marker = day
    connection.execute(
        f"DELETE FROM {counted_table} WHERE ranked_day_start = %s", (ranked_day_start,)
    )
    # This day's older count goes; earlier Seasons' counts stay. Only rows
    # that changed are written: a recount mostly repeats the last one, and
    # deleting and saving the whole day again wrote about 1 GB of change log
    # a day on production. Attacks save the trophies they won, defenses the
    # trophies they lost.
    same = f"""saved.population_player_id = fresh.population_player_id
              AND saved.exact_army_id = fresh.exact_army_id
              AND saved.{battles} = fresh.{battles}
              AND saved.one_star = fresh.one_star
              AND saved.two_star = fresh.two_star
              AND saved.three_star = fresh.three_star
              AND saved.destruction = fresh.destruction
              AND saved.{trophies} IS NOT DISTINCT FROM fresh.{trophies}"""
    inserted = connection.execute(
        f"""
        WITH fresh AS MATERIALIZED (
            SELECT ranked_day_start, population_player_id, exact_army_id,
                   count(*) AS {battles},
                   count(*) FILTER (WHERE fact.stars = 1) AS one_star,
                   count(*) FILTER (WHERE fact.stars = 2) AS two_star,
                   count(*) FILTER (WHERE fact.stars = 3) AS three_star,
                   sum(destruction_percentage) AS destruction,
                   COALESCE(sum(moved.trophies), 0) AS {trophies}
            FROM ({_facts(lens)}) AS fact{_TROPHIES_JOIN}
            GROUP BY 1, 2, 3
        ), gone AS (
            DELETE FROM {counts_table} AS saved
            WHERE saved.ranked_day_start = %s
              AND NOT EXISTS (SELECT FROM fresh WHERE {same})
        ), added AS (
            INSERT INTO {counts_table} (
                ranked_day_start, population_player_id, exact_army_id,
                {battles}, one_star, two_star, three_star, destruction, {trophies}
            )
            SELECT * FROM fresh
            WHERE NOT EXISTS (
                SELECT FROM {counts_table} AS saved
                WHERE saved.ranked_day_start = fresh.ranked_day_start AND {same}
            )
        )
        SELECT count(*) FROM fresh
        """,
        (season_id, [int(day_number)], *_trophies(lens), ranked_day_start),
    ).fetchone()[0]
    connection.execute(
        f"INSERT INTO {counted_table} (ranked_day_start, fact_input_hash) VALUES (%s, %s)",
        (ranked_day_start, marker),
    )
    return {"status": "counted", "lens": lens, "official_season_id": str(season_id),
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
    """Attack totals per type and variant for the selected players and Legend days.

    ``days`` maps each day number to its ``ranked_day_start``. Counted days
    are added up from their saved counts; days not counted yet, and trophy
    ranges, which saved counts cannot split, are added up from the day's
    attacks. An older partly read army gets its type and variant from its
    saved home troops and heroes. ``boards`` (each day's board by
    ``ranked_day_start``, lowest and highest rank) selects each day's players
    from that day's board instead of ``member_ids``. Rows are (type id,
    variant id, attacks, one-star, two-star, three-star, destruction, trophies
    won), by type and variant id.
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
                   two_star, three_star, destruction, trophies
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
            SELECT population_player_id, exact_army_id, 1, (fact.stars = 1)::int,
                   (fact.stars = 2)::int, (fact.stars = 3)::int,
                   destruction_percentage, COALESCE(moved.trophies, 0)
            FROM ({_FACTS} AND {fact_filter}) AS fact{_TROPHIES_JOIN}
            """
        )
        params += [season_id, fact_days, *filters, *_trophies("offense")]
    rows = connection.execute(
        f"""
        SELECT CASE WHEN source.exact_army_id = 0 THEN {UNREADABLE}
                    ELSE label.type_id END,
               COALESCE(label.variant_id, {OTHER_VARIANT}),
               CASE WHEN source.exact_army_id < 0 THEN -source.exact_army_id END,
               sum(attacks), sum(one_star), sum(two_star), sum(three_star),
               sum(destruction), sum(trophies)
        FROM ({" UNION ALL ".join(parts)}) AS source (
            population_player_id, exact_army_id, attacks, one_star, two_star,
            three_star, destruction, trophies)
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = source.exact_army_id
        GROUP BY 1, 2, 3
        """,
        params,
    ).fetchall()
    # An older partly read army has no distinct army: its saved decode is
    # read once, after the totals, so the totals never join the decodes.
    partial_ids = [row[2] for row in rows if row[2] is not None]
    partial = {
        decode_id: army_analytics.with_unnamed_ids({
            "home_troops": troops, "unresolved_components": unnamed, "heroes": heroes,
        })
        for decode_id, troops, unnamed, heroes in connection.execute(
            """
            SELECT id, home_troops, unresolved_components, heroes
            FROM battle_army_decodes WHERE id = ANY(%s::bigint[])
            """,
            (partial_ids,),
        ).fetchall()
    } if partial_ids else {}
    totals: dict[tuple[int, int], list[int]] = {}
    for type_id, variant_id, decode_id, *counts in rows:
        if decode_id in partial:
            army = partial[decode_id]
            type_id = classify(army["home_troops"])
            variant_id = variant(type_id, army["heroes"] or [])
        total = totals.setdefault((type_id or OTHER, int(variant_id)), [0] * 6)
        for index, value in enumerate(counts):
            total[index] += int(value)
    return [(*key, *total) for key, total in sorted(totals.items())]


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


def read_day_totals(
    connection: Any, days: list[datetime], lens: str = "offense"
) -> dict[datetime, dict[int, list[int]]]:
    """Every tracked player's attacks or defenses per type on counted Legend
    days, from the saved counts: [attacks, three-star attacks], or
    [defenses, three-star defenses, trophies lost], by day and type id.
    """
    counts_table, battles, _counted, _recent, _trophies_column = _COUNTS[lens]
    lost = ", sum(saved.trophies_lost)" if lens == "defense" else ""
    totals: dict[datetime, dict[int, list[int]]] = {day: {} for day in days}
    for day, type_id, troops, unnamed, *counts in connection.execute(
        f"""
        SELECT saved.ranked_day_start,
               CASE WHEN saved.exact_army_id = 0 THEN {UNREADABLE}
                    ELSE label.type_id END,
               decode.home_troops, decode.unresolved_components,
               sum(saved.{battles}), sum(saved.three_star){lost}
        FROM {counts_table} AS saved
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = saved.exact_army_id
        LEFT JOIN battle_army_decodes AS decode ON decode.id = -saved.exact_army_id
        WHERE saved.ranked_day_start = ANY(%s::timestamptz[])
        GROUP BY 1, 2, 3, 4
        """,
        (days,),
    ).fetchall():
        if troops is not None:
            # An older partly read army, typed from its saved home troops.
            type_id = classify(army_analytics.with_unnamed_ids(
                {"home_troops": troops, "unresolved_components": unnamed}
            )["home_troops"])
        total = totals[day].setdefault(type_id or OTHER, [0] * len(counts))
        for index, value in enumerate(counts):
            total[index] += int(value)
    return totals


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
    total = sum(row[2] for row in rows)
    unreadable = sum(row[2] for row in rows if row[0] == UNREADABLE)
    sort_field = {
        "usage-rate": "usage_rate", "usage-count": "usage_count",
        "three-star-rate": "three_star_rate", "average-stars": "average_stars",
        "average-destruction": "average_destruction",
        "average-trophies": "average_trophies",
    }[selection.sort]
    result_rows = sorted(
        type_result_rows(rows, total),
        key=lambda row: (-row[sort_field], row["type_id"]),
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


def all_counted(
    connection: Any, days: list[tuple[datetime, str]], lens: str
) -> bool:
    """Whether every given (day start, completed-day marker) day has saved
    counts for ``lens`` made from that marker."""
    _table, _battles, counted_table, _recent, _trophies_column = _COUNTS[lens]
    counted = connection.execute(
        f"""
        SELECT count(*) FROM {counted_table} AS counted
        JOIN unnest(%s::timestamptz[], %s::text[]) AS day (start, marker)
          ON counted.ranked_day_start = day.start
         AND counted.fact_input_hash = day.marker
        """,
        ([start for start, _ in days], [marker for _, marker in days]),
    ).fetchone()[0]
    return counted == len(days)


def rules_ready(connection: Any) -> bool:
    """Whether every saved distinct army has its type under these rules."""
    return connection.execute(
        "SELECT 1 FROM army_type_rule_versions WHERE rules_version = %s",
        (RULES_VERSION,),
    ).fetchone() is not None


def type_result_rows(
    rows: list[tuple[int, ...]], total_attacks: int
) -> list[dict[str, Any]]:
    """Armies page rows, one per type, in the shape the unit rows use. A type
    with variants lists those its attacks used under ``variants``, most used
    first and its Other last, each with its share of the type."""
    types: dict[int, list[int]] = {}
    for type_id, _variant_id, *counts in rows:
        total = types.setdefault(type_id, [0] * 6)
        for index, value in enumerate(counts):
            total[index] += value
    result = []
    for type_id, counts in sorted(types.items()):
        row = _result_row(
            f"army-type:{type_id}", TYPE_NAMES.get(type_id, f"Type {type_id}"),
            counts, total_attacks,
        )
        result.append({**row, "type_id": type_id})
        if type_id not in VARIANTS:
            continue
        names = {variant_id: name for variant_id, name, _items in VARIANTS[type_id]}
        variants = []
        for row_type, variant_id, *variant_counts in rows:
            if row_type != type_id:
                continue
            variants.append({
                **_result_row(
                    f"army-variant:{type_id}:{variant_id}",
                    names.get(variant_id, f"Other {TYPE_NAMES[type_id]}"),
                    variant_counts, counts[0],
                ),
                "variant_id": variant_id,
            })
        variants.sort(key=lambda row: (row["variant_id"] == OTHER_VARIANT,
                                       -row["usage_count"], row["variant_id"]))
        result[-1]["variants"] = variants
    return result


def _result_row(
    key: str, label: str, counts: list[int], denominator: int
) -> dict[str, Any]:
    attacks, one, two, three, destruction, trophies = counts
    star_counts = [attacks - one - two - three, one, two, three]
    star_rates = [count / attacks if attacks else 0 for count in star_counts]
    return {
        "key": key, "label": label,
        "usage_count": attacks, "usage_denominator": denominator,
        "usage_rate": attacks / denominator if denominator else 0,
        "star_counts": star_counts, "star_rates": star_rates,
        "three_star_rate": star_rates[3],
        "average_stars": (one + 2 * two + 3 * three) / attacks if attacks else 0,
        "average_destruction": destruction / attacks if attacks else 0,
        "average_trophies": trophies / attacks if attacks else 0,
        # What a miss wins on average, None without a miss.
        "average_miss_trophies": (trophies - THREE_STAR_TROPHIES * three)
        / (attacks - three) if attacks > three else None,
        "unknown_excluded_attacks": 0,
    }
