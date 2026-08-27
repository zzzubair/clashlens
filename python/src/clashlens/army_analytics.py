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
    category = selection.category
    relationship_category = category in {
        "hero-pet", "hero-equipment", "equipment-for-hero", "cc-composition"
    }
    states: Counter[str] = Counter()
    aggregates: dict[str, dict[str, Any]] = {}
    hero_fact_counts: Counter[str] = Counter()
    hero_unknown_counts: Counter[str] = Counter()
    unknown_counts: Counter[str] = Counter()
    unknown_present_counts: Counter[str] = Counter()
    cc_unknown_count = 0
    total_facts = 0
    usable_count = 0
    unknown_affected = 0
    unknown_occurrences = 0
    disagreement_count = 0

    def individual_items(fact: dict[str, Any]) -> set[str]:
        field = {
            "troops": "home_troops", "spells": "spells", "siege": "siege",
            "cc-troops": "cc_troops",
        }.get(category)
        if field:
            return {
                str(item[0]) for item in fact[field]
                if isinstance(item, list) and item
            }
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
            if fact["army_state"] == "partial" and any(
                item.get("section") == "i"
                for item in fact["unresolved_components"]
            ):
                return result
            composition = sorted(
                (str(item[0]), int(item[1]))
                for item in fact["cc_troops"]
                if isinstance(item, list) and len(item) > 1
            )
            if composition:
                result.add(
                    "cc:" + ",".join(f"{item}x{qty}" for item, qty in composition)
                )
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

    def unknown_heroes(fact: dict[str, Any]) -> set[str]:
        if fact["army_state"] == "decoded":
            return set()
        suffix = "pet" if category == "hero-pet" else "equipment"
        return {
            str(item.get("origin"))[: -len(suffix) - 1]
            for item in fact["unresolved_components"]
            if str(item.get("origin", "")).endswith(f":{suffix}")
        }

    for fact in facts:
        total_facts += 1
        state = str(fact["army_state"])
        states[state] += 1
        unresolved = fact["unresolved_components"]
        unknown_affected += bool(unresolved)
        unknown_occurrences += len(unresolved)
        disagreement_count += bool(fact["perspective_disagreement"])
        if state not in {"decoded", "partial"}:
            continue
        usable_count += 1
        values = relationships(fact) if relationship_category else individual_items(fact)
        for key in values:
            aggregate = aggregates.setdefault(
                key,
                {"usage_count": 0, "star_counts": [0, 0, 0, 0],
                 "stars": 0, "destruction": 0},
            )
            aggregate["usage_count"] += 1
            stars = int(fact["stars"])
            aggregate["star_counts"][stars] += 1
            aggregate["stars"] += stars
            aggregate["destruction"] += int(fact["destruction_percentage"])

        if not relationship_category:
            continue
        if category == "cc-composition":
            if state == "partial" and any(
                item.get("section") == "i" for item in unresolved
            ):
                cc_unknown_count += 1
            continue
        hero_ids = {
            str(hero["hero"])
            for hero in fact["heroes"]
            if isinstance(hero, dict) and hero.get("hero")
        }
        if category == "equipment-for-hero":
            hero_fact_counts.update(hero_ids)
        scoped_unknown = unknown_heroes(fact)
        unknown_counts.update(scoped_unknown)
        hero_unknown_counts.update(scoped_unknown & hero_ids)
        unknown_present_counts.update(
            key for key in values if key.split("|", 1)[0] in scoped_unknown
        )

    keys = sorted(aggregates)
    rows = []
    for key in keys:
        aggregate = aggregates[key]
        excluded_unknown = 0
        if category == "cc-composition":
            excluded_unknown = cc_unknown_count
            denominator = usable_count - excluded_unknown
        elif relationship_category:
            hero_id = key.split("|", 1)[0]
            if category == "equipment-for-hero":
                excluded_unknown = hero_unknown_counts[hero_id] - unknown_present_counts[key]
                denominator = hero_fact_counts[hero_id] - excluded_unknown
            else:
                excluded_unknown = unknown_counts[hero_id] - unknown_present_counts[key]
                denominator = usable_count - excluded_unknown
        else:
            denominator = usable_count
        sample = aggregate["usage_count"]
        star_counts = aggregate["star_counts"]
        star_rates = [count / sample if sample else 0 for count in star_counts]
        typed_ids = key.replace("cc:", "").replace("|", ",").split(",")

        def item_label(item: str) -> str:
            typed_id = item.split("x", 1)[0]
            name = catalog_name(typed_id)
            if name is not None:
                return name
            suffix = typed_id.split(":", 1)[1] if ":" in typed_id else typed_id
            return f"Unknown ID {suffix}"

        rows.append({
            "key": key, "label": " + ".join(item_label(item) for item in typed_ids),
            "usage_count": sample, "usage_denominator": denominator,
            "usage_rate": sample / denominator if denominator else 0,
            "star_counts": star_counts, "star_rates": star_rates,
            "three_star_rate": star_rates[3],
            "average_stars": aggregate["stars"] / sample if sample else 0,
            "average_destruction": aggregate["destruction"] / sample if sample else 0,
            "unknown_excluded_attacks": excluded_unknown,
        })
    sort_field = {
        "usage-rate": "usage_rate", "usage-count": "usage_count",
        "three-star-rate": "three_star_rate", "average-stars": "average_stars",
        "average-destruction": "average_destruction",
    }[selection.sort]
    rows.sort(key=lambda row: (-float(row[sort_field]), row["key"]))
    army_states = {
        "fully_decoded": states.pop("decoded", 0), "partial": states.pop("partial", 0),
        "missing_code": states.pop("missing_army_share_code", 0),
        "empty_code": states.pop("empty_army_share_code", 0),
        "malformed": states.pop("malformed", 0),
        "structurally_unsupported": states.pop("structurally_unsupported", 0),
        **dict(sorted(states.items())),
    }
    return {
        "kind": "army-analytics", "total_attacks": total_facts,
        "usable_army_sample": usable_count, "army_states": army_states,
        "army_states_sum_confirmed": sum(army_states.values()) == total_facts,
        "unknown_affected_attacks": unknown_affected,
        "unknown_component_occurrences": unknown_occurrences,
        "perspective_disagreement_count": disagreement_count,
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
