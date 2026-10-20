from __future__ import annotations

from itertools import permutations

import pytest

from clashlens.army_analytics import ArmyAnalyticsSelection
from clashlens.army_types import (
    DRAGON_RIDER,
    DRAGONS,
    FIREBALL_SUPER_MINION,
    HYDRA,
    LAVALOON,
    OTHER,
    OTHER_VARIANT,
    ROOT_RIDER,
    SUPER_ARCHER_RUIN_WITCH,
    SUPER_BOWLER,
    SUPER_WITCH,
    THROWER,
    THROWER_RUIN_WITCH,
    TYPE_NAMES,
    UNREADABLE,
    VARIANTS,
    classify,
    type_result_rows,
    variant,
)


def army(**troops: int) -> list[list]:
    return [[f"troop:{number.removeprefix('t')}", count] for number, count in troops.items()]


@pytest.mark.parametrize(
    ("troops", "expected"),
    [
        # 9 Throwers (144) and 5 Healers (70): Thrower clearly leads.
        (army(t132=9, t7=5), THROWER),
        # 8 Dragons and 6 Dragon Riders make Hydra, though neither leads clearly.
        (army(t8=8, t65=6, t1=2), HYDRA),
        # A published Fireball Super Minion army: Healers lead it by camp space.
        (army(t84=5, t57=8, t7=5, t1=4, t4=6), FIREBALL_SUPER_MINION),
        # Sir Moose's Super Archer army: 10 Super Archers, 4 Ruin Witches.
        (army(t27=10, t7=6, t109=4, t53=2, t28=1), SUPER_ARCHER_RUIN_WITCH),
        # Ice Hound counts like Lava Hound for LavaLoon.
        (army(t5=27, t76=3, t10=5), LAVALOON),
        (army(t65=8, t63=3, t57=7, t1=4), DRAGON_RIDER),
        # Healers and Ice Golems close behind do not stop a main troop leading.
        (army(t80=5, t7=5, t58=2, t28=2, t82=2, t1=2), SUPER_BOWLER),
        (army(t110=4, t7=5, t57=6, t58=3, t4=5, t1=4), ROOT_RIDER),
        (army(t66=5, t109=2, t7=5), SUPER_WITCH),
        # Throwers and Ruin Witches nearly level, Healers aside.
        (army(t132=7, t109=4, t7=8), THROWER_RUIN_WITCH),
        # Throwers clearly ahead of 3 Ruin Witches stay Thrower.
        (army(t132=10, t109=3, t7=5), THROWER),
    ],
)
def test_names_the_main_army_types(troops, expected) -> None:
    assert classify(troops) == expected


@pytest.mark.parametrize(
    "troops",
    [
        # Only support troops.
        army(t7=10, t1=20),
        # Throwers lead the main troops but hold under 35% of their space.
        army(t132=4, t6=10, t12=5, t3=8, t7=5),
        # Throwers and Ruin Witches nearly level, but only 2 Ruin Witches.
        army(t132=3, t109=2),
        # Throwers and Ruin Witches lead together under 50% of the main space.
        army(t132=4, t109=3, t3=12, t12=7, t6=12),
        # Throwers and Super Bowlers hold equal space.
        army(t132=15, t80=8),
        # Fewer than 3 of the leading troop.
        army(t81=2, t1=10),
        # LavaLoon and Fireball Super Minion both match.
        army(t5=12, t17=1, t84=4, t57=8),
        # A leader with no type of its own: Super Dragon.
        army(t81=4, t1=10),
    ],
)
def test_anything_no_type_fits_is_other(troops) -> None:
    assert classify(troops) == OTHER


def test_an_unknown_or_empty_army_is_unreadable() -> None:
    assert classify(army(t132=9, t9999=1)) == UNREADABLE
    assert classify([]) == UNREADABLE


def test_the_same_troops_get_the_same_type_however_they_are_listed() -> None:
    assert classify([["troop:132", 4], ["troop:7", 5], ["troop:132", 5]]) == THROWER
    assert classify(list(reversed(army(t132=9, t7=5)))) == THROWER
    # Ruin Witches and Valkyries hold equal space behind the Throwers.
    tied = army(t132=7, t109=4, t12=13)
    assert {classify(order) for order in permutations(tied)} == {THROWER_RUIN_WITCH}


def test_every_type_has_a_name() -> None:
    assert {OTHER, UNREADABLE, THROWER, HYDRA} <= set(TYPE_NAMES)
    assert TYPE_NAMES[OTHER] == "Other"


def test_army_types_count_attacks_only() -> None:
    values = {
        "season": "current", "start_day": 1, "end_day": 1, "population": "top-100",
        "category": "army-types", "sort": "usage-rate",
    }
    assert ArmyAnalyticsSelection.parse(lens="offense", **values).category == "army-types"
    with pytest.raises(ValueError):
        ArmyAnalyticsSelection.parse(lens="defense", **values)


def heroes(**equipment: tuple[int, ...]) -> list[dict]:
    hero_ids = {"queen": 1, "warden": 2, "prince": 6, "duke": 7}
    return [{"hero": f"hero:{hero_ids[name]}", "pet": None,
             "equipment": [f"equipment:{item}" for item in items]}
            for name, items in equipment.items()]


def name(type_id: int, variant_id: int) -> str:
    return {v: n for v, n, _items in VARIANTS[type_id]}.get(variant_id, "Other")


@pytest.mark.parametrize(
    ("type_id", "equipment", "expected"),
    [
        # Revenge Deck + Fire Heart on the Duke.
        (HYDRA, heroes(duke=(60, 52), warden=(22, 19)), "Revenge Hydra"),
        (HYDRA, heroes(duke=(60, 53)), "Revenge Backpack Hydra"),
        (HYDRA, heroes(duke=(53, 52)), "Backpack Hydra"),
        # The Queen's Monolith Arrow names a Super Bowler before the Duke does.
        (SUPER_BOWLER, heroes(queen=(16, 15), duke=(56, 59)), "Monolith Super Bowler"),
        (SUPER_BOWLER, heroes(queen=(17, 15), duke=(56, 59)), "Electro Fangs Super Bowler"),
        # The Prince's Portal Pendant names a LaLo before the Warden's Fireball.
        (LAVALOON, heroes(prince=(61, 49), warden=(22, 4)), "Portal Pendant LaLo"),
        (LAVALOON, heroes(warden=(22, 4)), "Fireball LaLo"),
        # Fireball first on a Thrower, whatever the Duke carries.
        (THROWER, heroes(warden=(22, 19), duke=(60, 53)), "Fireball Thrower"),
        # No listed item, or the item on the wrong hero: the type's Other.
        (HYDRA, heroes(duke=(52, 59)), "Other"),
        (HYDRA, heroes(warden=(60, 53)), "Other"),
        (HYDRA, [], "Other"),
    ],
)
def test_hero_equipment_names_the_variant(type_id, equipment, expected) -> None:
    assert name(type_id, variant(type_id, equipment)) == expected


def test_types_without_variants_and_every_variant_id_is_used_once() -> None:
    assert variant(DRAGONS, heroes(duke=(60, 53))) == 0
    assert variant(OTHER, heroes(duke=(60, 53))) == 0
    ids = [v for listed in VARIANTS.values() for v, _name, _items in listed]
    assert len(ids) == len(set(ids)) == 41 and 0 not in ids


def test_usually_with_counts_an_exact_15_point_lead() -> None:
    # Ice Block on 6 of a variant's 10 attacks (60%), and on 45 of the type's
    # 100 (45%): exactly 15 points more, so it is a tag.
    rows = [(HYDRA, 6, 10, 0, 0, 0, 0, 0), (HYDRA, OTHER_VARIANT, 90, 0, 0, 0, 0, 0)]
    spells = {(HYDRA, 6): {"spell:109": 6}, (HYDRA, OTHER_VARIANT): {"spell:109": 39}}
    [hydra] = type_result_rows(rows, 100, spells)
    assert [(row["label"], row["usually_with"]) for row in hydra["variants"]] == [
        ("Revenge Hydra", ["Ice Block"]), ("Other Hydra", []),
    ]


def test_usually_with_tags_tied_on_attacks_come_in_name_order() -> None:
    # Rage and Freeze on all 3 of a variant's attacks, Rage read first.
    rows = [(HYDRA, 6, 3, 0, 0, 0, 0, 0), (HYDRA, OTHER_VARIANT, 7, 0, 0, 0, 0, 0)]
    spells = {(HYDRA, 6): {"spell:2": 3, "spell:5": 3}}
    [hydra] = type_result_rows(rows, 10, spells)
    assert hydra["variants"][0]["usually_with"] == ["Freeze", "Rage"]
