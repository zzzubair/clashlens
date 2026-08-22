from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .catalog import CATALOG_VERSION, catalog_name

ARMY_ANALYTICS_RULE_VERSION = "army-analytics-v2"
LENSES = frozenset({"offense", "defense"})
CATEGORIES = frozenset(
    {
        "troops",
        "spells",
        "siege",
        "heroes",
        "pets",
        "equipment",
        "equipment-for-hero",
        "cc-troops",
        "hero-pet",
        "hero-equipment",
        "cc-composition",
    }
)
SORTS = frozenset(
    {
        "usage-rate",
        "usage-count",
        "three-star-rate",
        "average-stars",
        "average-destruction",
    }
)
TOP_PRESETS = frozenset({5, 10, 20, 50, 100, 200, 500, 1000})
RANK_BANDS = frozenset(
    {
        (1, 5),
        (6, 10),
        (11, 20),
        (21, 50),
        (51, 100),
        (101, 200),
        *((start, start + 99) for start in range(201, 1000, 100)),
    }
)


class ArmyAnalyticsUnavailable(Exception):
    """A requested inclusive range contains Legend days without a completed,
    reproducible frozen source publication."""

    def __init__(self, affected_days: list[int]) -> None:
        super().__init__(f"unavailable legend days: {affected_days}")
        self.affected_days = affected_days


class CurrentSeasonEmpty(Exception):
    """The confirmed current Legend season has no completed Legend day yet.

    The previous season is named so callers can link to it instead of
    silently serving the previous season's data for ``season=current``."""

    def __init__(self, previous_season_id: str | None) -> None:
        super().__init__("no completed legend days this season")
        self.previous_season_id = previous_season_id


@dataclass(frozen=True, slots=True)
class ArmyAnalyticsSelection:
    lens: str
    season: str
    start_day: int
    end_day: int
    population: str
    category: str
    sort: str

    @classmethod
    def parse(
        cls,
        *,
        lens: str,
        season: str,
        start_day: int,
        end_day: int,
        population: str,
        category: str,
        sort: str,
    ) -> ArmyAnalyticsSelection:
        if lens not in LENSES or category not in CATEGORIES or sort not in SORTS:
            raise ValueError("unsupported army analytics selection")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", season):
            raise ValueError("invalid season")
        if not 1 <= start_day <= end_day <= 28:
            raise ValueError("invalid Legend day range")
        _validate_population(population)
        return cls(lens, season, start_day, end_day, population, category, sort)

    def as_dict(self) -> dict[str, str | int]:
        return {
            "lens": self.lens,
            "season": self.season,
            "start_day": self.start_day,
            "end_day": self.end_day,
            "population": self.population,
            "category": self.category,
            "sort": self.sort,
        }


def build_army_result(
    facts: list[dict[str, Any]], selection: ArmyAnalyticsSelection
) -> dict[str, Any]:
    usable = [fact for fact in facts if fact["army_state"] in {"decoded", "partial"}]
    category = selection.category

    def individual_items(fact: dict[str, Any]) -> set[str]:
        field = {
            "troops": "home_troops", "spells": "spells", "siege": "siege",
            "cc-troops": "cc_troops",
        }.get(category)
        if field:
            return {str(item[0]) for item in fact[field] if isinstance(item, list) and item}
        items: set[str] = set()
        for hero in fact["heroes"]:
            if not isinstance(hero, dict):
                continue
            if category == "heroes" and hero.get("hero"):
                items.add(str(hero["hero"]))
            elif category == "pets" and hero.get("pet"):
                items.add(str(hero["pet"]))
            elif category == "equipment":
                items.update(str(item) for item in hero.get("equipment", []))
        return items

    def relationships(fact: dict[str, Any]) -> set[str]:
        result: set[str] = set()
        if category == "cc-composition":
            composition = sorted(
                (str(item[0]), int(item[1]))
                for item in fact["cc_troops"] if isinstance(item, list) and len(item) > 1
            )
            if composition:
                result.add("cc:" + ",".join(f"{item}x{qty}" for item, qty in composition))
            return result
        for hero in fact["heroes"]:
            if not isinstance(hero, dict) or not hero.get("hero"):
                continue
            hero_id = str(hero["hero"])
            pet = hero.get("pet")
            equipment = sorted(str(item) for item in hero.get("equipment", []))
            if category == "hero-pet" and pet:
                result.add(f"{hero_id}|{pet}")
            elif category == "hero-equipment" and len(equipment) == 2:
                result.add(f"{hero_id}|{','.join(equipment)}")
            elif category == "equipment-for-hero":
                result.update(f"{hero_id}|{item}" for item in equipment)
        return result

    relationship_category = category in {
        "hero-pet", "hero-equipment", "equipment-for-hero", "cc-composition"
    }
    presence = {
        fact["id"]: relationships(fact) if relationship_category else individual_items(fact)
        for fact in usable
    }
    keys = sorted({key for values in presence.values() for key in values})

    def uncertain(fact: dict[str, Any], key: str) -> bool:
        if fact["army_state"] == "decoded" or key in presence[fact["id"]]:
            return False
        unknown = fact["unresolved_components"]
        if category == "cc-composition":
            return any(item.get("section") == "i" for item in unknown)
        # Unknown evidence is scoped to the hero named by the row: an unknown
        # hero chip (origin "hero") belongs to a different hero entry and must
        # not exclude this row's proven relationships.
        hero_id = key.split("|", 1)[0]
        if category == "hero-pet":
            return any(item.get("origin") == f"{hero_id}:pet" for item in unknown)
        if category in {"hero-equipment", "equipment-for-hero"}:
            return any(
                item.get("origin") == f"{hero_id}:equipment" for item in unknown
            )
        return False

    rows = []
    for key in keys:
        excluded_unknown = 0
        if category == "equipment-for-hero":
            hero_id = key.split("|", 1)[0]
            hero_facts = [
                fact for fact in usable
                if any(hero.get("hero") == hero_id for hero in fact["heroes"] if isinstance(hero, dict))
            ]
            denominator = [fact for fact in hero_facts if not uncertain(fact, key)]
            # Exclusions are measured against the confirmed-hero denominator,
            # not against every usable attack.
            excluded_unknown = len(hero_facts) - len(denominator)
        elif relationship_category:
            denominator = [fact for fact in usable if not uncertain(fact, key)]
            excluded_unknown = len(usable) - len(denominator)
        else:
            # Individual denominators use every usable attack, so no attack is
            # excluded as unknown.
            denominator = usable
        matching = [fact for fact in denominator if key in presence[fact["id"]]]
        counts = Counter(int(fact["stars"]) for fact in matching)
        sample = len(matching)
        star_counts = [counts[index] for index in range(4)]
        star_rates = [count / sample if sample else 0 for count in star_counts]
        typed_ids = key.replace("cc:", "").replace("|", ",").split(",")
        label = " + ".join(catalog_name(item.split("x", 1)[0]) or item for item in typed_ids)
        rows.append({
            "key": key, "label": label, "usage_count": sample,
            "usage_denominator": len(denominator),
            "usage_rate": sample / len(denominator) if denominator else 0,
            "star_counts": star_counts, "star_rates": star_rates,
            "three_star_rate": star_rates[3],
            "average_stars": sum(int(fact["stars"]) for fact in matching) / sample if sample else 0,
            "average_destruction": sum(int(fact["destruction_percentage"]) for fact in matching) / sample if sample else 0,
            "unknown_excluded_attacks": excluded_unknown,
        })
    sort_field = {
        "usage-rate": "usage_rate", "usage-count": "usage_count",
        "three-star-rate": "three_star_rate", "average-stars": "average_stars",
        "average-destruction": "average_destruction",
    }[selection.sort]
    rows.sort(key=lambda row: (-float(row[sort_field]), row["key"]))
    states = Counter(str(fact["army_state"]) for fact in facts)
    army_states = {
        "fully_decoded": states.pop("decoded", 0), "partial": states.pop("partial", 0),
        "missing_code": states.pop("missing_army_share_code", 0),
        "empty_code": states.pop("empty_army_share_code", 0),
        "malformed": states.pop("malformed", 0),
        "structurally_unsupported": states.pop("structurally_unsupported", 0),
        **dict(sorted(states.items())),
    }
    return {
        "kind": "army-analytics", "total_attacks": len(facts),
        "usable_army_sample": len(usable), "army_states": army_states,
        "army_states_sum_confirmed": sum(army_states.values()) == len(facts),
        "unknown_affected_attacks": sum(bool(fact["unresolved_components"]) for fact in facts),
        "unknown_component_occurrences": sum(len(fact["unresolved_components"]) for fact in facts),
        "perspective_disagreement_count": sum(bool(fact["perspective_disagreement"]) for fact in facts),
        "missing_trophy_membership_evidence": 0,
        "collection_coverage": {"state": "complete", "completed_days": selection.end_day - selection.start_day + 1},
        "freshness": {"state": "frozen"},
        "versions": {"decoder": "army-decoder-v2", "catalog": CATALOG_VERSION, "analytics": ARMY_ANALYTICS_RULE_VERSION},
        "rows": rows,
    }


def _validate_population(value: str) -> None:
    if (match := re.fullmatch(r"top-(\d+)", value)) and int(
        match.group(1)
    ) in TOP_PRESETS:
        return
    if (match := re.fullmatch(r"streak-top-(\d+)", value)) and int(
        match.group(1)
    ) in TOP_PRESETS:
        return
    if (match := re.fullmatch(r"band-(\d+)-(\d+)", value)) and (
        int(match.group(1)),
        int(match.group(2)),
    ) in RANK_BANDS:
        return
    if match := re.fullmatch(r"trophies-(\d+)-(\d+)", value):
        minimum, maximum = map(int, match.groups())
        if minimum >= 5000 and maximum >= minimum:
            return
    raise ValueError("invalid population filter")
