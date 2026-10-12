from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime

from clashlens import projected_place
from clashlens.projected_place import (
    ACTIVITIES,
    LEVELS,
    ProjectedPlaces,
    activity,
    situations,
)

MORNING = datetime(2026, 10, 12, 9, tzinfo=UTC)
NOON = datetime(2026, 10, 12, 12, tzinfo=UTC)
# The characters player tags use.
TAG = "0289PYLQGRJCUV"


def test_activity_from_the_three_days_before() -> None:
    assert [activity(8, 0), activity(9, 0), activity(3, 0), activity(0, 5),
            activity(0, 0)] == [3, 3, 2, 1, 0]


def test_level_is_rank_band_then_trophy_bracket() -> None:
    # 1,200 players: the top 200 and 201-1,000 by place, then by trophies.
    trophies = [6000] * 200 + [5500] * 800 + [5450, 5150, 5050, 4950, 4850, 4700] * 33
    keys = situations(trophies, [8] * len(trophies), [8] * len(trophies),
                      [0] * len(trophies))
    assert {key % LEVELS for key in keys[:200]} == {0}
    assert {key % LEVELS for key in keys[200:1000]} == {1}
    assert [key % LEVELS for key in keys[1000:1006]] == [8, 6, 5, 4, 3, 2]
    # Open slots and activity sit above the level.
    key = situations([5000], [2], [5], [3])[0]
    assert key // (LEVELS * ACTIVITIES) == 2 * 9 + 5
    assert key // LEVELS % ACTIVITIES == 3


def _day(trophies, open_attacks, open_defenses, changes):
    keys = situations(trophies, open_attacks, open_defenses, [3] * len(trophies))
    pairs = sorted(zip(keys, changes, strict=True))
    return [key for key, _ in pairs], [change for _, change in pairs]


def _places(trophies, open_attacks, open_defenses, days, latest=None, seed=1):
    count = len(trophies)
    return ProjectedPlaces(
        trophies, open_attacks, open_defenses, [3] * count,
        latest or [None] * count,
        [f"#PP{TAG[index // len(TAG)]}{TAG[index % len(TAG)]}" for index in range(count)],
        days, seed=seed,
    )


def test_a_finished_player_is_passed_by_a_rival_who_usually_climbs() -> None:
    # Yesterday at this time 50 players with 2 attacks left won 80 more and
    # 50 who had finished stayed put.
    day = _day([5000] * 100, [2] * 50 + [0] * 50, [0] * 100, [80] * 50 + [0] * 50)
    places = _places([5460, 5410], [0, 2], [0, 0], [day])
    assert places.projected(0, 1, 2) == 2
    assert places.projected(1, 1, 2) == 1
    # Never outside the outer range.
    assert places.projected(0, 1, 1) == 1


def test_unseen_situations_stay_where_they_are() -> None:
    places = _places([5100, 5000], [8, 8], [8, 8], [([], [])])
    assert places.projected(0, 1, 2) == 1
    assert places.projected(1, 1, 2) == 2


def test_equal_trophies_follow_the_daily_boards_order() -> None:
    # Three finished players on 5,000: the latest trophy change first, no
    # change that day last. A fourth, 40 below with an attack left that
    # always wins 40, moves last of all, so ends first among the four.
    day = _day([5000] * 100, [1] * 50 + [0] * 50, [0] * 100, [40] * 50 + [0] * 50)
    places = _places(
        [5000, 5000, 5000, 4960], [0, 0, 0, 1], [0, 0, 0, 0], [day],
        latest=[MORNING, None, NOON, MORNING],
    )
    assert [places.projected(player, 1, 4) for player in range(4)] == [3, 4, 2, 1]


def test_a_situation_without_examples_copies_the_nearest_within_its_own_slots() -> None:
    # Just after the Reset a player has made one attack, so 7 attacks and
    # 8 defenses are open, but the examples at this time all still had 8
    # of each, and won 320. Copying them, kept to the 280 that 7 attacks
    # can win, takes them past 5,100 but not 5,290.
    day = _day([5000] * 100, [8] * 100, [8] * 100, [320] * 100)
    places = _places([5290, 5100, 5000], [0, 0, 7], [0, 0, 8], [day])
    assert places.projected(2, 1, 3) == 2


def test_each_imagined_day_copies_one_whole_day() -> None:
    # One day everyone with a defense left lost 40; the other, nobody did.
    # A finished player 20 below them either stays last or ends first:
    # never in between, as it would if each rival's luck were drawn on its
    # own.
    rivals = 100
    busy = _day([5000] * rivals, [0] * rivals, [1] * rivals, [-40] * rivals)
    quiet = _day([5000] * rivals, [0] * rivals, [1] * rivals, [0] * rivals)
    places = _places([5000] * rivals + [4980], [0] * (rivals + 1),
                     [1] * rivals + [0], [busy, quiet], seed=3)
    finished = places.position[rivals]
    ranks = {
        1 + places.size - bisect_right(ordered, keys[finished])
        for keys, ordered in zip(places.keys, places.sorted, strict=True)
    }
    assert ranks == {1, rivals + 1}
    assert len(places.keys) == projected_place.DRAWS
