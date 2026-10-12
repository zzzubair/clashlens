from __future__ import annotations

from bisect import bisect_right

from clashlens import likely_range
from clashlens.likely_range import (
    ACTIVITIES,
    LEVELS,
    LikelyPlaces,
    activity,
    situations,
)


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


def _examples(trophies, open_attacks, open_defenses, changes):
    keys = situations(trophies, open_attacks, open_defenses, [3] * len(trophies))
    return sorted(zip(keys, changes, strict=True))


def test_a_finished_player_is_passed_by_a_rival_who_usually_climbs() -> None:
    # Yesterday at this time 50 players with 2 attacks left won 80 more and
    # 50 who had finished stayed put.
    examples = _examples([5000] * 100, [2] * 50 + [0] * 50, [0] * 100,
                         [80] * 50 + [0] * 50)
    day = ([key for key, _ in examples], [change for _, change in examples])
    places = LikelyPlaces([5460, 5410], [0, 2], [0, 0], [3, 3], [day], seed=1)
    assert places.range(0, 1, 2) == {"best": 2, "worst": 2}
    assert places.range(1, 1, 2) == {"best": 1, "worst": 1}
    # Never outside the outer range.
    assert places.range(0, 1, 1) == {"best": 1, "worst": 1}


def test_unseen_situations_stay_where_they_are() -> None:
    places = LikelyPlaces([5100, 5000], [8, 8], [8, 8], [0, 0], [([], [])], seed=1)
    assert places.range(0, 1, 2) == {"best": 1, "worst": 1}
    assert places.range(1, 1, 2) == {"best": 2, "worst": 2}


def test_each_imagined_day_copies_one_whole_day() -> None:
    # One day everyone with a defense left lost 40; the other, nobody did.
    # A finished player 20 above them either stays or ends first: never in
    # between, as it would if each rival's luck were drawn on its own.
    rivals = 100
    busy = _examples([5000] * rivals, [0] * rivals, [1] * rivals, [-40] * rivals)
    quiet = _examples([5000] * rivals, [0] * rivals, [1] * rivals, [0] * rivals)
    days = [([k for k, _ in d], [c for _, c in d]) for d in (busy, quiet)]
    trophies = [5000] * rivals + [4980]
    places = LikelyPlaces(trophies, [0] * (rivals + 1), [1] * rivals + [0],
                          [3] * (rivals + 1), days, seed=3)
    finished = rivals
    ranks = {
        2 + rivals - bisect_right(ordered, finals[places.position[finished]])
        for finals, ordered in zip(places.finals, places.sorted, strict=True)
    }
    assert ranks == {1, rivals + 1}
    assert len(places.finals) == likely_range.DRAWS
