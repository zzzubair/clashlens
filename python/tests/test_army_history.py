"""Usage counts keep quantity and trophy relationships after catalogue updates."""
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


def test_quantity_and_trophy_groups_have_their_own_battle_denominators():
    stored = aggregate_usage([
        fact("u2x58-3x58", trophies=6000), fact("u5x58", trophies=6000),
        fact("u1x58", trophies=6000), fact("u1x0", trophies=6000),
        fact("u5x58", trophies=6100), fact("u1x58", trophies=None),
    ])
    rows, unresolved = usage_rows(stored["troops"], "troops")
    assert not unresolved
    assert {(r["unit_id"], r["quantity"], r["battle_trophies"]):
            (r["usage_count"], r["usage_denominator"], r["usage_rate"])
            for r in rows} == {
        ("troop:58", 5, 6000): (2, 4, .5),
        ("troop:58", 1, 6000): (1, 4, .25),
        ("troop:0", 1, 6000): (1, 4, .25),
        ("troop:58", 5, 6100): (1, 1, 1),
        ("troop:58", 1, None): (1, 1, 1),
    }


def test_unknown_namespaces_and_siege_are_resolved_without_armies(monkeypatch):
    stored = aggregate_usage([fact("h900p900e14_900u5x900-1x901s2x900i7x900-1x901d3x900")])
    assert usage_rows(stored["troops"], "troops") == ([], True)
    assert usage_rows(stored["troops"], "siege") == ([], True)
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
        rows, unresolved = usage_rows(stored["troops" if category == "siege" else category], category)
        assert not unresolved
        row = next(r for r in rows if r["unit_id"] == typed_id)
        assert (row["quantity"], row["battle_trophies"], row["usage_count"], row["usage_denominator"]) == (quantity, 6000, 1, 1)
        assert row["label"].startswith("Named")


def test_clan_castle_changes_cannot_change_retained_usage():
    home = "h0p9e14_32u5x58-1x97s2x2"
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i2x58-1x97d1x2")])
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i5x900-1x901d2x900")])
