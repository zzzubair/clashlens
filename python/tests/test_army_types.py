from __future__ import annotations

from itertools import permutations

import pytest

from clashlens.army_analytics import ArmyAnalyticsSelection
from clashlens.army_types import (
    DRAGON_RIDER,
    FIREBALL_SUPER_MINION,
    HYDRA,
    LAVALOON,
    OTHER,
    ROOT_RIDER,
    SUPER_ARCHER_RUIN_WITCH,
    SUPER_BOWLER,
    SUPER_WITCH,
    THROWER,
    THROWER_RUIN_WITCH,
    TYPE_NAMES,
    UNREADABLE,
    classify,
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
