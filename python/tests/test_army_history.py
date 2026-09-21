"""Finalized usage keeps unit quantities and star outcomes by ID."""
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


def test_five_uses_are_five_of_ten_with_quantity_and_star_counts():
    stars = [1, 2, 3, 3, 0]
    facts = [
        fact(
            "u5x58" if index < 5 else "u1x0",
            stars=stars[index] if index < 5 else 0,
            trophies=5000 + index * 100,
        )
        for index in range(10)
    ]
    rows, unresolved = usage_rows(aggregate_usage(facts)["troops"], "troops", 10)
    assert not unresolved
    row = next(row for row in rows if row["unit_id"] == "troop:58")
    assert row == {
        "key": "troop:58@5", "unit_id": "troop:58", "label": "Ice Golem",
        "quantity": 5, "usage_count": 5, "usage_denominator": 10,
        "usage_rate": .5, "one_star_count": 1, "two_star_count": 1,
        "three_star_count": 2,
    }
    without_trophies = [{**item, "battle_time_trophies": None} for item in facts]
    assert aggregate_usage(without_trophies) == aggregate_usage(facts)


def test_unknown_namespaces_and_siege_are_resolved_without_armies(monkeypatch):
    stored = aggregate_usage([fact("h900p900e14_900u5x900-1x901s2x900i7x900-1x901d3x900")])
    before = {}
    for category, namespace in (
        ("troops", "troop"), ("siege", "troop"), ("spells", "spell"),
        ("heroes", "hero"), ("pets", "pet"), ("equipment", "equipment"),
    ):
        rows, unresolved = usage_rows(
            stored["troops" if category == "siege" else category], category, 1
        )
        assert unresolved
        before[category] = {row["unit_id"]: row for row in rows}
        kind = "troop or siege" if namespace == "troop" else namespace
        assert before[category][f"{namespace}:900"]["label"] == f"Unknown {kind} (ID 900)"
        if namespace == "troop":
            assert before[category]["troop:901"]["label"] == "Unknown troop or siege (ID 901)"
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
        assert (row["quantity"], row["usage_count"], row["usage_denominator"]) == (
            quantity, 1, 1,
        )
        assert (row["one_star_count"], row["two_star_count"],
                row["three_star_count"]) == (0, 0, 1)
        assert row["label"].startswith("Named")
        assert {key: value for key, value in row.items() if key != "label"} == {
            key: value for key, value in before[category][typed_id].items() if key != "label"
        }
        if category in {"troops", "siege"}:
            assert [item["unit_id"] for item in rows] == [typed_id]


def test_clan_castle_changes_cannot_change_retained_usage():
    home = "h0p9e14_32u5x58-1x97s2x2"
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i2x58-1x97d1x2")])
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i5x900-1x901d2x900")])
