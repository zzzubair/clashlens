"""Shared setup for army history tests."""

from clashlens.army_decoder import DecodedArmy, decode_army_share_code


def fact(code, stars=3, destruction=100, trophies=6000):
    army = decode_army_share_code(code)
    assert isinstance(army, DecodedArmy)
    return {
        "army_state": "decoded", "stars": stars,
        "destruction_percentage": destruction, "battle_time_trophies": trophies,
        "home_troops": [[f.typed_id, f.quantity, f.origin] for f in army.home_troops],
        "spells": [[f.typed_id, f.quantity, f.origin] for f in army.spells],
        "siege": [[f.typed_id, f.quantity, f.origin] for f in army.siege],
        "cc_troops": [[f.typed_id, f.quantity, f.origin] for f in army.cc_troops],
        "heroes": [{"hero": h.hero_typed_id, "pet": h.pet_typed_id,
                    "equipment": list(h.equipment_typed_ids)} for h in army.heroes],
        "unresolved_components": [],
        "perspective_disagreement": False,
    }
