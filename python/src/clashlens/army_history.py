"""Whole-season usage and star counts by unit ID and quantity."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .catalog import catalog_name, is_siege_troop, is_valid_typed_id

HISTORY_CATEGORIES = frozenset({"troops", "spells", "heroes", "pets", "equipment"})
HISTORY_READ_CATEGORIES = HISTORY_CATEGORIES | {"siege"}
HISTORY_SORTS = frozenset({"usage-rate", "usage-count"})
_NAMESPACE = {"troop": "troops", "spell": "spells", "hero": "heroes",
              "pet": "pets", "equipment": "equipment"}


def home_units(fact: dict[str, Any]) -> Counter[str]:
    """Flatten current evidence to ID/quantity; discard clan-castle sources.

    Troop IDs keep their namespace even when their siege classification is not
    yet known. Nothing here stores relationships between different units.
    """
    units: Counter[str] = Counter()
    for field in ("home_troops", "spells", "siege"):
        for entry in fact.get(field, []):
            if len(entry) > 2 and entry[2] == "clan_castle":
                continue
            units[str(entry[0])] += int(entry[1])
    for hero in fact.get("heroes", []):
        units[str(hero["hero"])] += 1
        if hero.get("pet"):
            units[str(hero["pet"])] += 1
        units.update(hero.get("equipment", []))
    for entry in fact.get("unresolved_components", []):
        origin = str(entry.get("origin", ""))
        section = entry.get("section")
        numeric_id = entry.get("numeric_id")
        if numeric_id is None or origin == "clan_castle" or section in {"i", "d"}:
            continue
        namespace = {"u": "troop", "s": "spell"}.get(section)
        if section == "h":
            namespace = ("pet" if origin.endswith(":pet") else
                         "equipment" if origin.endswith(":equipment") else
                         "hero" if origin == "hero" else None)
        if namespace is None:
            continue
        typed_id = f"{namespace}:{numeric_id}"
        # Unknown heroes already have a hero entry to hold known equipment.
        if namespace == "hero":
            units.setdefault(typed_id, 1)
        else:
            units[typed_id] += int(entry.get("quantity", 1))
    return units


def aggregate_usage(facts: list[dict[str, Any]]) -> dict[str, list[list[Any]]]:
    """[typed ID, quantity, uses, one-star, two-star, three-star]."""
    counts: dict[tuple[str, int], list[int]] = {}
    for fact in facts:
        if fact["army_state"] not in {"decoded", "partial"}:
            continue
        stars = int(fact["stars"])
        for typed_id, quantity in home_units(fact).items():
            totals = counts.setdefault((typed_id, quantity), [0, 0, 0, 0])
            totals[0] += 1
            if stars:
                totals[stars] += 1
    result: dict[str, list[list[Any]]] = {key: [] for key in HISTORY_CATEGORIES}
    for (typed_id, quantity), totals in sorted(counts.items()):
        result[_NAMESPACE[typed_id.split(":", 1)[0]]].append(
            [typed_id, quantity, *totals]
        )
    return result


def usage_rows(
    stored: list[list[Any]], category: str, denominator: int
) -> tuple[list[dict], bool]:
    rows = []
    unresolved = False
    for typed_id, quantity, count, one_star, two_star, three_star in stored:
        known = is_valid_typed_id(typed_id)
        # Unclassified IDs appear explicitly ambiguous in both views until
        # the catalogue can determine which category owns them.
        if typed_id.startswith("troop:") and known and is_siege_troop(typed_id) != (category == "siege"):
            continue
        unresolved |= not known
        namespace, numeric_id = typed_id.split(":", 1)
        kind = "troop or siege" if namespace == "troop" else namespace
        rows.append({
            "key": f"{typed_id}@{quantity}",
            "unit_id": typed_id,
            "label": catalog_name(typed_id) or f"Unknown {kind} (ID {numeric_id})",
            "quantity": quantity,
            "usage_count": count,
            "usage_denominator": denominator,
            "usage_rate": count / denominator if denominator else 0,
            "one_star_count": one_star,
            "two_star_count": two_star,
            "three_star_count": three_star,
        })
    return rows, unresolved
