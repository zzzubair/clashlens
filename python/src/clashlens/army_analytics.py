from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .catalog import CATALOG_VERSION, unit_label

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
TOP_PRESETS = frozenset({5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000})
# Consistent top reads every member's facts, so it stays within the top 1,000.
STREAK_PRESETS = frozenset(count for count in TOP_PRESETS if count <= 1000)
RANK_BANDS = frozenset(
    {
        (1, 5),
        (6, 10),
        (11, 20),
        (21, 50),
        (51, 100),
        (101, 200),
        *((start, start + 99) for start in range(201, 1000, 100)),
        (1001, 2000),
        (2001, 5000),
        (5001, 10000),
    }
)
# A trophy range is any whole-number range inside these limits.
TROPHY_RANGE_LIMITS = (0, 99999)


def rank_band_firsts(low: int, high: int) -> list[int] | None:
    """First positions of the rank bands that make up ranks low-high.

    None unless low-high starts and ends on rank band edges. The bands split
    ranks 1-10,000 without gaps, so those between the edges cover the rest.
    """
    covered = [
        (first, last) for first, last in sorted(RANK_BANDS) if low <= first and last <= high
    ]
    if not covered or covered[0][0] != low or covered[-1][1] != high:
        return None
    return [first for first, _last in covered]


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


_RELATIONSHIP_CATEGORIES = frozenset(
    {"hero-pet", "hero-equipment", "equipment-for-hero", "cc-composition"}
)
_COUNTS = (
    "facts", "usable", "unknown_affected", "unknown_occurrences", "disagreements",
)
_TALLIES = ("states", "hero_facts")
# Where an old partial army kept each unnamed id, by army code section.
_UNNAMED_SECTIONS = {
    "u": ("home_troops", "troop"), "i": ("cc_troops", "troop"),
    "s": ("spells", "spell"), "d": ("spells", "spell"),
}


def with_unnamed_ids(fact: dict[str, Any]) -> dict[str, Any]:
    """The army with every saved id in place, named or not.

    Armies saved before saving stopped depending on the unit list kept ids
    it did not name apart, in ``unresolved_components``, and were marked
    partial. Reading puts those ids back beside the others, so they count
    like any other unit and no saved army needs saving again.
    """
    unnamed = [
        item for item in fact.get("unresolved_components") or []
        if isinstance(item, dict) and item.get("numeric_id") is not None
    ]
    if not unnamed and fact.get("army_state", fact.get("status")) != "partial":
        return fact
    fact = {**fact, "unresolved_components": []}
    for key in ("army_state", "status"):
        if fact.get(key) == "partial":
            fact[key] = "decoded"
    heroes = {
        str(hero["hero"]): {**hero, "equipment": list(hero.get("equipment") or [])}
        for hero in fact.get("heroes") or []
        if isinstance(hero, dict) and hero.get("hero")
    }
    for item in unnamed:
        number, origin = item["numeric_id"], str(item.get("origin", ""))
        if item.get("section") in _UNNAMED_SECTIONS:
            field, kind = _UNNAMED_SECTIONS[item["section"]]
            entry = [f"{kind}:{number}", int(item.get("quantity", 1)), origin]
            fact[field] = [*(fact.get(field) or []), entry]
            continue
        # Hero section origins read "hero:<id>:pet" or "hero:<id>:equipment";
        # an unnamed hero itself ("hero") already has its own entry.
        if not origin.startswith("hero:"):
            continue
        hero_id, _, part = origin.removeprefix("hero:").partition(":")
        hero = heroes.setdefault(
            f"hero:{hero_id}", {"hero": f"hero:{hero_id}", "pet": None, "equipment": []}
        )
        if part == "pet":
            hero["pet"] = f"pet:{number}"
        elif part == "equipment":
            hero["equipment"] = sorted([*hero["equipment"], f"equipment:{number}"])
    fact["heroes"] = list(heroes.values())
    return fact


def public_army_states(states: dict[str, int]) -> dict[str, int]:
    """Army read results by state; an old partial army counts as read."""
    states = Counter(states)
    return {
        "fully_decoded": states.pop("decoded", 0) + states.pop("partial", 0),
        "missing_code": states.pop("missing_army_share_code", 0),
        "empty_code": states.pop("empty_army_share_code", 0),
        "malformed": states.pop("malformed", 0),
        "structurally_unsupported": states.pop("structurally_unsupported", 0),
        **dict(sorted(states.items())),
    }


def new_army_totals() -> dict[str, Any]:
    """One category's counts, which add up across facts, days and rank bands.

    Plain JSON so they can be saved; ``rows`` maps a key to [uses, zero-star,
    one-star, two-star, three-star, stars, destruction].
    """
    return {
        **dict.fromkeys(_COUNTS, 0),
        **{name: {} for name in _TALLIES},
        "rows": {},
    }


def _tally(counts: dict[str, int], keys: Iterable[str]) -> None:
    for key in keys:
        counts[key] = counts.get(key, 0) + 1


def _individual_items(fact: dict[str, Any], category: str) -> set[str]:
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


def _relationships(fact: dict[str, Any], category: str) -> set[str]:
    result: set[str] = set()
    if category == "cc-composition":
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


def add_army_fact(
    totals: dict[str, Any], fact: dict[str, Any], category: str
) -> None:
    relationship_category = category in _RELATIONSHIP_CATEGORIES
    totals["facts"] += 1
    state = str(fact["army_state"])
    _tally(totals["states"], (state,))
    unresolved = fact["unresolved_components"]
    totals["unknown_affected"] += bool(unresolved)
    totals["unknown_occurrences"] += len(unresolved)
    totals["disagreements"] += bool(fact["perspective_disagreement"])
    if state not in {"decoded", "partial"}:
        return
    fact = with_unnamed_ids(fact)
    totals["usable"] += 1
    values = (
        _relationships(fact, category)
        if relationship_category
        else _individual_items(fact, category)
    )
    stars = int(fact["stars"])
    for key in values:
        row = totals["rows"].setdefault(key, [0, 0, 0, 0, 0, 0, 0])
        row[0] += 1
        row[1 + stars] += 1
        row[5] += stars
        row[6] += int(fact["destruction_percentage"])
    if category == "equipment-for-hero":
        _tally(
            totals["hero_facts"],
            {
                str(hero["hero"])
                for hero in fact["heroes"]
                if isinstance(hero, dict) and hero.get("hero")
            },
        )


def merge_army_totals(totals: dict[str, Any], other: dict[str, Any]) -> None:
    for name in _COUNTS:
        totals[name] += other[name]
    for name in _TALLIES:
        counts = totals[name]
        for key, value in other[name].items():
            counts[key] = counts.get(key, 0) + value
    for key, values in other["rows"].items():
        row = totals["rows"].setdefault(key, [0, 0, 0, 0, 0, 0, 0])
        for index, value in enumerate(values):
            row[index] += value


def finish_army_result(
    totals: dict[str, Any], selection: ArmyAnalyticsSelection
) -> dict[str, Any]:
    category = selection.category
    usable_count = totals["usable"]
    rows = []
    for key in sorted(totals["rows"]):
        sample, *star_counts, stars, destruction = totals["rows"][key]
        denominator = (
            totals["hero_facts"].get(key.split("|", 1)[0], 0)
            if category == "equipment-for-hero"
            else usable_count
        )
        star_rates = [count / sample if sample else 0 for count in star_counts]
        rows.append({
            "key": key, "label": row_label(key),
            "usage_count": sample, "usage_denominator": denominator,
            "usage_rate": sample / denominator if denominator else 0,
            "star_counts": star_counts, "star_rates": star_rates,
            "three_star_rate": star_rates[3],
            "average_stars": stars / sample if sample else 0,
            "average_destruction": destruction / sample if sample else 0,
            # Unnamed ids count like any other unit, so nothing is excluded.
            "unknown_excluded_attacks": 0,
        })
    sort_field = {
        "usage-rate": "usage_rate", "usage-count": "usage_count",
        "three-star-rate": "three_star_rate", "average-stars": "average_stars",
        "average-destruction": "average_destruction",
    }[selection.sort]
    rows.sort(key=lambda row: (-float(row[sort_field]), row["key"]))
    army_states = public_army_states(totals["states"])
    return {
        "kind": "army-analytics", "total_attacks": totals["facts"],
        "usable_army_sample": usable_count, "army_states": army_states,
        "army_states_sum_confirmed": sum(army_states.values()) == totals["facts"],
        "unknown_affected_attacks": totals["unknown_affected"],
        "unknown_component_occurrences": totals["unknown_occurrences"],
        "perspective_disagreement_count": totals["disagreements"],
        "missing_trophy_membership_evidence": 0,
        "collection_coverage": {"state": "complete", "completed_days": selection.end_day - selection.start_day + 1},
        "freshness": {"state": "frozen"},
        "versions": {"decoder": "army-decoder-v2", "catalog": CATALOG_VERSION, "analytics": ARMY_ANALYTICS_RULE_VERSION},
        "rows": rows,
    }


def row_label(key: str) -> str:
    """Names for a row key such as "hero:6|equipment:61" or "cc:troop:0x5"."""
    typed_ids = key.replace("cc:", "").replace("|", ",").split(",")
    return " + ".join(unit_label(item.split("x", 1)[0]) for item in typed_ids)


def build_army_result(
    facts: Iterable[dict[str, Any]], selection: ArmyAnalyticsSelection
) -> dict[str, Any]:
    totals = new_army_totals()
    for fact in facts:
        add_army_fact(totals, fact, selection.category)
    return finish_army_result(totals, selection)


def _validate_population(value: str) -> None:
    if (match := re.fullmatch(r"top-(\d+)", value)) and int(
        match.group(1)
    ) in TOP_PRESETS:
        return
    if (match := re.fullmatch(r"streak-top-(\d+)", value)) and int(
        match.group(1)
    ) in STREAK_PRESETS:
        return
    if (match := re.fullmatch(r"band-(\d{1,5})-(\d{1,5})", value)) and (
        rank_band_firsts(int(match.group(1)), int(match.group(2))) is not None
    ):
        return
    if match := re.fullmatch(r"trophies-(\d{1,5})-(\d{1,5})", value):
        minimum, maximum = map(int, match.groups())
        lowest, highest = TROPHY_RANGE_LIMITS
        if lowest <= minimum <= maximum <= highest:
            return
    raise ValueError("invalid population filter")
