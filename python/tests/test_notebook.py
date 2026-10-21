"""Season notebook rules that need no database: runs from day picks, base
IDs, and the names, notes and links a Clasher may save."""

from __future__ import annotations

import pytest

from clashlens.api import ApiError
from clashlens.api_notebook import (
    base_on,
    clean_link,
    clean_name,
    clean_note,
    new_code,
    parse_code,
    runs,
)
from clashlens.notebook_ranks import top_bases

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


def test_only_an_exact_base_id_is_read() -> None:
    assert parse_code("B-7K3QX9") == "B-7K3QX9"
    for bad in ("b-7k3qx9", "7K3QX9", "B-IL0OQX", " B-7K3QX9", "B-7K3QX", "B-7K3QXU"):
        with pytest.raises(ApiError):
            parse_code(bad)


def test_emoji_are_kept_but_invisible_characters_are_not() -> None:
    # 🧑‍🚀 and ❤️‍🔥 are each joined by U+200D; 🫟 and 🫩 are newer than
    # the Unicode 15 table Python 3.12 has.
    for name, note in (
        ("\U0001f9d1\u200d\U0001f680 Rocket", "CC: \u2764\ufe0f\u200d\U0001f525 lure first"),
        ("\U0001fadf Splash", "CC \U0001fae9 lure first"),
    ):
        assert (clean_name(name), clean_note(note)) == (name, note)
    # A zero-width space, a right-to-left override, a bell, a surrogate and a
    # private-use character stay refused, for that reason and not length.
    for hidden in ("\u200b", "\u202e", "\x07", "\ud800", "\ue000"):
        for clean in (clean_name, clean_note):
            with pytest.raises(ApiError) as refused:
                clean(f"Ring{hidden}")
            assert refused.value.code == "invalid_characters"
    with pytest.raises(ApiError) as refused:
        clean_note("x" * 1001)
    assert refused.value.code == "invalid_note"


def test_only_the_games_own_base_links_are_kept() -> None:
    link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH17%3AHV%3AAAAAKgAAAAJ"
    assert clean_link(f"  {link} ") == link
    assert clean_link("") is None
    for bad in ("javascript:alert(1)", "https://example.com/en?action=OpenLayout",
                "https://link.clashofclans.com.evil.example/x", link + " <b>"):
        with pytest.raises(ApiError):
            clean_link(bad)


def test_the_top_3_is_fewest_trophies_lost_a_defense_among_bases_with_16() -> None:
    def record(defenses: int, lost: int) -> dict:
        return {"defenses": defenses, "trophies_lost": lost}

    bases = {
        "B-000001": ("Ring", record(20, 400)),  # 20 a defense
        "B-000002": ("Box", record(15, 0)),  # too few defenses
        "B-000003": ("Fort", record(16, 160)),  # 10
        "B-000004": ("Spiral", record(32, 320)),  # 10, more defenses
        "B-000005": ("Hex", record(16, 400)),  # 25: fourth
        "B-000006": ("arrow", record(16, 160)),  # 10, ties Fort by name
    }
    assert top_bases(bases) == ["B-000004", "B-000006", "B-000003"]
    assert top_bases({}) == []
