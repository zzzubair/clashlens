from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .catalog import CATALOG_HASH, CATALOG_VERSION, is_siege_troop, is_valid_typed_id

DECODER_VERSION = "army-decoder-v1"


@dataclass(frozen=True, slots=True)
class TroopFact:
    typed_id: str
    quantity: int
    origin: str


@dataclass(frozen=True, slots=True)
class SpellFact:
    typed_id: str
    quantity: int
    origin: str


@dataclass(frozen=True, slots=True)
class SiegeFact:
    typed_id: str
    quantity: int
    origin: str


@dataclass(frozen=True, slots=True)
class HeroFact:
    hero_typed_id: str
    pet_typed_id: str | None
    equipment_typed_ids: tuple[str, ...]
    raw_m: str | None


@dataclass(frozen=True, slots=True)
class DecodedArmy:
    home_troops: tuple[TroopFact, ...]
    cc_troops: tuple[TroopFact, ...]
    spells: tuple[SpellFact, ...]
    home_spells_raw: tuple[SpellFact, ...]
    cc_spells_raw: tuple[SpellFact, ...]
    siege: tuple[SiegeFact, ...]
    heroes: tuple[HeroFact, ...]
    raw_code: str
    decoder_version: str
    catalog_version: str
    catalog_hash: str
    identity_hash: str


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    raw_code: str | None
    category: str
    detail: str
    decoder_version: str
    catalog_version: str
    catalog_hash: str


class DecodeError(Exception):
    def __init__(self, category: str, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


_QTY_ID_RE = re.compile(r"^\d+x\d+$")
_SECTION_RE = re.compile(r"([husid])([^husid]*)")
_HERO_RE = re.compile(r"^(\d+)(?:m(\d+))?(?:p(\d+))?(?:e([\d_]+))?$")


def decode_army_share_code(raw_code: str | None) -> DecodedArmy | DecodeFailure:
    if raw_code is None:
        return DecodeFailure(
            None,
            "missing_army_share_code",
            "armyShareCode is missing",
            DECODER_VERSION,
            CATALOG_VERSION,
            CATALOG_HASH,
        )
    if not isinstance(raw_code, str):
        return DecodeFailure(
            str(raw_code),
            "malformed",
            "armyShareCode must be text",
            DECODER_VERSION,
            CATALOG_VERSION,
            CATALOG_HASH,
        )
    if raw_code == "":
        return DecodeFailure(
            "",
            "empty_army_share_code",
            "armyShareCode is empty",
            DECODER_VERSION,
            CATALOG_VERSION,
            CATALOG_HASH,
        )
    try:
        return _decode(raw_code)
    except DecodeError as e:
        return DecodeFailure(
            raw_code,
            e.category,
            e.detail,
            DECODER_VERSION,
            CATALOG_VERSION,
            CATALOG_HASH,
        )


def _guard_int(s: str, label: str) -> int:
    if len(s) > 7:
        raise DecodeError("malformed", f"huge integer {label}")
    try:
        v = int(s)
    except ValueError:
        raise DecodeError("malformed", f"invalid integer {label}") from None
    if v < 0 or v > 1_000_000:
        raise DecodeError("malformed", f"out of range {label}")
    return v


def _check_typed(typed: str, expected_ns: str) -> None:
    if is_valid_typed_id(typed):
        return
    rid = typed.split(":", 1)[1]
    for ns in ("troop", "spell", "hero", "pet", "equipment"):
        if ns == expected_ns:
            continue
        if is_valid_typed_id(f"{ns}:{rid}"):
            raise DecodeError("wrong_category", f"{typed} exists as {ns}")
    raise DecodeError("unknown_id", f"unknown {typed}")


def _parse_section(content: str, ns: str) -> list[tuple[str, int]]:
    if content == "":
        raise DecodeError("partial", f"empty {ns} section")
    if content.startswith("-") or content.endswith("-") or "--" in content:
        raise DecodeError("malformed", f"empty entry in {ns}")
    out: list[tuple[str, int]] = []
    for entry in content.split("-"):
        if entry == "":
            raise DecodeError("malformed", f"empty entry in {ns}")
        if not _QTY_ID_RE.fullmatch(entry):
            for ch in entry:
                if ch.isalpha() and ch != "x":
                    raise DecodeError("unknown_token", f"unknown char {ch} in {entry}")
            raise DecodeError("malformed", f"malformed entry {entry}")
        qty_s, id_s = entry.split("x", 1)
        qty = _guard_int(qty_s, "quantity")
        rid = _guard_int(id_s, "id")
        if qty <= 0 or qty > 1000:
            raise DecodeError("malformed", f"quantity {qty} out of range")
        typed = f"{ns}:{rid}"
        _check_typed(typed, ns)
        out.append((typed, qty))
    return out


def _decode(raw: str) -> DecodedArmy:
    if len(raw) > 2000:
        raise DecodeError("malformed", "share code too long")
    for ch in raw:
        if ch.isalpha() and ch not in "husidpemx":
            raise DecodeError("unknown_token", f"unknown token {ch}")
        if ch not in "0123456789x_-.husidpem":
            raise DecodeError("malformed", f"illegal char {ch}")
    if not raw or raw[0] not in "husid":
        raise DecodeError("malformed", "must start with h/u/s/i/d")
    sections: dict[str, str] = {}
    pos = 0
    for m in _SECTION_RE.finditer(raw):
        letter, content = m.group(1), m.group(2)
        if letter in sections:
            raise DecodeError("malformed", f"duplicate section {letter}")
        sections[letter] = content
        pos = m.end()
    if pos != len(raw):
        raise DecodeError("malformed", f"trailing {raw[pos:]}")

    home_troops: list[TroopFact] = []
    cc_troops: list[TroopFact] = []
    home_spells: list[SpellFact] = []
    cc_spells: list[SpellFact] = []
    siege: list[SiegeFact] = []
    heroes: list[HeroFact] = []

    if "u" in sections:
        for typed, qty in _parse_section(sections["u"], "troop"):
            if is_siege_troop(typed):
                siege.append(SiegeFact(typed, qty, "home"))
            else:
                home_troops.append(TroopFact(typed, qty, "home"))
    if "i" in sections:
        for typed, qty in _parse_section(sections["i"], "troop"):
            if is_siege_troop(typed):
                siege.append(SiegeFact(typed, qty, "clan_castle"))
            else:
                cc_troops.append(TroopFact(typed, qty, "clan_castle"))
    if "s" in sections:
        for typed, qty in _parse_section(sections["s"], "spell"):
            home_spells.append(SpellFact(typed, qty, "home"))
    if "d" in sections:
        for typed, qty in _parse_section(sections["d"], "spell"):
            cc_spells.append(SpellFact(typed, qty, "clan_castle"))

    if "h" in sections:
        h_content = sections["h"]
        if h_content == "":
            raise DecodeError("partial", "empty hero section")
        seen_heroes: set[str] = set()
        chips = h_content.split("-")
        for chip in chips:
            if chip == "":
                raise DecodeError("malformed", "empty hero entry")
            match = _HERO_RE.fullmatch(chip)
            if not match:
                for ch in chip:
                    if ch.isalpha() and ch not in "pme":
                        raise DecodeError(
                            "unknown_token", f"unknown hero suffix {ch} in {chip}"
                        )
                raise DecodeError("malformed", f"malformed hero {chip}")
            hero_s, m_s, pet_s, equip_s = match.groups()
            hero_id = _guard_int(hero_s, "hero")
            hero_typed = f"hero:{hero_id}"
            _check_typed(hero_typed, "hero")
            if hero_typed in seen_heroes:
                raise DecodeError("malformed", f"duplicate hero {hero_typed}")
            seen_heroes.add(hero_typed)
            pet_typed = None
            if pet_s is not None:
                pid = _guard_int(pet_s, "pet")
                pet_typed = f"pet:{pid}"
                _check_typed(pet_typed, "pet")
            equip_list: list[str] = []
            raw_m = f"m{m_s}" if m_s is not None else None
            if m_s is not None:
                _guard_int(m_s, "m")
            if equip_s is not None:
                if equip_s.startswith("_") or equip_s.endswith("_") or "__" in equip_s:
                    raise DecodeError("malformed", f"bad equipment {equip_s}")
                parts = equip_s.split("_")
                if len(parts) > 2:
                    raise DecodeError("malformed", "equipment exceeds two")
                for part in parts:
                    if part == "":
                        raise DecodeError("malformed", "empty equipment")
                    eid = _guard_int(part, "equipment")
                    eq_typed = f"equipment:{eid}"
                    _check_typed(eq_typed, "equipment")
                    if eq_typed in equip_list:
                        raise DecodeError(
                            "malformed", f"duplicate equipment {eq_typed}"
                        )
                    equip_list.append(eq_typed)
            heroes.append(
                HeroFact(
                    hero_typed,
                    pet_typed,
                    tuple(sorted(equip_list)),
                    raw_m,
                )
            )

    pooled = tuple(sorted(home_spells + cc_spells, key=lambda x: x.typed_id))

    from collections import Counter

    def agg_troops(facts: list[TroopFact]) -> list[tuple[str, int]]:
        c = Counter()
        for f in facts:
            c[f.typed_id] += f.quantity
        return sorted(c.items())

    def agg_spells(facts: tuple[SpellFact, ...]) -> list[tuple[str, int]]:
        c = Counter()
        for f in facts:
            c[f.typed_id] += f.quantity
        return sorted(c.items())

    payload = {
        "home_troops": agg_troops(home_troops),
        "spells": agg_spells(pooled),
        "heroes": sorted(
            (h.hero_typed_id, h.pet_typed_id, tuple(sorted(h.equipment_typed_ids)))
            for h in heroes
        ),
    }
    identity_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return DecodedArmy(
        home_troops=tuple(home_troops),
        cc_troops=tuple(cc_troops),
        spells=pooled,
        home_spells_raw=tuple(home_spells),
        cc_spells_raw=tuple(cc_spells),
        siege=tuple(siege),
        heroes=tuple(sorted(heroes, key=lambda h: h.hero_typed_id)),
        raw_code=raw,
        decoder_version=DECODER_VERSION,
        catalog_version=CATALOG_VERSION,
        catalog_hash=CATALOG_HASH,
        identity_hash=identity_hash,
    )
