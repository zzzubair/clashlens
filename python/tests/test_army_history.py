"""Usage counts keep quantity and trophy relationships after catalogue updates."""
import json
from dataclasses import asdict

from clashlens import catalog
from clashlens.army_decoder import DecodedArmy, decode_army_share_code
from clashlens.army_history import aggregate_usage, usage_rows


def fact(code, stars=3, destruction=100, trophies=6000):
    army = decode_army_share_code(code)
    assert isinstance(army, DecodedArmy)
    return {
        "army_state": army.status, "stars": stars,
        "destruction_percentage": destruction, "battle_time_trophies": trophies,
        "home_troops": [[f.typed_id, f.quantity, f.origin] for f in army.home_troops],
        "spells": [[f.typed_id, f.quantity, f.origin] for f in army.spells],
        "siege": [[f.typed_id, f.quantity, f.origin] for f in army.siege],
        "cc_troops": [[f.typed_id, f.quantity, f.origin] for f in army.cc_troops],
        "heroes": [{"hero": h.hero_typed_id, "pet": h.pet_typed_id,
                    "equipment": list(h.equipment_typed_ids)} for h in army.heroes],
        "unresolved_components": [asdict(u) for u in army.unknown],
        "perspective_disagreement": False,
    }


def test_whole_season_rate_keeps_quantity_and_trophy_bucket_evidence():
    stored = aggregate_usage([
        fact("u2x58-3x58", trophies=6000), fact("u5x58", trophies=6000),
        fact("u1x58", trophies=6000), fact("u1x0", trophies=6000),
        fact("u5x58", trophies=6100), fact("u1x58", trophies=None),
    ])
    rows, unresolved = usage_rows(stored["troops"], "troops", 6)
    assert not unresolved
    by_id = {row["unit_id"]: row for row in rows}
    assert (by_id["troop:58"]["usage_count"],
            by_id["troop:58"]["usage_denominator"],
            by_id["troop:58"]["usage_rate"]) == (5, 6, 5 / 6)
    assert by_id["troop:58"]["quantity_trophy_groups"] == [
        {"quantity": 1, "battle_trophy_min": None, "battle_trophy_max": None,
         "usage_count": 1},
        {"quantity": 1, "battle_trophy_min": 6000, "battle_trophy_max": 6099,
         "usage_count": 1},
        {"quantity": 5, "battle_trophy_min": 6000, "battle_trophy_max": 6099,
         "usage_count": 2},
        {"quantity": 5, "battle_trophy_min": 6100, "battle_trophy_max": 6199,
         "usage_count": 1},
    ]


def test_five_uses_across_trophy_buckets_are_five_of_ten():
    facts = [fact("u1x58" if index < 5 else "u1x0", trophies=5000 + index * 100)
             for index in range(10)]
    rows, unresolved = usage_rows(aggregate_usage(facts)["troops"], "troops", 10)
    assert not unresolved
    row = next(row for row in rows if row["unit_id"] == "troop:58")
    assert (row["usage_count"], row["usage_denominator"], row["usage_rate"]) == (
        5, 10, .5,
    )
    assert len(row["quantity_trophy_groups"]) == 5


def test_thousands_of_battles_fit_the_retained_category_limit():
    facts = [
        {
            "army_state": "decoded",
            "battle_time_trophies": 5000 + index,
            "home_troops": [
                [f"troop:{unit_id}", (index + unit_id) % 5 + 1, "home"]
                for unit_id in range(100, 110)
            ],
            "spells": [], "siege": [], "heroes": [],
            "unresolved_components": [],
        }
        for index in range(3000)
    ]
    retained = aggregate_usage(facts)["troops"]
    assert len(json.dumps(retained).encode()) < 524_288
    assert sum(row[1] for row in retained) == 30_000


def test_unknown_namespaces_and_siege_are_resolved_without_armies(monkeypatch):
    stored = aggregate_usage([fact("h900p900e14_900u5x900-1x901s2x900i7x900-1x901d3x900")])
    assert usage_rows(stored["troops"], "troops", 1) == ([], True)
    assert usage_rows(stored["troops"], "siege", 1) == ([], True)
    for namespace in ("troop", "spell", "hero", "pet", "equipment"):
        monkeypatch.setitem(catalog._CATALOG_ENTRIES, f"{namespace}:900",
                            {"name": f"Named {namespace}", "category": namespace, "is_siege": False})
    monkeypatch.setitem(catalog._CATALOG_ENTRIES, "troop:901",
                        {"name": "Named siege", "category": "troop", "is_siege": True})
    for category, typed_id, quantity in (
        ("troops", "troop:900", 5), ("siege", "troop:901", 1),
        ("spells", "spell:900", 2), ("heroes", "hero:900", 1),
        ("pets", "pet:900", 1), ("equipment", "equipment:900", 1),
    ):
        rows, unresolved = usage_rows(
            stored["troops" if category == "siege" else category], category, 1
        )
        assert not unresolved
        row = next(r for r in rows if r["unit_id"] == typed_id)
        assert (row["usage_count"], row["usage_denominator"]) == (1, 1)
        assert row["quantity_trophy_groups"] == [{
            "quantity": quantity,
            "battle_trophy_min": 6000,
            "battle_trophy_max": 6099,
            "usage_count": 1,
        }]
        assert row["label"].startswith("Named")


def test_clan_castle_changes_cannot_change_retained_usage():
    home = "h0p9e14_32u5x58-1x97s2x2"
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i2x58-1x97d1x2")])
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i5x900-1x901d2x900")])
