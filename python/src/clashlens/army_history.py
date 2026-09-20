"""Season usage by unit, quantity and battle-time trophy bucket."""
from __future__ import annotations

from collections import Counter
from typing import Any

from .catalog import catalog_name, is_siege_troop, is_valid_typed_id

HISTORY_CATEGORIES = frozenset({"troops", "spells", "heroes", "pets", "equipment"})
HISTORY_READ_CATEGORIES = HISTORY_CATEGORIES | {"siege"}
HISTORY_SORTS = frozenset({"usage-rate", "usage-count"})
_NAMESPACE = {"troop": "troops", "spell": "spells", "hero": "heroes",
              "pet": "pets", "equipment": "equipment"}
_TROPHY_BUCKET_SIZE = 100


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


def _trophy_bucket(trophies: int | None) -> int | None:
    if trophies is None:
        return None
    return trophies // _TROPHY_BUCKET_SIZE * _TROPHY_BUCKET_SIZE


def aggregate_usage(facts: list[dict[str, Any]]) -> dict[str, list[list[Any]]]:
    """[typed ID, using battles, [[quantity, trophy bucket, battles]]]."""
    usage_counts: Counter[str] = Counter()
    evidence_counts: dict[str, Counter[tuple[int, int | None]]] = {}
    for fact in facts:
        if fact["army_state"] not in {"decoded", "partial"}:
            continue
        trophy_bucket = _trophy_bucket(fact.get("battle_time_trophies"))
        for typed_id, quantity in home_units(fact).items():
            usage_counts[typed_id] += 1
            evidence_counts.setdefault(typed_id, Counter())[quantity, trophy_bucket] += 1
    result: dict[str, list[list[Any]]] = {key: [] for key in HISTORY_CATEGORIES}
    for typed_id, count in sorted(usage_counts.items()):
        evidence = [
            [quantity, trophy_bucket, evidence_count]
            for (quantity, trophy_bucket), evidence_count in sorted(
                evidence_counts[typed_id].items(),
                key=lambda item: (item[0][0],
                                  -1 if item[0][1] is None else item[0][1]),
            )
        ]
        result[_NAMESPACE[typed_id.split(":", 1)[0]]].append(
            [typed_id, count, evidence]
        )
    return result


def usage_rows(
    stored: list[list[Any]], category: str, denominator: int
) -> tuple[list[dict], bool]:
    rows = []
    unresolved = False
    for typed_id, count, evidence in stored:
        known = is_valid_typed_id(typed_id)
        if typed_id.startswith("troop:"):
            if not known:
                # The encoded troop namespace cannot establish troop vs siege.
                unresolved = True
                continue
            if is_siege_troop(typed_id) != (category == "siege"):
                continue
        unresolved |= not known
        rows.append({
            "key": typed_id,
            "unit_id": typed_id,
            "label": catalog_name(typed_id),
            "usage_count": count,
            "usage_denominator": denominator,
            "usage_rate": count / denominator if denominator else 0,
            "quantity_trophy_groups": [
                {
                    "quantity": quantity,
                    "battle_trophy_min": trophy_bucket,
                    "battle_trophy_max": (
                        None if trophy_bucket is None
                        else trophy_bucket + _TROPHY_BUCKET_SIZE - 1
                    ),
                    "usage_count": evidence_count,
                }
                for quantity, trophy_bucket, evidence_count in evidence
            ],
        })
    return rows, unresolved
