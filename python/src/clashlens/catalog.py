from __future__ import annotations

import hashlib
import json

CATALOG_VERSION = "unit-catalog-v1"
CATALOG_PROVENANCE = "ClashKingInc/clashy.py@0703aee64a24c48aef296856bd688704d434181f coc/static/static_data.json blob b90a6b2bfbccac3b755a68f78fe8885b35bc80d6 sha256 3fa1e2b9ccd4a24f48ca7ade5a23d54c8ce0c3f17ad986270f2b913584f9c1d5; fixture h0p9e14_32d1x53u2x58-1x97s2x2 observed 2026-08-21"
CATALOG_LICENSE = "MIT; Supercell Fan Content Policy applies to game metadata"

_CATALOG_ENTRIES: dict[str, dict[str, str | bool]] = {
    "equipment:0": {
        "category": "equipment",
        "is_siege": False,
        "name": "Barbarian Puppet",
    },
    "equipment:1": {"category": "equipment", "is_siege": False, "name": "Rage Vial"},
    "equipment:10": {
        "category": "equipment",
        "is_siege": False,
        "name": "Giant Gauntlet",
    },
    "equipment:11": {"category": "equipment", "is_siege": False, "name": "Vampstache"},
    "equipment:12": {"category": "equipment", "is_siege": False, "name": "Haste Vial"},
    "equipment:13": {
        "category": "equipment",
        "is_siege": False,
        "name": "Rocket Spear",
    },
    "equipment:14": {"category": "equipment", "is_siege": False, "name": "Spiky Ball"},
    "equipment:15": {
        "category": "equipment",
        "is_siege": False,
        "name": "Frozen Arrow",
    },
    "equipment:16": {
        "category": "equipment",
        "is_siege": False,
        "name": "Monolith Arrow",
    },
    "equipment:17": {"category": "equipment", "is_siege": False, "name": "Giant Arrow"},
    "equipment:19": {
        "category": "equipment",
        "is_siege": False,
        "name": "Heroic Torch",
    },
    "equipment:2": {
        "category": "equipment",
        "is_siege": False,
        "name": "Archer Puppet",
    },
    "equipment:20": {
        "category": "equipment",
        "is_siege": False,
        "name": "Healer Puppet",
    },
    "equipment:22": {"category": "equipment", "is_siege": False, "name": "Fireball"},
    "equipment:24": {"category": "equipment", "is_siege": False, "name": "Rage Gem"},
    "equipment:3": {
        "category": "equipment",
        "is_siege": False,
        "name": "Invisibility Vial",
    },
    "equipment:32": {
        "category": "equipment",
        "is_siege": False,
        "name": "Snake Bracelet",
    },
    "equipment:34": {
        "category": "equipment",
        "is_siege": False,
        "name": "Healing Tome",
    },
    "equipment:35": {"category": "equipment", "is_siege": False, "name": "Dark Crown"},
    "equipment:39": {
        "category": "equipment",
        "is_siege": False,
        "name": "Magic Mirror",
    },
    "equipment:4": {"category": "equipment", "is_siege": False, "name": "Eternal Tome"},
    "equipment:40": {
        "category": "equipment",
        "is_siege": False,
        "name": "Electro Boots",
    },
    "equipment:41": {
        "category": "equipment",
        "is_siege": False,
        "name": "Lavaloon Puppet",
    },
    "equipment:42": {
        "category": "equipment",
        "is_siege": False,
        "name": "Henchmen Puppet",
    },
    "equipment:43": {"category": "equipment", "is_siege": False, "name": "Dark Orb"},
    "equipment:44": {"category": "equipment", "is_siege": False, "name": "Metal Pants"},
    "equipment:47": {"category": "equipment", "is_siege": False, "name": "Noble Iron"},
    "equipment:48": {
        "category": "equipment",
        "is_siege": False,
        "name": "Action Figure",
    },
    "equipment:49": {
        "category": "equipment",
        "is_siege": False,
        "name": "Meteor Staff",
    },
    "equipment:5": {"category": "equipment", "is_siege": False, "name": "Life Gem"},
    "equipment:50": {"category": "equipment", "is_siege": False, "name": "Frost Flake"},
    "equipment:51": {"category": "equipment", "is_siege": False, "name": "Stick Horse"},
    "equipment:52": {"category": "equipment", "is_siege": False, "name": "Fire Heart"},
    "equipment:53": {
        "category": "equipment",
        "is_siege": False,
        "name": "Rocket Backpack",
    },
    "equipment:56": {
        "category": "equipment",
        "is_siege": False,
        "name": "Stun Blaster",
    },
    "equipment:57": {
        "category": "equipment",
        "is_siege": False,
        "name": "Flame Blower",
    },
    "equipment:59": {
        "category": "equipment",
        "is_siege": False,
        "name": "Electro Fangs",
    },
    "equipment:6": {
        "category": "equipment",
        "is_siege": False,
        "name": "Seeking Shield",
    },
    "equipment:7": {"category": "equipment", "is_siege": False, "name": "Royal Gem"},
    "equipment:8": {
        "category": "equipment",
        "is_siege": False,
        "name": "Earthquake Boots",
    },
    "equipment:9": {
        "category": "equipment",
        "is_siege": False,
        "name": "Hog Rider Puppet",
    },
    "hero:0": {"category": "hero", "is_siege": False, "name": "Barbarian King"},
    "hero:1": {"category": "hero", "is_siege": False, "name": "Archer Queen"},
    "hero:2": {"category": "hero", "is_siege": False, "name": "Grand Warden"},
    "hero:4": {"category": "hero", "is_siege": False, "name": "Royal Champion"},
    "hero:6": {"category": "hero", "is_siege": False, "name": "Minion Prince"},
    "hero:7": {"category": "hero", "is_siege": False, "name": "Dragon Duke"},
    "pet:0": {"category": "pet", "is_siege": False, "name": "L.A.S.S.I"},
    "pet:1": {"category": "pet", "is_siege": False, "name": "Mighty Yak"},
    "pet:10": {"category": "pet", "is_siege": False, "name": "Spirit Fox"},
    "pet:11": {"category": "pet", "is_siege": False, "name": "Angry Jelly"},
    "pet:16": {"category": "pet", "is_siege": False, "name": "Sneezy"},
    "pet:17": {"category": "pet", "is_siege": False, "name": "Greedy Raven"},
    "pet:2": {"category": "pet", "is_siege": False, "name": "Electro Owl"},
    "pet:3": {"category": "pet", "is_siege": False, "name": "Unicorn"},
    "pet:4": {"category": "pet", "is_siege": False, "name": "Phoenix"},
    "pet:7": {"category": "pet", "is_siege": False, "name": "Poison Lizard"},
    "pet:8": {"category": "pet", "is_siege": False, "name": "Diggy"},
    "pet:9": {"category": "pet", "is_siege": False, "name": "Frosty"},
    "spell:0": {"category": "spell", "is_siege": False, "name": "Lightning Spell"},
    "spell:1": {"category": "spell", "is_siege": False, "name": "Healing Spell"},
    "spell:10": {"category": "spell", "is_siege": False, "name": "Earthquake Spell"},
    "spell:109": {"category": "spell", "is_siege": False, "name": "Ice Block Spell"},
    "spell:11": {"category": "spell", "is_siege": False, "name": "Haste Spell"},
    "spell:120": {"category": "spell", "is_siege": False, "name": "Totem Spell"},
    "spell:123": {"category": "spell", "is_siege": False, "name": "Angry Spell"},
    "spell:16": {"category": "spell", "is_siege": False, "name": "Clone Spell"},
    "spell:17": {"category": "spell", "is_siege": False, "name": "Skeleton Spell"},
    "spell:2": {"category": "spell", "is_siege": False, "name": "Rage Spell"},
    "spell:28": {"category": "spell", "is_siege": False, "name": "Bat Spell"},
    "spell:3": {"category": "spell", "is_siege": False, "name": "Jump Spell"},
    "spell:35": {"category": "spell", "is_siege": False, "name": "Invisibility Spell"},
    "spell:5": {"category": "spell", "is_siege": False, "name": "Freeze Spell"},
    "spell:53": {"category": "spell", "is_siege": False, "name": "Recall Spell"},
    "spell:6": {"category": "spell", "is_siege": False, "name": "Santa's Surprise"},
    "spell:70": {"category": "spell", "is_siege": False, "name": "Overgrowth Spell"},
    "spell:73": {"category": "spell", "is_siege": False, "name": "Bag of Frostmites"},
    "spell:9": {"category": "spell", "is_siege": False, "name": "Poison Spell"},
    "spell:98": {"category": "spell", "is_siege": False, "name": "Revive Spell"},
    "troop:0": {"category": "troop", "is_siege": False, "name": "Barbarian"},
    "troop:1": {"category": "troop", "is_siege": False, "name": "Archer"},
    "troop:10": {"category": "troop", "is_siege": False, "name": "Minion"},
    "troop:101": {"category": "troop", "is_siege": False, "name": "Barcher"},
    "troop:102": {"category": "troop", "is_siege": False, "name": "Witch Golem"},
    "troop:103": {"category": "troop", "is_siege": False, "name": "Hog Wizard"},
    "troop:104": {"category": "troop", "is_siege": False, "name": "Lavaloon"},
    "troop:109": {"category": "troop", "is_siege": False, "name": "Ruin Witch"},
    "troop:11": {"category": "troop", "is_siege": False, "name": "Hog Rider"},
    "troop:110": {"category": "troop", "is_siege": False, "name": "Root Rider"},
    "troop:118": {"category": "troop", "is_siege": False, "name": "C.O.O.K.I.E"},
    "troop:119": {"category": "troop", "is_siege": False, "name": "Firecracker"},
    "troop:12": {"category": "troop", "is_siege": False, "name": "Valkyrie"},
    "troop:120": {"category": "troop", "is_siege": False, "name": "Azure Dragon"},
    "troop:121": {"category": "troop", "is_siege": False, "name": "Barbarian Kicker"},
    "troop:122": {"category": "troop", "is_siege": False, "name": "Giant Thrower"},
    "troop:123": {"category": "troop", "is_siege": False, "name": "Druid"},
    "troop:125": {"category": "troop", "is_siege": False, "name": "Broom Witch"},
    "troop:13": {"category": "troop", "is_siege": False, "name": "Golem"},
    "troop:130": {"category": "troop", "is_siege": False, "name": "Ice Minion"},
    "troop:132": {"category": "troop", "is_siege": False, "name": "Thrower"},
    "troop:135": {"category": "troop", "is_siege": True, "name": "Troop Launcher"},
    "troop:136": {"category": "troop", "is_siege": False, "name": "Debt Collector"},
    "troop:142": {"category": "troop", "is_siege": False, "name": "Snake Barrel"},
    "troop:147": {"category": "troop", "is_siege": False, "name": "Super Yeti"},
    "troop:15": {"category": "troop", "is_siege": False, "name": "Witch"},
    "troop:150": {"category": "troop", "is_siege": False, "name": "Furnace"},
    "troop:156": {"category": "troop", "is_siege": False, "name": "Giant Giant"},
    "troop:157": {"category": "troop", "is_siege": False, "name": "K.A.N.E"},
    "troop:158": {"category": "troop", "is_siege": False, "name": "The Disarmer"},
    "troop:159": {"category": "troop", "is_siege": False, "name": "YEETer"},
    "troop:167": {"category": "troop", "is_siege": False, "name": "Meteor Golem"},
    "troop:17": {"category": "troop", "is_siege": False, "name": "Lava Hound"},
    "troop:177": {"category": "troop", "is_siege": False, "name": "Meteor Golem"},
    "troop:188": {"category": "troop", "is_siege": True, "name": "Sky Wagon"},
    "troop:2": {"category": "troop", "is_siege": False, "name": "Goblin"},
    "troop:22": {"category": "troop", "is_siege": False, "name": "Bowler"},
    "troop:23": {"category": "troop", "is_siege": False, "name": "Baby Dragon"},
    "troop:24": {"category": "troop", "is_siege": False, "name": "Miner"},
    "troop:26": {"category": "troop", "is_siege": False, "name": "Super Barbarian"},
    "troop:27": {"category": "troop", "is_siege": False, "name": "Super Archer"},
    "troop:28": {"category": "troop", "is_siege": False, "name": "Super Wall Breaker"},
    "troop:29": {"category": "troop", "is_siege": False, "name": "Super Giant"},
    "troop:3": {"category": "troop", "is_siege": False, "name": "Giant"},
    "troop:30": {"category": "troop", "is_siege": False, "name": "Ice Wizard"},
    "troop:4": {"category": "troop", "is_siege": False, "name": "Wall Breaker"},
    "troop:45": {"category": "troop", "is_siege": False, "name": "Battle Ram"},
    "troop:47": {"category": "troop", "is_siege": False, "name": "Royal Ghost"},
    "troop:48": {"category": "troop", "is_siege": False, "name": "Pumpkin Barbarian"},
    "troop:5": {"category": "troop", "is_siege": False, "name": "Balloon"},
    "troop:50": {"category": "troop", "is_siege": False, "name": "Giant Skeleton"},
    "troop:51": {"category": "troop", "is_siege": True, "name": "Wall Wrecker"},
    "troop:52": {"category": "troop", "is_siege": True, "name": "Battle Blimp"},
    "troop:53": {"category": "troop", "is_siege": False, "name": "Yeti"},
    "troop:55": {"category": "troop", "is_siege": False, "name": "Sneaky Goblin"},
    "troop:56": {"category": "troop", "is_siege": False, "name": "Super Miner"},
    "troop:57": {"category": "troop", "is_siege": False, "name": "Rocket Balloon"},
    "troop:58": {"category": "troop", "is_siege": False, "name": "Ice Golem"},
    "troop:59": {"category": "troop", "is_siege": False, "name": "Electro Dragon"},
    "troop:6": {"category": "troop", "is_siege": False, "name": "Wizard"},
    "troop:61": {"category": "troop", "is_siege": False, "name": "Skeleton Barrel"},
    "troop:62": {"category": "troop", "is_siege": True, "name": "Stone Slammer"},
    "troop:63": {"category": "troop", "is_siege": False, "name": "Inferno Dragon"},
    "troop:64": {"category": "troop", "is_siege": False, "name": "Super Valkyrie"},
    "troop:65": {"category": "troop", "is_siege": False, "name": "Dragon Rider"},
    "troop:66": {"category": "troop", "is_siege": False, "name": "Super Witch"},
    "troop:67": {"category": "troop", "is_siege": False, "name": "M.E.C.H.A"},
    "troop:7": {"category": "troop", "is_siege": False, "name": "Healer"},
    "troop:72": {"category": "troop", "is_siege": False, "name": "Party Wizard"},
    "troop:75": {"category": "troop", "is_siege": True, "name": "Siege Barracks"},
    "troop:76": {"category": "troop", "is_siege": False, "name": "Ice Hound"},
    "troop:8": {"category": "troop", "is_siege": False, "name": "Dragon"},
    "troop:80": {"category": "troop", "is_siege": False, "name": "Super Bowler"},
    "troop:81": {"category": "troop", "is_siege": False, "name": "Super Dragon"},
    "troop:82": {"category": "troop", "is_siege": False, "name": "Headhunter"},
    "troop:83": {"category": "troop", "is_siege": False, "name": "Super Wizard"},
    "troop:84": {"category": "troop", "is_siege": False, "name": "Super Minion"},
    "troop:87": {"category": "troop", "is_siege": True, "name": "Log Launcher"},
    "troop:9": {"category": "troop", "is_siege": False, "name": "P.E.K.K.A"},
    "troop:91": {"category": "troop", "is_siege": True, "name": "Flame Flinger"},
    "troop:92": {"category": "troop", "is_siege": True, "name": "Battle Drill"},
    "troop:94": {"category": "troop", "is_siege": False, "name": "Ram Rider"},
    "troop:95": {"category": "troop", "is_siege": False, "name": "Electro Titan"},
    "troop:97": {"category": "troop", "is_siege": False, "name": "Apprentice Warden"},
    "troop:98": {"category": "troop", "is_siege": False, "name": "Super Hog Rider"},
}

_CATALOG_CANONICAL_JSON = json.dumps(
    _CATALOG_ENTRIES, sort_keys=True, separators=(",", ":")
)
CATALOG_HASH = hashlib.sha256(_CATALOG_CANONICAL_JSON.encode()).hexdigest()


def catalog_name(typed_id: str) -> str | None:
    entry = _CATALOG_ENTRIES.get(typed_id)
    return str(entry["name"]) if entry else None


def is_siege_troop(typed_id: str) -> bool:
    entry = _CATALOG_ENTRIES.get(typed_id)
    return bool(entry and entry["is_siege"])


def is_valid_typed_id(typed_id: str) -> bool:
    return typed_id in _CATALOG_ENTRIES


def catalog_entries() -> dict[str, dict[str, str | bool]]:
    return {typed_id: dict(entry) for typed_id, entry in _CATALOG_ENTRIES.items()}


def catalog_version_info() -> dict[str, str]:
    return {
        "version": CATALOG_VERSION,
        "hash": CATALOG_HASH,
        "provenance": CATALOG_PROVENANCE,
        "license": CATALOG_LICENSE,
    }
