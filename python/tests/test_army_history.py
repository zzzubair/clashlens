"""Finalized usage keeps unit quantities and star outcomes by ID."""
import json
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace

from clashlens import api_analytics, catalog
from clashlens.army_decoder import DecodedArmy, decode_army_share_code
from clashlens.army_history import aggregate_usage, usage_rows
from clashlens.army_season_summaries import PROJECTION_VERSION


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


def test_thousands_of_battles_fit_storage_and_response_bounds(monkeypatch):
    for unit_id in range(100, 105):
        monkeypatch.setitem(catalog._CATALOG_ENTRIES, f"troop:{unit_id}", {
            "name": f"Unit {unit_id}", "category": "troop", "is_siege": False,
        })
    facts = [
        {
            "army_state": "decoded", "stars": index % 4,
            "home_troops": [
                [f"troop:{unit_id}", (index + unit_id) % 100 + 1, "home"]
                for unit_id in range(100, 105)
            ],
            "spells": [], "siege": [], "heroes": [],
            "unresolved_components": [],
        }
        for index in range(15_000)
    ]
    retained = aggregate_usage(facts)["troops"]
    assert len(json.dumps(retained).encode()) < 524_288
    assert sum(row[2] for row in retained) == 75_000

    stored_row = (
        28,
        0,
        [],
        "complete",
        15_000,
        15_000,
        {"fully_decoded": 15_000},
        0,
        0,
        0,
        0,
        [],
        PROJECTION_VERSION,
        "a" * 64,
        retained,
    )

    class Connection:
        def execute(self, *_args, **_kwargs):
            return self

        def fetchone(self):
            return stored_row

    @contextmanager
    def connection():
        yield Connection()

    database = SimpleNamespace(pool=SimpleNamespace(connection=connection))
    response_page = api_analytics.get_army_season_summary(
        database, "season", "offense", "troops", "usage-rate"
    )
    assert response_page is not None
    assert response_page["pagination"] == {
        "offset": 0,
        "total_rows": 500,
        "next_offset": 200,
    }
    assert len(response_page["rows"]) == 200
    response_bytes = json.dumps(response_page, separators=(",", ":")).encode()
    assert len(response_bytes) < 1_048_576


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
        assert (row["quantity"], row["usage_count"], row["usage_denominator"]) == (
            quantity, 1, 1,
        )
        assert (row["one_star_count"], row["two_star_count"],
                row["three_star_count"]) == (0, 0, 1)
        assert row["label"].startswith("Named")


def test_clan_castle_changes_cannot_change_retained_usage():
    home = "h0p9e14_32u5x58-1x97s2x2"
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i2x58-1x97d1x2")])
    assert aggregate_usage([fact(home)]) == aggregate_usage([
        fact(home + "i5x900-1x901d2x900")])
