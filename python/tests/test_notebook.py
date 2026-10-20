"""Season notebook rules that need no database: runs from day picks, and
base IDs as support would read them out or type them."""

from __future__ import annotations

import pytest

from clashlens.api import ApiError
from clashlens.api_notebook import (
    base_on,
    clean_link,
    new_code,
    parse_code,
    runs,
)

RING, BOX, FORT = 1, 2, 3
LEGEND = set(range(1, 29))


def test_a_base_carries_forward_until_the_next_pick() -> None:
    # Box from Day 1, Fort on Day 9, Ring from Day 10; today is Day 16.
    picks = {1: BOX, 9: FORT, 10: RING}
    assert runs(picks, 16, LEGEND) == [(1, 8, BOX), (9, 9, FORT), (10, 16, RING)]
    assert base_on(picks, 16, LEGEND) == RING


def test_days_before_the_first_pick_or_with_no_base_are_in_no_run() -> None:
    # Ring from Day 3, no base from Day 6, Ring again from Day 8.
    assert runs({3: RING, 6: None, 8: RING}, 9, LEGEND) == [(3, 5, RING), (8, 9, RING)]
    assert base_on({3: RING, 6: None}, 7, LEGEND) is None
    assert base_on({}, 7, LEGEND) is None


def test_picking_the_same_base_again_keeps_one_run() -> None:
    assert runs({1: RING, 4: RING}, 6, LEGEND) == [(1, 6, RING)]


def test_a_pick_stops_at_the_first_day_out_of_legend_league() -> None:
    # Ring from Day 3; out of Legend League for Days 8 to 14, back from 15
    # with no base until Box is picked on Day 16.
    legend = LEGEND - set(range(8, 15))
    assert runs({3: RING}, 16, legend) == [(3, 7, RING)]
    assert base_on({3: RING}, 15, legend) is None
    assert runs({3: RING, 16: BOX}, 16, legend) == [(3, 7, RING), (16, 16, BOX)]


def test_a_pick_from_tomorrow_is_in_no_run_yet() -> None:
    assert runs({1: RING, 17: BOX}, 16, LEGEND) == [(1, 16, RING)]
    assert base_on({1: RING, 17: BOX}, 17, LEGEND) == BOX
    # Ring is still the base today.
    assert base_on({1: RING, 17: BOX}, 16, LEGEND) == RING
    assert base_on({3: RING}, 2, LEGEND) is None


def test_base_ids_are_short_and_never_use_letters_that_look_like_digits() -> None:
    codes = {new_code() for _ in range(2_000)}
    assert len(codes) == 2_000
    for code in codes:
        assert code.startswith("B-") and len(code) == 8
        assert not set(code[2:]) & set("ILOU")


def test_support_can_type_a_base_id_loosely() -> None:
    assert parse_code("b-7k3qx9") == "B-7K3QX9"
    assert parse_code("7K3QX9") == "B-7K3QX9"
    # I and L read as 1, O as 0.
    assert parse_code("B-IL0OQX") == "B-1100QX"
    with pytest.raises(ApiError):
        parse_code("B-7K3QX")
    with pytest.raises(ApiError):
        parse_code("B-7K3QXU")


def test_only_the_games_own_base_links_are_kept() -> None:
    link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH17%3AHV%3AAAAAKgAAAAJ"
    assert clean_link(f"  {link} ") == link
    assert clean_link("") is None
    for bad in ("javascript:alert(1)", "https://example.com/en?action=OpenLayout",
                "https://link.clashofclans.com.evil.example/x", link + " <b>"):
        with pytest.raises(ApiError):
            clean_link(bad)
