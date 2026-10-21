"""The Notebook's rankings: the armies that did best and worst against one
base, and the Season's Top 3 bases.

Both read the same per-day defense counts as a base's record
(army_package_defense_day_counts), on the counted Legend days the base ran,
so they always agree with it. A defense's army is the attacker's, named by
its army type as the dashboard's Armies on defense card names it; Other and
unreadable armies are left out of the ranking, as that card leaves them out.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from fractions import Fraction
from typing import Any

from . import army_analytics, army_types

# An army is ranked against a base once it has this many defenses on it.
FEWEST_ARMY_DEFENSES = 3
# A base is in the Top 3 once it has this many defenses: two full Legend days.
FEWEST_TOP_DEFENSES = 16
TOP = 3
_UNNAMED = frozenset({army_types.OTHER, army_types.UNREADABLE})


def _average(row: dict[str, Any]) -> Fraction:
    return Fraction(row["trophies_lost"], row["defenses"])


def base_armies(
    connection: Any, days: Iterable[tuple[int, datetime]]
) -> list[dict[str, Any]] | None:
    """Each army type with at least 3 defenses on the given (player, Legend
    day start) days, fewest trophies lost a defense first: its defenses,
    held (not 3-starred) and trophies lost. None while the worker is still
    labelling armies under new type rules."""
    if not army_types.rules_ready(connection):
        return None
    days = list(days)
    if not days:
        return []
    totals: dict[int, list[int]] = {}
    for type_id, troops, unnamed, *counts in connection.execute(
        f"""
        SELECT CASE WHEN saved.exact_army_id = 0 THEN {army_types.UNREADABLE}
                    ELSE label.type_id END,
               decode.home_troops, decode.unresolved_components,
               sum(saved.defenses), sum(saved.three_star), sum(saved.trophies_lost)
        FROM unnest(%s::bigint[], %s::timestamptz[]) AS ran (player_id, start)
        JOIN army_package_defense_day_counts AS saved
          ON saved.ranked_day_start = ran.start
         AND saved.population_player_id = ran.player_id
        LEFT JOIN army_package_types AS label
          ON label.exact_army_id = saved.exact_army_id
        LEFT JOIN battle_army_decodes AS decode ON decode.id = -saved.exact_army_id
        GROUP BY 1, 2, 3
        """,
        ([player_id for player_id, _ in days], [start for _, start in days]),
    ).fetchall():
        if troops is not None:
            # An older partly read army, typed from its saved home troops.
            type_id = army_types.classify(army_analytics.with_unnamed_ids(
                {"home_troops": troops, "unresolved_components": unnamed}
            )["home_troops"])
        total = totals.setdefault(type_id or army_types.OTHER, [0, 0, 0])
        for index, value in enumerate(counts):
            total[index] += int(value)
    rows = [
        {
            "type_id": type_id,
            "name": army_types.TYPE_NAMES.get(type_id, f"Type {type_id}"),
            "defenses": defenses,
            "held": defenses - three,
            "trophies_lost": lost,
        }
        for type_id, (defenses, three, lost) in totals.items()
        if type_id not in _UNNAMED and defenses >= FEWEST_ARMY_DEFENSES
    ]
    return sorted(rows, key=lambda row: (_average(row), -row["defenses"], row["type_id"]))


def top_bases(records: dict[str, tuple[str, dict[str, Any]]]) -> list[str]:
    """The Season's Top 3 bases, by base ID, from {base ID: (name, record)}:
    fewest trophies lost a defense among bases with at least 16 defenses,
    more defenses first on a tie, then by name."""
    ranked = sorted(
        (
            (_average(record), -record["defenses"], name.casefold(), code)
            for code, (name, record) in records.items()
            if record["defenses"] >= FEWEST_TOP_DEFENSES
        ),
    )
    return [code for *_, code in ranked[:TOP]]
